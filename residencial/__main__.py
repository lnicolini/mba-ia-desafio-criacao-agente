"""Sobe a API em http://localhost:8000.

Uso: uv run python -m residencial
"""

import uvicorn


def main() -> None:
    uvicorn.run("residencial.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
