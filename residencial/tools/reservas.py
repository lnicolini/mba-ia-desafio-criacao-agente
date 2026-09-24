"""Tools do especialista de reservas.

Aqui está a parte de código da **Garantia 1** para reservas: a decisão de
pedir confirmação é do ``FunctionTool`` (pela taxa da área), nunca do modelo.
E a parte de código da **Garantia 2**: nenhuma tool recebe apartamento; ele
vem sempre do state da sessão.
"""

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .. import db
from .sessao import apartamento_da_sessao, chave_idempotencia, confirmada_pelo_sistema


def listar_areas() -> dict:
    """Lista as áreas comuns reserváveis, com id, nome e taxa em reais.

    Taxa 0 significa que a reserva não gera cobrança.
    """
    return {
        "areas": [
            {"id": a["id"], "nome": a["nome"], "taxa": a["taxa"], "gera_cobranca": a["taxa"] > 0}
            for a in db.areas().values()
        ]
    }


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre ou ocupada em uma data.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    try:
        a = db.resolver_area(area)
        d = db.validar_data(data)
    except db.RegraViolada as exc:
        return {"erro": str(exc)}
    ocupada = db.area_ocupada(a["id"], d)
    return {"area": a["id"], "data": d, "situacao": "ocupada" if ocupada else "livre"}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta conversa."""
    apto = apartamento_da_sessao(tool_context)
    return {"reservas": db.listar_reservas(apto)}


def _gera_cobranca(area: str, data: str, tool_context: ToolContext | None = None) -> bool:
    """Regra 2: pede confirmação quando a área tem taxa maior que zero.

    O ADK chama este callback com os mesmos argumentos da tool. A decisão
    depende só da taxa da área em `dados/areas.json`, nunca do modelo.
    """
    try:
        return db.resolver_area(area)["taxa"] > 0
    except db.RegraViolada:
        return False


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa maior que zero geram cobrança e só são gravadas depois que
    o morador aprovar a confirmação pelo aplicativo. O sistema pede essa
    aprovação sozinho; escrever no chat não conta.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.
    """
    try:
        a = db.resolver_area(area)
        d = db.validar_data(data)
    except db.RegraViolada as exc:
        return {"erro": str(exc)}

    # Defesa em profundidade da Garantia 1: além do `require_confirmation`, a
    # própria função recusa gravar uma reserva com cobrança sem aprovação.
    if a["taxa"] > 0 and not confirmada_pelo_sistema(tool_context):
        return {"erro": "Reserva com cobrança exige aprovação pelo aplicativo. Nada foi gravado."}

    apto = apartamento_da_sessao(tool_context)
    resultado = db.criar_reserva(apto, a["id"], d, chave_idempotencia(tool_context))
    if not resultado.criada:
        return {
            "status": "recusada",
            "motivo": f"A área {a['nome']} já está ocupada em {d}. Nenhuma reserva foi criada.",
        }
    cobranca = a["taxa"] if a["taxa"] > 0 else 0
    return {
        "status": "reservada",
        "codigo": resultado.codigo,
        "area": a["id"],
        "data": d,
        "cobranca": cobranca,
        "mensagem": (
            f"Reserva concluída e gravada. Cobrança de R$ {cobranca:.2f} aprovada."
            if cobranca
            else "Reserva concluída e gravada, sem cobrança."
        ),
    }


def cancelar_reserva(
    tool_context: ToolContext, codigo: str = "", area: str = "", data: str = ""
) -> dict:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Informe o código da reserva, ou a área e a data dela. Só reservas do
    próprio apartamento podem ser canceladas.

    Args:
        codigo: código da reserva (opcional se área e data forem informadas).
        area: id da área da reserva (opcional se o código for informado).
        data: data da reserva no formato AAAA-MM-DD (opcional se o código for informado).
    """
    apto = apartamento_da_sessao(tool_context)
    minhas = db.listar_reservas(apto)
    try:
        if codigo.strip():
            alvo = [r for r in minhas if r["codigo"].upper() == codigo.strip().upper()]
        else:
            a = db.resolver_area(area)
            d = db.validar_data(data)
            alvo = [r for r in minhas if r["area"] == a["id"] and r["data"] == d]
    except db.RegraViolada as exc:
        return {"erro": str(exc)}

    if not alvo:
        # Mesma resposta, exista ou não uma reserva de outro apartamento.
        return {"status": "nao_encontrada", "motivo": "Não há reserva ativa do seu apartamento com esses dados."}
    reserva = alvo[0]
    if not db.cancelar_reserva(apto, reserva["codigo"]):
        return {"status": "nao_encontrada", "motivo": "A reserva já não está ativa."}
    return {"status": "cancelada", **reserva}


TOOLS_RESERVAS = [
    listar_areas,
    consultar_disponibilidade,
    listar_minhas_reservas,
    FunctionTool(reservar_area, require_confirmation=_gera_cobranca),
    cancelar_reserva,
]
