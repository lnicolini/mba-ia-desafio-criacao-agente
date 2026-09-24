"""Configuração central: caminhos, banco, modelos e nome do app."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# `dados/` é o estado inicial (somente leitura). Tudo o que o assistente grava
# vai para `var/`, que fica fora do Git.
DADOS_DIR = RAIZ / "dados"
VAR_DIR = Path(os.getenv("AURORA_VAR_DIR", RAIZ / "var"))

# Dois bancos SQLite: o do condomínio (reservas/visitantes/códigos) e o das
# sessões do ADK (eventos e state), persistido pelo SqliteSessionService.
CONDOMINIO_DB = VAR_DIR / "condominio.db"
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"

# Modelos Gemini. O padrão é configurável via .env; a ordem de grandeza do
# fluxo do avaliador é de dezenas de chamadas, então um flash barato é ideal.
# `or` (e não o default de os.getenv) porque o .env.example deixa as variáveis
# vazias, e uma variável presente porém vazia precisa cair no padrão.
MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.6-flash"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or MODELO_PRINCIPAL
