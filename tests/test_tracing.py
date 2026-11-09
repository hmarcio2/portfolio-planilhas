"""Tracing do agente de planilhas: estrutura do trace, privacidade dos metadados e notas automáticas."""

import json

import pytest
from fastapi.testclient import TestClient

from app.guardrails import evaluate_turn
from app.main import create_app
from portfolio_core import LLMChunk, LLMError, Settings, Span, ToolUse, Tracer, Usage

from conftest import CSV


# ---------- notas ----------

def call(name="run_sql", is_error=False):
    return {"name": name, "is_error": is_error}


def scores(**kw):
    base = dict(answer="São 5 linhas.", calls=[call()], unverified=[])
    return {s.name: s for s in evaluate_turn(**{**base, **kw})}


def test_clean_turn_has_no_alerts():
    assert {n: s.value for n, s in scores().items()} == {"unverified_numbers": 0, "tool_errors": 0, "no_query": 0}


def test_unverified_numbers_are_counted_and_listed():
    s = scores(unverified=["96.069,57"])
    assert s["unverified_numbers"].value == 1 and "96.069,57" in s["unverified_numbers"].comment
    assert s["unverified_numbers"].data_type == "NUMERIC"


def test_failed_tool_calls_are_counted_even_if_the_model_recovered():
    s = scores(calls=[call(is_error=True), call()])
    assert s["tool_errors"].value == 1 and s["no_query"].value == 0 and "1 de 2" in s["tool_errors"].comment


def test_answer_without_a_successful_query_is_flagged():
    assert scores(calls=[])["no_query"].value == 1
    assert scores(calls=[call(is_error=True)])["no_query"].value == 1
    assert scores(calls=[call("describe_table")])["no_query"].value == 1


def test_failed_or_empty_turn_has_no_scores():
    assert evaluate_turn(answer="", calls=[], unverified=[]) == []
    assert evaluate_turn(answer="x", calls=[], unverified=[], failed=True) == []


# ---------- trace no fluxo da API ----------

class Rec(Span):
    def __init__(self, name="root", kind="span", **kw):
        self.name, self.kind, self.kw, self.updates, self.children, self.scored, self.ended = name, kind, kw, [], [], {}, False

    def child(self, name, *, as_type="span", **kw):
        c = Rec(name, as_type, **kw)
        self.children.append(c)
        return c

    def update(self, **kw):
        self.updates.append({k: v for k, v in kw.items() if v is not None})

    def end(self):
        self.ended = True

    def score(self, name, value, *, data_type="NUMERIC", comment=None):
        self.scored[name] = (value, comment)

    @property
    def trace_id(self):
        return "b" * 32

    @property
    def merged(self):
        out = {}
        for u in self.updates:
            out.update(u)
        return out


class RecTracer(Tracer):
    enabled = True

    def __init__(self):
        self.roots, self.shut = [], False

    def start(self, name, *, session_id=None, input=None, metadata=None, tags=None):
        root = Rec(name, session_id=session_id, input=input, metadata=metadata)
        self.roots.append(root)
        return root

    def shutdown(self):
        self.shut = True


class Scripted:
    def __init__(self, *turns):
        self.turns = list(turns)

    async def stream(self, *, system, messages, tools=None):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for c in turn:
            yield c


def tool_turn(*uses):
    return [LLMChunk(done=True, stop_reason="tool_use", tool_uses=list(uses), content=[{"type": "tool_use"}], usage=Usage(10, 5))]


def text_turn(text):
    return [LLMChunk(text=text), LLMChunk(done=True, stop_reason="end_turn", usage=Usage(20, 8))]


def ask(store, provider, message="quantas linhas?", filename="vendas 2026.csv"):
    tracer = RecTracer()
    with TestClient(create_app(Settings(provider="echo", model="m-test"), provider, store, tracer)) as client:
        sid = client.post("/upload", files={"file": (filename, CSV, "text/csv")}).json()["session_id"]
        text = client.post("/chat", json={"session_id": sid, "message": message}).text
    return tracer, text


def test_turn_trace_has_generations_sql_tool_and_scores(store):
    provider = Scripted(tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT count(*) AS n FROM vendas_2026"})),
                        text_turn("São 5 linhas."))
    tracer, text = ask(store, provider)
    (root,) = tracer.roots
    assert root.name == "sheet-turn" and root.kw["input"] == "quantas linhas?" and root.kw["session_id"]
    assert [(c.name, c.kind) for c in root.children] == [("generation", "generation"), ("run_sql", "tool"),
                                                         ("generation", "generation")]
    sql = root.children[1]
    assert sql.kw["input"] == {"query": "SELECT count(*) AS n FROM vendas_2026"} and sql.merged["metadata"]["sql"]
    assert '"n": 5' in sql.merged["output"] or "5" in sql.merged["output"]
    assert root.merged["output"] == "São 5 linhas." and root.merged["metadata"]["queries"] == 1
    assert root.scored["no_query"][0] == 0 and root.scored["unverified_numbers"][0] == 0
    assert root.ended and all(c.ended for c in root.children)
    assert '"trace_id": "' + "b" * 32 in text


def test_trace_metadata_does_not_include_the_file_name(store):
    provider = Scripted(text_turn("ok"))
    tracer, _ = ask(store, provider, filename="planilha-do-joao-silva.csv")
    root = tracer.roots[0]
    assert root.kw["metadata"]["tables"] == 1 and root.kw["metadata"]["rows"] == 5
    assert "joao" not in json.dumps(root.kw["metadata"], default=str).lower()


def test_invented_number_becomes_a_score(store):
    provider = Scripted(tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT count(*) AS n FROM vendas_2026"})),
                        text_turn("A receita foi de R$ 96.069,57."))
    tracer, text = ask(store, provider)
    value, comment = tracer.roots[0].scored["unverified_numbers"]
    assert value == 1 and "96.069,57" in comment and "event: warning" in text


def test_sql_error_is_visible_on_the_tool_observation_and_in_the_score(store):
    provider = Scripted(tool_turn(ToolUse("t1", "run_sql", {"query": "DROP TABLE vendas_2026"})), text_turn("Não consegui."))
    tracer, _ = ask(store, provider)
    root = tracer.roots[0]
    assert root.children[1].merged["level"] == "ERROR"
    assert root.scored["tool_errors"][0] == 1 and root.scored["no_query"][0] == 1


def test_provider_failure_marks_the_trace_as_error_without_scores(store):
    tracer, text = ask(store, Scripted(LLMError("fora do ar")))
    root = tracer.roots[0]
    assert "event: error" in text and root.merged["level"] == "ERROR" and root.scored == {} and root.ended
    assert root.children[0].merged["level"] == "ERROR"


def test_tracer_is_shut_down_with_the_app(store):
    tracer, _ = ask(store, Scripted(text_turn("ok")))
    assert tracer.shut


def test_default_tracer_is_a_noop_without_keys(store, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    with TestClient(create_app(Settings(provider="echo"), Scripted(text_turn("ok")), store)) as client:
        sid = client.post("/upload", files={"file": ("a.csv", CSV, "text/csv")}).json()["session_id"]
        text = client.post("/chat", json={"session_id": sid, "message": "oi"}).text
    assert "trace_id" not in text and "event: done" in text
