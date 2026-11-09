"""API do agente analista de planilhas: upload, chat com streaming (SSE) e relatório."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from portfolio_core import (
    DailyBudget,
    LLMError,
    LLMProvider,
    RateLimiter,
    SessionStore,
    Settings,
    Tracer,
    Usage,
    get_provider,
    get_tracer,
    sse_event,
)
from portfolio_core.tracing import log_llm_call

from .agent import run_agent
from .data import DatasetStore, Limits, StoreFull, UploadError
from .guardrails import evaluate_turn
from .report import build_report

logger = logging.getLogger("portfolio.sheets")

APP_NAME = "02-agente-planilhas"
BASE_DIR = Path(__file__).resolve().parent.parent


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


def _limits_from_env() -> Limits:
    env = os.environ
    return Limits(
        max_bytes=int(float(env.get("PORTFOLIO_SHEETS_MAX_MB", 5)) * 1024 * 1024),
        max_rows=int(env.get("PORTFOLIO_SHEETS_MAX_ROWS", 200_000)),
        max_columns=int(env.get("PORTFOLIO_SHEETS_MAX_COLUMNS", 100)),
        max_sheets=int(env.get("PORTFOLIO_SHEETS_MAX_SHEETS", 5)),
        ttl_seconds=float(env.get("PORTFOLIO_SHEETS_TTL_SECONDS", 1800)),
        max_sessions=int(env.get("PORTFOLIO_SHEETS_MAX_SESSIONS", 30)),
    )


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    store: DatasetStore | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    base = (settings or Settings.from_env()).resolved()
    # Análise com ferramentas pede mais raciocínio e saída do que o chat simples.
    settings = dataclasses.replace(
        base,
        effort=os.environ.get("PORTFOLIO_SHEETS_EFFORT", "medium"),
        max_tokens=int(os.environ.get("PORTFOLIO_SHEETS_MAX_TOKENS", 4000)),
    )
    provider = provider or get_provider(settings)
    tracer = tracer or get_tracer()  # Langfuse se houver LANGFUSE_PUBLIC_KEY/SECRET_KEY; senão, no-op
    store = store or DatasetStore(_limits_from_env())
    chat_limiter = RateLimiter(settings.rate_limit_per_minute)
    upload_limiter = RateLimiter(5)
    budget = DailyBudget(settings.daily_message_cap)
    sessions = SessionStore(ttl=settings.session_ttl_seconds, max_messages=settings.max_history_messages)

    async def janitor():
        while True:
            await asyncio.sleep(60)
            store.cleanup()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(janitor())
        yield
        task.cancel()
        store.close_all()
        await asyncio.to_thread(tracer.shutdown)  # envia os traces pendentes antes de encerrar

    app = FastAPI(title="Agente analista de planilhas", docs_url=None, redoc_url=None, lifespan=lifespan)

    def client_key(request: Request) -> str:
        if settings.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.get("/health")
    def health():
        return {"status": "ok", "sessions": len(store), "provider": type(provider).__name__}

    @app.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...)):
        allowed, retry_after = upload_limiter.check(client_key(request))
        if not allowed:
            return JSONResponse({"error": "Muitos envios em pouco tempo. Aguarde um instante."}, status_code=429,
                                headers={"Retry-After": str(int(retry_after) + 1)})
        limit = store.limits.max_bytes
        data = await file.read(limit + 1)
        try:
            dataset = await asyncio.to_thread(store.create, file.filename or "planilha", data)
        except UploadError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except StoreFull as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return {
            "session_id": dataset.session_id,
            "filename": dataset.filename,
            "tables": [t.as_dict() for t in dataset.tables],
            "ttl_minutes": int(store.limits.ttl_seconds // 60),
        }

    @app.post("/chat")
    async def chat(body: ChatRequest, request: Request):
        dataset = store.get(body.session_id)
        if dataset is None:
            return JSONResponse({"error": "Sessão não encontrada ou expirada. Envie a planilha novamente."},
                                status_code=404)
        message = body.message.strip()
        if not message or len(message) > settings.max_message_chars:
            return JSONResponse({"error": f"A pergunta deve ter entre 1 e {settings.max_message_chars} caracteres."},
                                status_code=422)
        allowed, retry_after = chat_limiter.check(client_key(request))
        if not allowed:
            return JSONResponse({"error": "Muitas mensagens em pouco tempo. Aguarde um instante."}, status_code=429,
                                headers={"Retry-After": str(int(retry_after) + 1)})
        if not budget.try_consume():
            return JSONResponse({"error": "O limite diário de análises desta demo foi atingido. Volte amanhã!"},
                                status_code=429)

        sid = body.session_id
        sessions.append(sid, "user", message)
        history = sessions.history(sid)

        # Um trace por pergunta, agrupado por sessão. Sem nome do arquivo (pode ter dado pessoal): só o formato da planilha.
        trace = tracer.start(
            "sheet-turn", session_id=sid, input=message,
            metadata={"app": APP_NAME, "provider": settings.provider, "model": settings.model,
                      "tables": len(dataset.tables), "rows": sum(t.rows for t in dataset.tables),
                      "turn": sum(m["role"] == "user" for m in history)},
        )

        async def events():
            meta = {"session_id": sid}
            if trace.trace_id:
                meta["trace_id"] = trace.trace_id
            yield sse_event("meta", meta)
            started = time.monotonic()
            usage, stop_reason = None, None
            answer, calls, unverified, queries = "", [], [], []
            outcome = {"level": None, "status": None}
            try:
                async for ev in run_agent(provider, dataset, history, trace=trace, model=settings.model or None,
                                          model_parameters={"temperature": settings.temperature, "max_tokens": settings.max_tokens}):
                    if ev.kind == "done":
                        usage, stop_reason = ev.data["usage"], ev.data["stop_reason"]
                        answer, calls, unverified, queries = ev.data["answer"], ev.data["calls"], ev.data["unverified"], ev.data["queries"]
                        sessions.append(sid, "assistant", ev.data["answer"])
                        dataset.report.append({"question": message, "answer": ev.data["answer"],
                                               "queries": ev.data["queries"], "charts": ev.data["charts"],
                                               "unverified": ev.data["unverified"]})
                        yield sse_event("done", {"queries": len(ev.data["queries"])})
                    elif ev.kind == "error":
                        sessions.drop_last(sid)
                        outcome.update(level="ERROR", status=str(ev.data.get("error"))[:300])
                        yield sse_event("error", ev.data)
                    else:
                        yield sse_event(ev.kind, ev.data)
            except LLMError as exc:
                logger.exception("Falha na chamada ao LLM")
                sessions.drop_last(sid)
                log_llm_call(app=APP_NAME, model=settings.model, latency_s=time.monotonic() - started,
                             usage=usage or Usage(), stop_reason=None, error=type(exc).__name__)
                outcome.update(level="ERROR", status=f"{type(exc).__name__}: {exc}"[:300])
                yield sse_event("error", {"error": "Não consegui analisar agora. Tente novamente em instantes."})
                return
            finally:
                # Também roda se o visitante fechar a página no meio (o gerador é cancelado).
                latency = round(time.monotonic() - started, 3)
                trace.update(output=answer or None, level=outcome["level"], status_message=outcome["status"],
                             metadata={"queries": len(queries), "tool_calls": len(calls), "stop_reason": stop_reason,
                                       "latency_s": latency})
                for score in evaluate_turn(answer=answer, calls=calls, unverified=unverified,
                                           failed=outcome["level"] == "ERROR"):
                    trace.score(score.name, score.value, data_type=score.data_type, comment=score.comment)
                trace.end()
            if usage is not None:
                log_llm_call(app=APP_NAME, model=settings.model, latency_s=time.monotonic() - started,
                             usage=usage, stop_reason=stop_reason)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/sessions/{session_id}/report.md")
    def report(session_id: str):
        dataset = store.get(session_id)
        if dataset is None:
            return JSONResponse({"error": "Sessão não encontrada ou expirada."}, status_code=404)
        return PlainTextResponse(build_report(dataset), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="analise.md"'})

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str):
        deleted = store.delete(session_id)
        return JSONResponse({"deleted": deleted}, status_code=200 if deleted else 404)

    app.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
    return app
