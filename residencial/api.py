"""API HTTP do assistente (FastAPI + Runner do ADK).

Rotas de conversa: /sessoes, /sessoes/{id}/mensagens, /sessoes/{id}/confirmacoes
e /sessoes/{id}/eventos. Rotas de verificação: /apartamentos/{n}/reservas e
/apartamentos/{n}/visitantes, que leem o banco direto, sem passar pelo modelo.

É aqui que mora a maior parte da **Garantia 1**: o pedido de confirmação do
ADK é um FunctionCall ``adk_request_confirmation`` gravado na sessão; ele deixa
de estar pendente quando a sessão recebe o FunctionResponse correspondente —
que só a rota ``/confirmacoes`` produz.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.runners import Runner
from google.adk.sessions.session import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import config, db
from .agents import app as adk_app
from .tools.sessao import CHAVE_APARTAMENTO

logger = logging.getLogger("residencial.api")

# Todas as sessões ficam sob o mesmo user_id; o apartamento de cada sessão é
# gravado no state dela (Garantia 2).
USER_ID = "morador"


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


class Estado:
    runner: Runner
    sessoes: SqliteSessionService
    # Uma conversa por vez em cada sessão (evita duas execuções concorrentes
    # gravando eventos na mesma sessão). Sessões diferentes correm em paralelo.
    travas: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


estado = Estado()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.inicializar()
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    # Garantia 3: sessões e eventos persistidos em SQLite (var/sessoes.db).
    estado.sessoes = SqliteSessionService(str(config.SESSOES_DB))
    estado.runner = Runner(app=adk_app, session_service=estado.sessoes)
    yield
    await estado.runner.close()


api = FastAPI(title="Residencial Aurora", lifespan=lifespan)
app = api  # uvicorn residencial.api:app


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _sessao_ou_404(session_id: str) -> Session:
    sessao = await estado.sessoes.get_session(
        app_name=config.APP_NAME, user_id=USER_ID, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return sessao


def confirmacoes_pendentes(sessao: Session) -> list[dict[str, Any]]:
    """Garantia 1: pedidos de confirmação do ADK ainda sem resposta.

    Um pedido é um FunctionCall ``adk_request_confirmation`` na sessão. Ele
    deixa de estar pendente quando há um FunctionResponse com o mesmo id — o
    que só a rota /confirmacoes produz. Por isso a leitura sobrevive a reinício.
    """
    pedidos: dict[str, dict[str, Any]] = {}
    respondidos: set[str] = set()
    for evento in sessao.events:
        for chamada in evento.get_function_calls():
            if chamada.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and chamada.id:
                original = (chamada.args or {}).get("originalFunctionCall") or {}
                pedidos[chamada.id] = {
                    "id": chamada.id,
                    "acao": original.get("name", ""),
                    "detalhes": _detalhes(original.get("name", ""), original.get("args") or {}),
                }
        for resposta in evento.get_function_responses():
            if resposta.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and resposta.id:
                respondidos.add(resposta.id)
    return [p for pid, p in pedidos.items() if pid not in respondidos]


def _detalhes(acao: str, args: dict[str, Any]) -> dict[str, Any]:
    detalhes = dict(args)
    if acao == "reservar_area":
        try:
            area = db.resolver_area(str(args.get("area", "")))
            detalhes["area"] = area["id"]
            detalhes["taxa"] = area["taxa"]
        except db.RegraViolada:
            pass
    return detalhes


def _texto_resposta(eventos: list) -> str:
    partes: list[str] = []
    for evento in eventos:
        if evento.author == "user" or not evento.content or not evento.content.parts:
            continue
        for parte in evento.content.parts:
            if parte.text and not parte.thought:
                partes.append(parte.text.strip())
    return "\n\n".join(p for p in partes if p)


async def _executar(session_id: str, mensagem: types.Content) -> dict[str, Any]:
    eventos = []
    async for evento in estado.runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=mensagem
    ):
        eventos.append(evento)
    sessao = await _sessao_ou_404(session_id)
    return {
        "resposta": _texto_resposta(eventos),
        "confirmacoes_pendentes": confirmacoes_pendentes(sessao),
    }


# --------------------------------------------------------------------------
# Rotas de conversa
# --------------------------------------------------------------------------


@api.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao) -> dict[str, str]:
    try:
        apartamento = db.validar_apartamento(corpo.apartamento)
    except db.RegraViolada as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Garantia 2: o apartamento é gravado UMA vez, aqui, no state da sessão.
    sessao = await estado.sessoes.create_session(
        app_name=config.APP_NAME, user_id=USER_ID, state={CHAVE_APARTAMENTO: apartamento}
    )
    return {"session_id": sessao.id}


@api.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem) -> dict[str, Any]:
    await _sessao_ou_404(session_id)
    async with estado.travas[session_id]:
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, mensagem)


@api.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao) -> dict[str, Any]:
    await _sessao_ou_404(session_id)
    async with estado.travas[session_id]:
        sessao = await _sessao_ou_404(session_id)
        pendentes = {p["id"] for p in confirmacoes_pendentes(sessao)}
        if corpo.id not in pendentes:
            # Id inexistente, de outra sessão ou já respondido: nada executa.
            raise HTTPException(
                status_code=409, detail="Não existe confirmação pendente com esse id nesta sessão."
            )
        # A resposta vira o FunctionResponse que o ADK espera para retomar a
        # tool que pediu confirmação (ToolConfirmation.from_response_dict).
        mensagem = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id,
                        name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        return await _executar(session_id, mensagem)


@api.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str) -> list[dict[str, Any]]:
    sessao = await _sessao_ou_404(session_id)
    return [e.model_dump(mode="json", exclude_none=True, by_alias=True) for e in sessao.events]


# --------------------------------------------------------------------------
# Rotas de verificação (leem o banco direto, sem modelo)
# --------------------------------------------------------------------------


@api.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return db.listar_reservas(numero)


@api.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str) -> list[dict[str, Any]]:
    return db.listar_visitantes(numero)
