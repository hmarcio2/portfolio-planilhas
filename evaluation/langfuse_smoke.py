"""Teste de fumaça do Langfuse para o analista de planilhas: manda UM trace de teste pelo pipeline real (upload de uma
planilha de exemplo, uma consulta SQL de verdade no DuckDB, guardrails) e confere se o trace, a ferramenta e as notas
chegaram ao servidor.

Uso (na pasta 02-agente-planilhas), com as chaves do projeto Langfuse DESTE app e o servidor no ar:

    $env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
    $env:LANGFUSE_SECRET_KEY = "sk-lf-..."
    $env:LANGFUSE_BASE_URL   = "http://localhost:3300"
    ../.venv/Scripts/python -m evaluation.langfuse_smoke

Não chama nenhum LLM (provedor roteirizado). A planilha é sintética. O trace vai para o ambiente ``portfolio-smoke``.
"""

from __future__ import annotations

import os
import sys
import time

from fastapi.testclient import TestClient

from app.data import DatasetStore, Limits
from app.main import create_app
from portfolio_core import LLMChunk, Settings, ToolUse, Usage, get_tracer
from portfolio_core.config import load_dotenv_if_present

CSV = "Região,Produto,Vendas\nNorte,Café,100\nSul,Café,200\nNorte,Chá,50\n".encode("utf-8")
EXPECTED = {"sheet-turn", "generation", "run_sql"}
EXPECTED_SCORES = {"unverified_numbers", "tool_errors", "no_query"}


class ScriptedProvider:
    def __init__(self):
        self.turns = [
            [LLMChunk(done=True, stop_reason="tool_use", content=[{"type": "tool_use"}], usage=Usage(300, 25),
                      tool_uses=[ToolUse("t1", "run_sql", {"query": "SELECT count(*) AS n FROM vendas"})])],
            [LLMChunk(text="Teste de fumaça: "), LLMChunk(text="a planilha tem 3 linhas."),
             LLMChunk(done=True, stop_reason="end_turn", usage=Usage(500, 10))],
        ]

    async def stream(self, *, system, messages, tools=None):
        for chunk in self.turns.pop(0):
            yield chunk


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv_if_present()
    os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "portfolio-smoke")
    tracer = get_tracer()
    if not tracer.enabled:
        print("Langfuse desligado: defina LANGFUSE_PUBLIC_KEY e LANGFUSE_SECRET_KEY (e LANGFUSE_BASE_URL).", file=sys.stderr)
        return 2
    client = tracer._client  # noqa: SLF001
    host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
    try:
        ok = client.auth_check()
    except Exception as exc:
        print(f"Não consegui falar com o Langfuse em {host}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    if not ok:
        print(f"O Langfuse em {host} recusou as chaves (auth_check falhou).", file=sys.stderr)
        return 3
    print(f"Conectado ao Langfuse em {host} (ambiente: {os.environ['LANGFUSE_TRACING_ENVIRONMENT']}).")

    store = DatasetStore(Limits(max_bytes=200_000, ttl_seconds=120))
    settings = Settings(provider="echo", model="smoke-test-model", rate_limit_per_minute=100)
    try:
        with TestClient(create_app(settings, ScriptedProvider(), store, tracer)) as app_client:
            session_id = app_client.post("/upload", files={"file": ("vendas.csv", CSV, "text/csv")}).json()["session_id"]
            reply = app_client.post("/chat", json={"session_id": session_id, "message": "Teste de fumaça: quantas linhas?"})
            trace_id = reply.text.split('"trace_id": "')[1].split('"')[0]
    finally:
        store.close_all()
    tracer.flush()
    print(f"Trace enviado (sessão {session_id}). Esperando o servidor processar...")

    deadline, seen = time.monotonic() + 60, {}
    while time.monotonic() < deadline and not EXPECTED <= set(seen):
        try:
            page = client.api.observations.get_many(session_id=session_id, limit=20, fields="core,basic,usage,model")
            seen = {o.name: o for o in page.data}
        except Exception as exc:
            print(f"  (leitura ainda falhou: {type(exc).__name__}; tentando de novo)")
        if not EXPECTED <= set(seen):
            time.sleep(3)
    if not EXPECTED <= set(seen):
        print(f"FALHA: faltam as observações {sorted(EXPECTED - set(seen))}. Chegaram: {', '.join(sorted(seen)) or 'nenhuma'}",
              file=sys.stderr)
        return 1
    print("OK: chegaram as observações (sheet-turn, 2 generations, run_sql).")

    deadline, found = time.monotonic() + 60, set()
    while time.monotonic() < deadline and not EXPECTED_SCORES <= found:
        try:
            found = {s.name for s in client.api.scores.get_many(trace_id=trace_id, limit=50).data}
        except Exception:
            pass
        if not EXPECTED_SCORES <= found:
            time.sleep(3)
    if EXPECTED_SCORES <= found:
        print(f"OK: chegaram as {len(EXPECTED_SCORES)} notas.")
    else:
        print(f"AVISO: ainda não consigo ler as notas {sorted(EXPECTED_SCORES - found)} (foram enviadas; confira na interface).")
    try:
        print("  veja em:", client.get_trace_url(trace_id=trace_id))
    except Exception:
        pass
    tracer.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
