"""Armazenamento do condomínio em SQLite.

Os arquivos de `dados/` são o estado inicial e nunca são alterados: são
copiados para `var/condominio.db` na primeira subida e no comando de
restauração. Tudo o que o assistente grava vai para esse banco.

Garantias que vivem aqui:

- **Garantia 5 (dois moradores, uma reserva)**: o índice único parcial
  ``uq_reserva_ativa`` proíbe duas reservas *ativas* para a mesma área e data
  no instante do INSERT. Quem perder a corrida recebe ``IntegrityError``, que
  a tool converte numa resposta normal de "data indisponível".

- **Código único (regra 5)**: todo código já emitido — inclusive de reservas
  canceladas ou removidas pela restauração — fica em ``codigos_emitidos``
  (PRIMARY KEY). Um código novo nunca repete outro.

- **Idempotência (apoio à Garantia 1)**: ``chave_idempotencia UNIQUE`` garante
  que uma mesma chamada de tool (session_id + function_call_id) tenha no
  máximo um efeito, mesmo que o ADK reexecute a tool retomada.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import string
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import cache
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo            TEXT PRIMARY KEY,
    apartamento       TEXT NOT NULL,
    area              TEXT NOT NULL,
    data              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ativa'
                      CHECK (status IN ('ativa', 'cancelada')),
    chave_idempotencia TEXT UNIQUE,
    criada_em         TEXT NOT NULL DEFAULT (datetime('now')),
    cancelada_em      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_reserva_ativa
    ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento       TEXT NOT NULL,
    nome              TEXT NOT NULL,
    data              TEXT NOT NULL,
    chave_idempotencia TEXT UNIQUE,
    criado_em         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Impede a mesma autorização (apartamento, nome, data) de ser gravada duas
-- vezes, mesmo que o ADK reexecute a tool por causa da mensagem duplicada.
CREATE UNIQUE INDEX IF NOT EXISTS uq_visitante
    ON visitantes (apartamento, nome, data);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


class RegraViolada(ValueError):
    """Entrada que desrespeita uma regra do condomínio (área, data, nome...)."""


# --------------------------------------------------------------------------
# Dados estáticos (somente leitura)
# --------------------------------------------------------------------------


def _ler_json(nome: str) -> list[dict]:
    with open(config.DADOS_DIR / nome, encoding="utf-8") as f:
        return json.load(f)


@cache
def apartamentos() -> dict[str, dict]:
    return {a["numero"]: a for a in _ler_json("apartamentos.json")}


@cache
def areas() -> dict[str, dict]:
    return {a["id"]: a for a in _ler_json("areas.json")}


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore")
    return "-".join(sem_acento.decode().lower().replace("_", " ").split())


def resolver_area(area: str) -> dict:
    """Aceita o id ("salao-de-festas") ou o nome ("Salão de festas")."""
    alvo = _normalizar(area or "")
    for a in areas().values():
        if alvo in (a["id"], _normalizar(a["nome"])):
            return a
    for a in areas().values():
        if alvo and (a["id"].startswith(alvo) or _normalizar(a["nome"]).startswith(alvo)):
            return a
    validas = ", ".join(areas())
    raise RegraViolada(f"Área desconhecida: {area!r}. Áreas válidas: {validas}.")


def validar_data(valor: str) -> str:
    try:
        return date.fromisoformat((valor or "").strip()).isoformat()
    except ValueError as exc:
        raise RegraViolada(f"Data inválida: {valor!r}. Use o formato AAAA-MM-DD.") from exc


def validar_apartamento(numero: str) -> str:
    numero = str(numero or "").strip()
    if numero not in apartamentos():
        raise RegraViolada(f"Apartamento inexistente: {numero!r}.")
    return numero


# --------------------------------------------------------------------------
# Conexão, esquema e restauração
# --------------------------------------------------------------------------


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.CONDOMINIO_DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


def _carregar_estado_inicial(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM reservas")
    conn.execute("DELETE FROM visitantes")
    for r in _ler_json("reservas.json"):
        conn.execute(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            (r["codigo"], r["apartamento"], r["area"], r["data"]),
        )
        conn.execute("INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)", (r["codigo"],))
    for v in _ler_json("visitantes.json"):
        conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (v["apartamento"], v["nome"], v["data"]),
        )
    conn.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES ('inicializado', '1')")


def inicializar() -> None:
    """Cria o esquema e, só na primeira vez, carrega o estado de `dados/`."""
    with conectar() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            ja = conn.execute("SELECT 1 FROM meta WHERE chave = 'inicializado'").fetchone()
            if not ja:
                _carregar_estado_inicial(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def restaurar() -> None:
    """Volta reservas e visitantes ao estado dos arquivos de `dados/`.

    `codigos_emitidos` é preservado de propósito: um código emitido antes da
    restauração continua sem poder ser reutilizado.
    """
    with conectar() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _carregar_estado_inicial(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


def listar_reservas(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def area_ocupada(area: str, data: str) -> bool:
    """Só diz SE a data está ocupada; nunca de quem é a reserva (Garantia 2)."""
    with conectar() as conn:
        row = conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area, data),
        ).fetchone()
    return row is not None


@dataclass
class ResultadoReserva:
    criada: bool
    codigo: str | None = None
    motivo: str | None = None


_ALFABETO = string.ascii_uppercase + string.digits


def _novo_codigo() -> str:
    return "RSV-" + "".join(secrets.choice(_ALFABETO) for _ in range(6))


def criar_reserva(
    apartamento: str,
    area: str,
    data: str,
    chave_idempotencia: str | None,
) -> ResultadoReserva:
    """Grava uma reserva ativa, garantindo exclusividade no instante do INSERT."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if chave_idempotencia:
                existente = conn.execute(
                    "SELECT codigo FROM reservas WHERE chave_idempotencia = ?",
                    (chave_idempotencia,),
                ).fetchone()
                if existente:
                    conn.execute("COMMIT")
                    return ResultadoReserva(criada=True, codigo=existente["codigo"])

            while True:
                codigo = _novo_codigo()
                try:
                    conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
                    break
                except sqlite3.IntegrityError:
                    continue

            try:
                conn.execute(
                    "INSERT INTO reservas (codigo, apartamento, area, data, chave_idempotencia)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (codigo, apartamento, area, data, chave_idempotencia),
                )
            except sqlite3.IntegrityError:
                # uq_reserva_ativa: outra reserva ativa já ocupa a área nessa data.
                conn.execute("ROLLBACK")
                return ResultadoReserva(criada=False, motivo="indisponivel")

            conn.execute("COMMIT")
            return ResultadoReserva(criada=True, codigo=codigo)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def cancelar_reserva(apartamento: str, codigo: str) -> bool:
    """Cancela uma reserva ativa DO PRÓPRIO apartamento. Retorna se cancelou."""
    with conectar() as conn:
        cur = conn.execute(
            "UPDATE reservas SET status = 'cancelada', cancelada_em = datetime('now')"
            " WHERE codigo = ? AND apartamento = ? AND status = 'ativa'",
            (codigo, apartamento),
        )
    return cur.rowcount == 1


# --------------------------------------------------------------------------
# Visitantes
# --------------------------------------------------------------------------


def listar_visitantes(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def autorizar_visitante(
    apartamento: str,
    nome: str,
    data: str,
    chave_idempotencia: str | None,
) -> dict:
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if chave_idempotencia:
                existente = conn.execute(
                    "SELECT nome, data FROM visitantes WHERE chave_idempotencia = ?",
                    (chave_idempotencia,),
                ).fetchone()
                if existente:
                    conn.execute("COMMIT")
                    return dict(existente)
            # Idempotência por (apartamento, nome, data): a mesma visita já
            # autorizada não é gravada de novo (cobre o caso de a tool rodar
            # duas vezes por causa da mensagem duplicada do Runner).
            duplicada = conn.execute(
                "SELECT nome, data FROM visitantes WHERE apartamento = ? AND nome = ? AND data = ?",
                (apartamento, nome, data),
            ).fetchone()
            if duplicada:
                conn.execute("COMMIT")
                return dict(duplicada)
            conn.execute(
                "INSERT INTO visitantes (apartamento, nome, data, chave_idempotencia)"
                " VALUES (?, ?, ?, ?)",
                (apartamento, nome, data, chave_idempotencia),
            )
            conn.execute("COMMIT")
            return {"nome": nome, "data": data}
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
