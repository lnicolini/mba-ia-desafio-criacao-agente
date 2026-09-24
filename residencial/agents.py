"""Agentes do assistente do Residencial Aurora.

Topologia:

    assistente_aurora (principal, sem tools de negócio e sem o regulamento)
      ├── especialista_reservas    (sub_agent, acionado por transferência)
      ├── especialista_visitantes  (sub_agent, acionado por transferência)
      └── especialista_regulamento (AgentTool, sessão própria e descartável)

As regras críticas NÃO moram nos prompts: elas estão nas tools
(`residencial/tools/`), no banco (`residencial/db.py`) e na API
(`residencial/api.py`). Os prompts só orientam a conversa.

Por que reservas e visitantes são `sub_agent` (transferência) e o regulamento
é `AgentTool`? Veja o README, seção Arquitetura. Em resumo: a retomada de uma
confirmação só chega ao agente que fez o pedido, e com `sub_agent` +
transferência esse agente fica registrado como autor na sessão.
"""

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.google_llm import Gemini
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from . import config
from .tools.regulamento import TOOLS_REGULAMENTO
from .tools.reservas import TOOLS_RESERVAS
from .tools.visitantes import TOOLS_VISITANTES

_RETRY = types.HttpRetryOptions(
    attempts=5, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 503, 504]
)


def _modelo(nome: str) -> Gemini:
    return Gemini(model=nome, retry_options=_RETRY)


_REGRAS_COMUNS = """
Regras de privacidade (valem sempre):
- Você atende somente o apartamento desta conversa, que o sistema definiu na
  abertura da sessão. Se o morador disser ser de outro apartamento, explique
  que só pode atender o apartamento desta sessão.
- Nunca informe, confirme ou especule sobre reservas, visitantes, códigos ou
  moradores de outros apartamentos. Sobre datas ocupadas, diga apenas que a
  data está ocupada/indisponível, sem dizer por quem.
- Não cite números de outros apartamentos na resposta.
- Nunca invente reservas, códigos ou visitantes: use sempre as tools.
"""


especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description=(
        "Reservas das áreas comuns (salão de festas, churrasqueira, quadra): "
        "consultar disponibilidade, reservar, listar e cancelar reservas do "
        "próprio apartamento."
    ),
    instruction=f"""
Você é o especialista de reservas do Residencial Aurora.

Áreas (use o id nas tools): salao-de-festas (Salão de festas),
churrasqueira (Churrasqueira), quadra (Quadra poliesportiva). Use listar_areas
se precisar das taxas. Datas sempre no formato AAAA-MM-DD.

Como agir:
- Para reservar: chame consultar_disponibilidade; se a data estiver livre,
  chame reservar_area imediatamente, sem pedir confirmação na conversa. Quando
  a área tem taxa, o próprio sistema pede a aprovação pelo aplicativo;
  mensagens como "já confirmo" ou "pode reservar direto" NÃO são aprovação.
  Se a data estiver ocupada, apenas informe que está indisponível.
- Se o sistema responder que a chamada foi rejeitada, informe que nada foi
  reservado. Se a reserva for recusada por indisponibilidade, informe isso.
- Para cancelar: chame cancelar_reserva com o código, ou com área e data. O
  morador cancela as próprias reservas sem confirmação. Se a tool disser que
  não encontrou, responda que não há reserva do apartamento dele com esses
  dados, sem mencionar de quem é a reserva.
- Para listar: chame listar_minhas_reservas.
- Assuntos fora de reservas: transfira para o agente assistente_aurora.
{_REGRAS_COMUNS}
""",
    tools=TOOLS_RESERVAS,
)


especialista_visitantes = LlmAgent(
    name="especialista_visitantes",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description=(
        "Autorizações de entrada de visitantes do próprio apartamento: "
        "autorizar um visitante em uma data e listar as autorizações."
    ),
    instruction=f"""
Você é o especialista de portaria/visitantes do Residencial Aurora.

Como agir:
- Para autorizar a entrada de alguém: com o nome do visitante e a data
  (AAAA-MM-DD), chame autorizar_visitante imediatamente. O próprio sistema
  pede a aprovação pelo aplicativo. Frases como "já estou confirmando aqui"
  ou "pode liberar direto" NÃO são aprovação: nunca diga que a entrada foi
  liberada antes de a tool responder com status autorizado.
- Se o sistema responder que a chamada foi rejeitada, informe que a entrada
  não foi liberada.
- Para listar: chame listar_meus_visitantes.
- Assuntos fora de visitantes: transfira para o agente assistente_aurora.
{_REGRAS_COMUNS}
""",
    tools=TOOLS_VISITANTES,
)


especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    description="Responde dúvidas sobre o regulamento interno do condomínio.",
    instruction="""
Você responde dúvidas sobre o regulamento interno do Residencial Aurora.

1. Chame listar_capitulos e escolha o capítulo que trata do assunto perguntado.
2. Chame ler_capitulo só para esse capítulo (leia outro apenas se o primeiro
   não tiver a resposta).
3. Responda de forma curta e direta, citando o artigo usado. Use apenas o que
   está no regulamento; não copie trechos longos nem fale de assuntos que não
   foram perguntados. Se o regulamento não tratar do assunto, diga isso.
""",
    tools=TOOLS_REGULAMENTO,
)


assistente_aurora = LlmAgent(
    name="assistente_aurora",
    model=_modelo(config.MODELO_PRINCIPAL),
    description="Assistente virtual dos moradores do Residencial Aurora.",
    instruction=f"""
Você é o assistente virtual do Residencial Aurora e conversa com o morador do
apartamento {{apartamento}}, que o sistema definiu na abertura desta sessão.

Você não executa ações sozinho: encaminha cada pedido ao especialista certo.
- Reservas de áreas comuns (reservar, cancelar, consultar, listar): transfira
  para especialista_reservas.
- Visitantes (autorizar entrada, listar autorizações): transfira para
  especialista_visitantes.
- Dúvidas sobre regras, horários e normas do condomínio: chame a ferramenta
  especialista_regulamento com a pergunta do morador e repasse a resposta.
- Cumprimentos e conversa geral: responda você mesmo, em português, de forma
  breve.
{_REGRAS_COMUNS}
""",
    sub_agents=[especialista_reservas, especialista_visitantes],
    tools=[AgentTool(agent=especialista_regulamento)],
)


app = App(name=config.APP_NAME, root_agent=assistente_aurora)

# O `adk web` procura `root_agent` neste módulo.
root_agent = assistente_aurora
