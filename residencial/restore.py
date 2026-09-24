"""Restaura reservas e visitantes ao estado de `dados/` e apaga as sessões.

Uso: uv run python -m residencial.restore
"""

import sqlite3

from . import config, db


def apagar_sessoes() -> None:
    if not config.SESSOES_DB.exists():
        return
    conn = sqlite3.connect(config.SESSOES_DB, timeout=30)
    try:
        tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        with conn:
            for tabela in ("events", "sessions", "user_states", "app_states"):
                if tabela in tabelas:
                    conn.execute(f"DELETE FROM {tabela}")
    finally:
        conn.close()


def main() -> None:
    db.restaurar()
    apagar_sessoes()
    print("Dados restaurados a partir de dados/ e sessões apagadas.")
    for apto in db.apartamentos():
        print(f"  {apto}: reservas={db.listar_reservas(apto)} visitantes={db.listar_visitantes(apto)}")


if __name__ == "__main__":
    main()
