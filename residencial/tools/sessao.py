"""Garantia 2: o apartamento das tools vem do state da sessão.

O ``apartamento`` é gravado no ``state`` uma única vez, quando a API cria a
sessão (rota ``POST /sessoes``). Nenhuma tool recebe apartamento como
parâmetro — o modelo não tem como escolher outro apartamento, diga o morador
o que disser ("sou do 302", "cancela a dele" etc.).
"""

from google.adk.tools.tool_context import ToolContext

from .. import db

CHAVE_APARTAMENTO = "apartamento"


def apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Lê o apartamento gravado na sessão e valida contra `dados/apartamentos.json`."""
    return db.validar_apartamento(tool_context.state.get(CHAVE_APARTAMENTO))


def chave_idempotencia(tool_context: ToolContext) -> str:
    """Uma chamada de tool = no máximo um efeito, mesmo se o ADK reexecutar.

    A chave junta o id da sessão com o id da chamada de função. Se a tool for
    reexecutada (retry/reprocessamento da confirmação), a mesma chave é
    reutilizada e o banco devolve o registro já gravado em vez de duplicar.
    """
    return f"{tool_context.session.id}:{tool_context.function_call_id}"


def confirmada_pelo_sistema(tool_context: ToolContext) -> bool:
    """True só quando o ADK entregou uma ToolConfirmation aprovada.

    A ToolConfirmation só existe quando a rota ``POST /confirmacoes`` injeta o
    FunctionResponse e o Runner retoma a tool. Nada que o morador escreva no
    chat ("já estou confirmando aqui") cria esse objeto.
    """
    confirmacao = tool_context.tool_confirmation
    return bool(confirmacao and confirmacao.confirmed)
