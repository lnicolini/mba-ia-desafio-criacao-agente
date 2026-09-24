# Residencial Aurora: assistente virtual com Google ADK

Assistente dos moradores do Residencial Aurora, entregue como uma API HTTP ([FastAPI](https://fastapi.tiangolo.com/) + [Google ADK](https://google.github.io/adk-docs/) `2.9.1`). Pelo chat, cada morador reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias reservas, autoriza a entrada de visitantes e consulta o regulamento interno.

O Gemini conduz o diálogo. As regras que não podem ser quebradas, porém, não ficam na conversa: ficam nas tools, no banco e nas rotas da API. É essa separação que impede qualquer mensagem de contorná-las.

## Como o projeto está organizado

```
residencial/
├── agents.py          # agente principal + especialistas, App do ADK
├── api.py             # rotas HTTP, Runner, lógica de confirmação e eventos
├── db.py              # SQLite do condomínio (reservas, visitantes, códigos)
├── config.py          # caminhos, modelos, carrega o .env
├── restore.py         # restaura reservas/visitantes ao estado de dados/
├── __main__.py        # sobe a API (uvicorn, porta 8000)
└── tools/
    ├── sessao.py      # apartamento da sessão, idempotência, confirmação
    ├── reservas.py    # tools de reservas
    ├── visitantes.py  # tools de visitantes
    └── regulamento.py # tools de leitura do regulamento
dados/                 # estado inicial (somente leitura, nunca é alterado)
var/                   # bancos SQLite gerados em execução (fora do Git)
```

## Arquitetura

```
                 POST /sessoes/{id}/mensagens · /confirmacoes
                              │
                    Runner (ADK) + SqliteSessionService
                              │
                   ┌──────────▼──────────┐
                   │  assistente_aurora  │  principal: sem tools de negócio,
                   │     (LlmAgent)      │  sem o regulamento nas instruções
                   └───┬──────────┬───┬──┘
       transferência    │          │   │  AgentTool (sessão isolada)
      ┌─────────────────┘          │   └───────────────┐
┌─────▼────────────┐  ┌────────────▼───────┐   ┌────────▼─────────────┐
│ esp. reservas    │  │ esp. visitantes    │   │ esp. regulamento     │
│  (sub_agent)     │  │  (sub_agent)       │   │ listar_capitulos     │
└─────┬────────────┘  └────────┬───────────┘   │ ler_capitulo         │
      └────────────┬───────────┘               └──────────────────────┘
                   ▼
        var/condominio.db (SQLite)
```

| Agente | Papel | Como entra em cena | Por que assim |
|---|---|---|---|
| `assistente_aurora` (principal) | Porta de entrada: entende o pedido e encaminha; responde cumprimentos. Não lê nem grava dados do condomínio. | É o `root_agent` do `App`; o `Runner` o executa. | Mantém o prompt curto e barato; o regulamento não entra nas instruções dele (Garantia 4). |
| `especialista_reservas` | Consulta áreas/disponibilidade e reserva, lista e cancela reservas **do apartamento da sessão**. | `sub_agent`, acionado por transferência. | Reservar é multi-turno e pode parar numa confirmação; a retomada precisa voltar a quem pediu (Garantia 1). |
| `especialista_visitantes` | Autoriza e lista visitantes **do apartamento da sessão**. | `sub_agent`, acionado por transferência. | Mesma razão: a confirmação de acesso precisa ser retomada pelo agente que a pediu. |
| `especialista_regulamento` | Responde dúvidas lendo **um capítulo por vez**. | **`AgentTool`** do principal. | Roda numa sessão própria e descartável; o texto do capítulo não entra na sessão do morador (Garantia 4). |

Os especialistas usam a transferência padrão do ADK. A mensagem seguinte vai para o último agente que respondeu, e ele devolve o controle ao principal (ou repassa a outro especialista) quando o assunto muda. Isso também mantém o autor de um pedido de confirmação como "último agente" — por isso a aprovação, mesmo depois de um restart, chega ao agente certo.

Tudo o que o assistente grava vai para o SQLite em `var/` (sem serviço externo):

- `var/condominio.db`: reservas, visitantes e códigos emitidos. Na primeira subida é carregado a partir de `dados/`, que nunca é alterado.
- `var/sessoes.db`: sessões e eventos do ADK (`SqliteSessionService`).

Reservas e visitantes são sempre lidos e gravados pelas tools (`residencial/tools/`), que acessam `var/condominio.db`. Nada vem da memória do modelo.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

Cobrar e liberar acesso são ações que não podem acontecer sem o morador confirmar — e a confirmação vem por uma **rota**, nunca pelo texto da conversa.

| Arquivo | Trecho |
|---|---|
| `residencial/tools/reservas.py` | `reservar_area` é registrada como `FunctionTool(..., require_confirmation=_gera_cobranca)`. `_gera_cobranca` olha **só a taxa da área** (`taxa > 0`). A quadra (taxa 0) passa direto, sem confirmação. |
| `residencial/tools/visitantes.py` | `autorizar_visitante` é `FunctionTool(..., require_confirmation=True)`: liberar acesso **sempre** pede. |
| `residencial/tools/sessao.py` | `confirmada_pelo_sistema()` é a trava de segurança: sem uma `ToolConfirmation` aprovada, `reservar_area` (com taxa) e `autorizar_visitante` se recusam a gravar. |
| `residencial/api.py` | `confirmacoes_pendentes()` lista os pedidos `adk_request_confirmation` da sessão que ainda não têm `FunctionResponse`. `responder_confirmacao()` devolve **409** se o id não estiver nessa lista (inexistente, de outra sessão ou já respondido); se estiver, injeta `FunctionResponse(name="adk_request_confirmation", id=..., response={"confirmed": ...})` e o ADK retoma a tool. |
| `residencial/tools/sessao.py` + `residencial/db.py` | `chave_idempotencia()` (`session_id:function_call_id`) + coluna `chave_idempotencia UNIQUE`: se o ADK reexecutar a tool retomada, o efeito acontece uma única vez. |
| `residencial/db.py` | Índice único `uq_visitante (apartamento, nome, data)` + dedup em `autorizar_visitante()`: se o Runner duplicar a mensagem (comportamento do ADK com `sub_agent`) e a tool rodar duas vezes, a mesma autorização só é gravada uma vez. |

**Por que não dá para burlar:** quem decide "isto pede confirmação?" é o `FunctionTool` (pela taxa ou por ser um acesso), não o modelo. A `ToolConfirmation` só passa a existir quando a rota `/confirmacoes` injeta o `FunctionResponse` — escrever "já confirmei" no chat não cria esse objeto. E, uma vez respondido, o id deixa de estar pendente: reenviar recebe 409 sem chegar ao `Runner`.

### Garantia 2: cada sessão pertence a um apartamento

O apartamento é definido **uma única vez**, na abertura da sessão, e representa o morador autenticado.

| Arquivo | Trecho |
|---|---|
| `residencial/api.py` | `criar_sessao()` grava `state={CHAVE_APARTAMENTO: apartamento}` na criação. Nenhuma tool escreve essa chave depois. |
| `residencial/tools/sessao.py` | `apartamento_da_sessao(tool_context)` lê `tool_context.state["apartamento"]` e valida contra `dados/apartamentos.json`. |
| `residencial/tools/reservas.py`, `residencial/tools/visitantes.py` | **Nenhuma tool tem parâmetro de apartamento.** `listar_minhas_reservas`, `reservar_area`, `cancelar_reserva`, `listar_meus_visitantes` e `autorizar_visitante` usam só `apartamento_da_sessao()`. |
| `residencial/tools/reservas.py` | `cancelar_reserva()` busca o alvo apenas em `db.listar_reservas(apto_da_sessao)`, e `db.cancelar_reserva()` ainda filtra `WHERE apartamento = ?`. Reserva de outro apartamento responde "não encontrada", sem revelar nada. |
| `residencial/tools/reservas.py` + `residencial/db.py` | `consultar_disponibilidade()` / `db.area_ocupada()` devolvem só `livre`/`ocupada`. `reservar_area()` recusada devolve "já está ocupada", **sem código nem apartamento do dono**. |

**Por que não dá para burlar:** o modelo não tem como informar outro apartamento, porque esse parâmetro não existe na tool. E nenhuma tool devolve dados de outro apartamento, então essa informação nunca chega ao contexto nem aos eventos da sessão. "Sou do 302" vira apenas texto.

### Garantia 3: nada se perde no reinício

Reiniciar a API não apaga conversas nem dados.

| Arquivo | Trecho |
|---|---|
| `residencial/api.py` | `lifespan()` monta `SqliteSessionService(str(config.SESSOES_DB))` + `Runner(...)`. Sessões e eventos ficam em `var/sessoes.db`. |
| `residencial/db.py` | Reservas, visitantes e códigos ficam em `var/condominio.db`. `inicializar()` só carrega `dados/` na **primeira** subida (flag `meta.inicializado`), então reiniciar não apaga nada. |
| `residencial/db.py` | Tabela `codigos_emitidos (codigo PRIMARY KEY)` + `_novo_codigo()` (`RSV-` + 6 caracteres aleatórios). Cada código novo é registrado nessa tabela na mesma transação da reserva; um código já usado não é sorteado de novo, nem após cancelamento nem após restauração. |

**Por que não dá para burlar:** a persistência é feita pelo `SessionService` e pelo banco, fora do modelo. Até um pedido de confirmação pendente fica gravado como evento, então uma aprovação feita depois do restart retoma a tool normalmente.

### Garantia 4: o regulamento é consultado, não carregado

O regulamento é longo. Se entrasse inteiro no histórico, acompanharia todas as mensagens seguintes e encareceria cada chamada ao modelo. Por isso ele é lido por demanda.

| Arquivo | Trecho |
|---|---|
| `residencial/agents.py` | A instrução de `assistente_aurora` não contém o regulamento; ele só recebe `AgentTool(agent=especialista_regulamento)`. |
| `residencial/tools/regulamento.py` | `_capitulos()` divide `dados/regulamento.md` por `## Capítulo`. `listar_capitulos()` devolve só os títulos. `ler_capitulo(numero)` devolve **um** capítulo. |
| `google.adk.tools.agent_tool.AgentTool` | O especialista roda num `Runner` próprio, com sessão em memória. As chamadas `listar_capitulos`/`ler_capitulo` e o texto do capítulo ficam nessa sessão descartável. Na sessão do morador entram só `especialista_regulamento(request=pergunta)` e a resposta curta. |

**Por que não dá para burlar:** a tool nunca devolve o regulamento inteiro, e o `AgentTool` isola o que o especialista lê. Mesmo que ele abra o capítulo errado, esse texto não chega aos eventos da sessão do morador.

### Garantia 5: dois moradores, uma reserva

Dois moradores podem pedir a mesma área na mesma data e aprovar ao mesmo tempo. No fim, só um reserva.

| Arquivo | Trecho |
|---|---|
| `residencial/db.py` | Índice único parcial `uq_reserva_ativa ON reservas (area, data) WHERE status = 'ativa'`: o SQLite barra a segunda reserva ativa para a mesma área/data. |
| `residencial/db.py` | `criar_reserva()` faz o `INSERT` dentro de `BEGIN IMMEDIATE`. O `sqlite3.IntegrityError` é capturado e vira `ResultadoReserva(criada=False, motivo="indisponivel")`. |
| `residencial/tools/reservas.py` | `reservar_area()` transforma a recusa numa resposta normal ("já está ocupada… Nenhuma reserva foi criada"), e a API responde 200. |

**Por que não dá para burlar (nem conferindo antes):** `consultar_disponibilidade` serve só para a conversa. A exclusividade é decidida pelo banco **no instante do INSERT**, de forma atômica — vale até entre processos diferentes. Quem perde a corrida recebe `IntegrityError` em vez de gravar uma segunda reserva.

## Como rodar

### Pré-requisitos

- Python 3.12+ e o gerenciador [uv](https://docs.astral.sh/uv/)
- Uma chave de API do [Google AI Studio](https://aistudio.google.com/apikey)
- Sem serviços externos: o armazenamento é um SQLite embutido, criado automaticamente em `var/`.

> **Cota do modelo.** O tier gratuito do Google AI Studio limita as requisições diárias por modelo (ex.: 20/dia em alguns Gemini 3.x), e o fluxo de avaliação faz dezenas de chamadas. Se a sua chave for de tier gratuito, ela pode travar no meio do fluxo — use uma chave com billing ativado ou troque o modelo por um com cota disponível via `AURORA_MODELO_PRINCIPAL`/`AURORA_MODELO_ESPECIALISTAS`.

### Variáveis do `.env`

```bash
cp .env.example .env
```

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | Use `FALSE` (AI Studio). |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal. Padrão: `gemini-3.6-flash`. |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas. Padrão: o mesmo do principal. |

> **Modelo padrão (tier gratuito).** O padrão é `gemini-3.6-flash`, da linha **Flash**, que roda no tier gratuito do Google AI Studio — por isso está sujeito à cota diária citada acima. Para mais folga (ou um modelo Pro, com limites maiores), ative o billing e/ou ajuste as variáveis de modelo.

### Instalar

```bash
uv sync
```

### Restaurar os dados iniciais

Recarrega reservas e visitantes a partir de `dados/` e limpa todas as sessões. Os códigos já usados continuam reservados, para nunca serem reutilizados.

```bash
uv run python -m residencial.restore
```

### Subir a API

```bash
uv run python -m residencial
```

O serviço sobe em `http://localhost:8000`. Ctrl+C encerra; rodar o mesmo comando de novo volta com tudo preservado — conversas, eventos e dados (Garantia 3).

### Contrato resumido

Todas as rotas usam JSON, e as datas das rotas de verificação seguem `AAAA-MM-DD`. Rotas que recebem `{session_id}` devolvem `404` quando a sessão não existe.

| Requisição | Resposta |
|---|---|
| `POST /sessoes` `{"apartamento": "101"}` | `201 {"session_id": "..."}` |
| `POST /sessoes/{id}/mensagens` `{"texto": "..."}` | `200 {"resposta": "...", "confirmacoes_pendentes": [{"id", "acao", "detalhes"}]}` |
| `POST /sessoes/{id}/confirmacoes` `{"id": "...", "confirmado": true}` | `200` (mesmo formato) ou `409` |
| `GET /sessoes/{id}/eventos` | `200 [eventos completos, em ordem]` |
| `GET /apartamentos/{n}/reservas` | `200 [{"codigo", "area", "data"}]` |
| `GET /apartamentos/{n}/visitantes` | `200 [{"nome", "data"}]` |

O formato de um item de `confirmacoes_pendentes` (a ação fica aguardando aprovação):

```json
{"id": "adk-…", "acao": "reservar_area", "detalhes": {"area": "churrasqueira", "data": "2030-08-15", "taxa": 80.0}}
```

```json
{"id": "adk-…", "acao": "autorizar_visitante", "detalhes": {"nome": "Beatriz Lima", "data": "2030-08-16"}}
```
