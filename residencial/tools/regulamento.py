"""Tools do especialista de regulamento (Garantia 4).

O regulamento nunca é carregado inteiro: ele é dividido por capítulo e o
especialista lê só o capítulo do assunto perguntado. Como esse especialista
roda como ``AgentTool`` (numa sessão própria e descartável), o texto do
capítulo nem entra na sessão do morador — lá ficam só a pergunta e a resposta
curta.
"""

import re
from functools import cache

from .. import config

_CAPITULO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)


@cache
def _capitulos() -> list[dict]:
    texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CAPITULO.finditer(texto))
    capitulos = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        capitulos.append(
            {"numero": i + 1, "romano": m.group(1), "titulo": m.group(2).strip(), "texto": texto[m.start():fim].strip()}
        )
    return capitulos


def listar_capitulos() -> dict:
    """Lista os capítulos do regulamento interno (número e título), sem o conteúdo."""
    return {
        "capitulos": [
            {"numero": c["numero"], "titulo": f"Capítulo {c['romano']}: {c['titulo']}"}
            for c in _capitulos()
        ]
    }


def ler_capitulo(numero: int) -> dict:
    """Devolve o texto de UM capítulo do regulamento interno.

    Args:
        numero: número do capítulo (1 a 14), conforme listar_capitulos.
    """
    for c in _capitulos():
        if c["numero"] == int(numero):
            return {"capitulo": c["texto"]}
    return {"erro": f"Capítulo {numero} não existe. Use listar_capitulos."}


TOOLS_REGULAMENTO = [listar_capitulos, ler_capitulo]
