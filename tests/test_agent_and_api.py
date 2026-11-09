import json

import pytest
from fastapi.testclient import TestClient

from app.agent import run_agent
from app.main import create_app
from app.tools import execute_tool
from portfolio_core import LLMChunk, Settings, ToolUse, Usage

from conftest import CSV


class ScriptedProvider:
    """Devolve, por chamada, a lista de chunks combinada de antemão; guarda o que recebeu."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    async def stream(self, *, system, messages, tools=None):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        for chunk in self.turns.pop(0):
            yield chunk


def tool_turn(*uses, text=""):
    chunks = [LLMChunk(text=text)] if text else []
    chunks.append(LLMChunk(done=True, stop_reason="tool_use", tool_uses=list(uses), content=[{"type": "tool_use"}],
                           usage=Usage(10, 5)))
    return chunks


def text_turn(text):
    return [LLMChunk(text=text), LLMChunk(done=True, stop_reason="end_turn", usage=Usage(20, 8))]


async def collect(agen):
    return [ev async for ev in agen]


# ---------- tools ----------

def test_run_sql_tool_returns_json_and_stores_result(dataset):
    out = execute_tool(dataset, "run_sql", {"query": "SELECT produto, sum(vendas) v FROM vendas_2026 GROUP BY 1 ORDER BY 1"})
    assert not out.is_error
    payload = json.loads(out.content)
    assert payload["colunas"] == ["produto", "v"] and payload["linhas"][0][0] == "Café"
    assert dataset.last_result is not None


def test_run_sql_tool_error_is_not_raised(dataset):
    out = execute_tool(dataset, "run_sql", {"query": "DROP TABLE vendas_2026"})
    assert out.is_error and "SELECT" in out.content


def test_tool_rejects_bad_arguments(dataset):
    assert execute_tool(dataset, "run_sql", {"query": 5}).is_error
    assert execute_tool(dataset, "run_sql", "texto").is_error
    assert execute_tool(dataset, "nao_existe", {}).is_error


def test_describe_table_profile(dataset):
    out = execute_tool(dataset, "describe_table", {"table": "vendas_2026"})
    profile = json.loads(out.content)
    vendas = next(c for c in profile["colunas"] if c["coluna"] == "vendas")
    assert vendas["nulos"] == 1
    produto = next(c for c in profile["colunas"] if c["coluna"] == "produto")
    assert {v for v, _ in produto["mais_frequentes"]} == {"Café", "Chá"}
    assert execute_tool(dataset, "describe_table", {"table": "outra"}).is_error


def test_make_chart_requires_prior_query_and_valid_columns(dataset):
    assert execute_tool(dataset, "make_chart", {"kind": "bar", "x": "a", "y": ["b"], "title": "t"}).is_error
    execute_tool(dataset, "run_sql", {"query": "SELECT regiao, sum(vendas) AS total FROM vendas_2026 GROUP BY 1"})
    bad = execute_tool(dataset, "make_chart", {"kind": "bar", "x": "regiao", "y": ["nada"], "title": "t"})
    assert bad.is_error
    not_numeric = execute_tool(dataset, "make_chart", {"kind": "bar", "x": "total", "y": ["regiao"], "title": "t"})
    assert not_numeric.is_error
    ok = execute_tool(dataset, "make_chart", {"kind": "bar", "x": "regiao", "y": ["total"], "title": "Vendas"})
    assert not ok.is_error and sorted(ok.chart["labels"]) == ["Norte", "Sul"]
    assert ok.chart["datasets"][0]["label"] == "total"


# ---------- agente ----------

@pytest.mark.asyncio
async def test_agent_loop_runs_tool_and_feeds_result_back(dataset):
    provider = ScriptedProvider(
        tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT count(*) AS n FROM vendas_2026"}), text="Vou contar."),
        text_turn("São 5 linhas."),
    )
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "quantas linhas?"}]))
    kinds = [e.kind for e in events]
    assert kinds == ["delta", "tool", "delta", "delta", "done"]  # texto, tool, separador, texto final, fim
    done = events[-1].data
    assert done["answer"] == "São 5 linhas." and done["queries"] == ["SELECT count(*) AS n FROM vendas_2026"]

    # segundo pedido: turno do assistente devolvido e tool_result na mensagem seguinte
    second = provider.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-1]["role"] == "user"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1" and not result["is_error"]
    assert "5" in result["content"]
    # o esquema da planilha vai no prompt de sistema e as ferramentas são oferecidas
    assert "vendas_2026" in provider.calls[0]["system"] and provider.calls[0]["tools"]


@pytest.mark.asyncio
async def test_agent_survives_failed_sql_and_corrects(dataset):
    provider = ScriptedProvider(
        tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT x FROM nada"})),
        tool_turn(ToolUse("t2", "run_sql", {"query": "SELECT 1 AS x"})),
        text_turn("ok"),
    )
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "oi"}]))
    assert events[-1].kind == "done"
    first_result = provider.calls[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] is True


@pytest.mark.asyncio
async def test_agent_emits_chart_event(dataset):
    provider = ScriptedProvider(
        tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT regiao, sum(vendas) AS total FROM vendas_2026 GROUP BY 1"})),
        tool_turn(ToolUse("t2", "make_chart", {"kind": "bar", "x": "regiao", "y": ["total"], "title": "T"})),
        text_turn("Veja o gráfico."),
    )
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "grafico"}]))
    assert [e.kind for e in events].count("chart") == 1
    assert events[-1].data["charts"] == 1


@pytest.mark.asyncio
async def test_agent_stops_at_step_limit(dataset):
    loop = [tool_turn(ToolUse(f"t{i}", "run_sql", {"query": "SELECT 1"})) for i in range(3)]
    events = await collect(run_agent(ScriptedProvider(*loop), dataset, [{"role": "user", "content": "x"}], max_steps=3))
    assert events[-1].kind == "error"


@pytest.mark.asyncio
async def test_agent_reports_refusal(dataset):
    provider = ScriptedProvider([LLMChunk(done=True, stop_reason="refusal")])
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "x"}]))
    assert events[-1].kind == "error"


# ---------- API ----------

def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        out.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


@pytest.fixture
def client(store):
    provider = ScriptedProvider(
        tool_turn(ToolUse("t1", "run_sql", {"query": "SELECT count(*) AS n FROM vendas_2026"})),
        text_turn("São 5 linhas."),
    )
    app = create_app(Settings(provider="echo"), provider, store)
    with TestClient(app) as c:
        c.provider = provider
        yield c


def upload(client, name="vendas 2026.csv", data=CSV):
    return client.post("/upload", files={"file": (name, data, "text/csv")})


def test_upload_chat_report_delete_flow(client):
    r = upload(client)
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert r.json()["tables"][0]["name"] == "vendas_2026"

    events = parse_sse(client.post("/chat", json={"session_id": sid, "message": "quantas linhas?"}).text)
    assert [n for n, _ in events][0] == "meta" and events[-1][0] == "done"
    assert any(n == "tool" for n, _ in events)

    report = client.get(f"/sessions/{sid}/report.md")
    assert report.status_code == 200
    assert "quantas linhas?" in report.text and "```sql" in report.text and "São 5 linhas." in report.text

    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}/report.md").status_code == 404
    assert client.post("/chat", json={"session_id": sid, "message": "oi"}).status_code == 404


def test_upload_errors_are_user_friendly(client):
    r = upload(client, "x.txt", b"abc")
    assert r.status_code == 422 and "Formato" in r.json()["error"]


def test_chat_unknown_session(client):
    assert client.post("/chat", json={"session_id": "nope", "message": "oi"}).status_code == 404


def test_chat_rate_limit(store):
    app = create_app(Settings(provider="echo", rate_limit_per_minute=1), ScriptedProvider(text_turn("a"), text_turn("b")), store)
    with TestClient(app) as c:
        sid = upload(c).json()["session_id"]
        assert c.post("/chat", json={"session_id": sid, "message": "1"}).status_code == 200
        assert c.post("/chat", json={"session_id": sid, "message": "2"}).status_code == 429


def test_upload_rate_limit(store):
    app = create_app(Settings(provider="echo"), ScriptedProvider(), store)
    with TestClient(app) as c:
        codes = [upload(c).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_static_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Agente analista" in r.text
