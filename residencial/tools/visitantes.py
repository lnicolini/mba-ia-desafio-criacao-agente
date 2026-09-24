"""Tools do especialista de visitantes.

Autorizar um visitante libera o acesso de alguém ao prédio, então **sempre**
pede confirmação do sistema (Garantia 1). O apartamento vem da sessão
(Garantia 2).
"""

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .. import db
from .sessao import apartamento_da_sessao, chave_idempotencia, confirmada_pelo_sistema


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento do morador desta conversa."""
    apto = apartamento_da_sessao(tool_context)
    return {"visitantes": db.listar_visitantes(apto)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Liberar acesso sempre exige aprovação do morador pelo aplicativo; o
    sistema pede a confirmação sozinho. O que o morador escreve no chat não
    conta como aprovação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    nome = " ".join((nome or "").split())
    if not nome:
        return {"erro": "Informe o nome do visitante."}
    try:
        d = db.validar_data(data)
    except db.RegraViolada as exc:
        return {"erro": str(exc)}

    # Defesa em profundidade da Garantia 1 (ver reservas.reservar_area).
    if not confirmada_pelo_sistema(tool_context):
        return {"erro": "Liberar acesso exige aprovação pelo aplicativo. Nada foi gravado."}

    apto = apartamento_da_sessao(tool_context)
    registro = db.autorizar_visitante(apto, nome, d, chave_idempotencia(tool_context))
    return {"status": "autorizado", "mensagem": "Entrada autorizada e registrada na portaria.", **registro}


TOOLS_VISITANTES = [
    listar_meus_visitantes,
    FunctionTool(autorizar_visitante, require_confirmation=True),
]
