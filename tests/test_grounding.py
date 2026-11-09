import json

import pytest
from fastapi.testclient import TestClient

from app.agent import run_agent
from app.grounding import numbers_in_tool_result, parse_numbers, unverified_numbers
from app.main import create_app
from portfolio_core import LLMChunk, Settings, ToolUse, Usage

from conftest import CSV


# ---------- leitura de números ----------

@pytest.mark.parametrize(
    "text,expected",
    [("R$ 1.234,56", 1234.56), ("1,234.56", 1234.56), ("12,5", 12.5), ("1.234", 1234), ("1.234.567,89", 1234567.89), ("0.125", 0.125)],
)
def test_parse_numbers_formats(text, expected):
    assert any(abs(n - expected) < 1e-6 for n in parse_numbers(text))


def test_tool_result_numbers_are_read_from_json_not_guessed():
    content = json.dumps({"colunas": ["r", "total"], "linhas": [["Sul", 128158.7], ["Norte", 1.234]], "ok": True})
    nums = numbers_in_tool_result(content)
    assert 128158.7 in nums and 1.234 in nums and 1234.0 not in nums  # 1.234 é decimal no JSON, não "mil duzentos"
    assert 1.0 not in nums  # booleanos não viram número


# ---------- o que é sinalizado ----------

EVIDENCE = [128158.7, 600.0, 48774.09]


def test_numbers_from_queries_pass_in_any_notation():
    assert unverified_numbers("A receita foi R$ 128.158,70 em 600 linhas.", EVIDENCE) == []
    assert unverified_numbers("Receita: 128158.70", EVIDENCE) == []
    assert unverified_numbers("Cerca de 128 mil de receita.", EVIDENCE) == []  # 128 < 1000: não verificado, mas sem alarme falso


def test_invented_large_number_is_flagged():
    assert unverified_numbers("O total de receita acumulada é de R$ 96.069,57.", EVIDENCE) == ["96.069,57"]


def test_small_numbers_years_and_question_numbers_are_ignored():
    assert unverified_numbers("Cresceu 99,8% em 2026, com 7 itens.", EVIDENCE) == []
    assert unverified_numbers("Sobre os 50.000 pedidos citados: não tenho esse dado.", EVIDENCE, question="E os 50.000 pedidos?") == []


def test_each_flagged_number_is_reported_once():
    assert unverified_numbers("São 54.321 e depois 54.321 de novo.", []) == ["54.321"]


# ---------- no agente e na API ----------

class Scripted:
    def __init__(self, *turns):
        self.turns = list(turns)

    async def stream(self, *, system, messages, tools=None):
        for chunk in self.turns.pop(0):
            yield chunk


def sql_turn(query):
    return [LLMChunk(done=True, stop_reason="tool_use", content=["x"], tool_uses=[ToolUse("t1", "run_sql", {"query": query})])]


def text_turn(text):
    return [LLMChunk(text=text), LLMChunk(done=True, stop_reason="end_turn", usage=Usage(1, 1))]


async def collect(agen):
    return [e async for e in agen]


@pytest.mark.asyncio
async def test_agent_warns_about_a_number_that_no_query_produced(dataset):
    provider = Scripted(sql_turn("SELECT count(*) AS n FROM vendas_2026"), text_turn("São 5 linhas, somando R$ 96.069,57."))
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "quantas linhas?"}]))
    kinds = [e.kind for e in events]
    assert kinds[-2:] == ["warning", "done"]
    assert events[-2].data == {"unverified": ["96.069,57"]} and events[-1].data["unverified"] == ["96.069,57"]


@pytest.mark.asyncio
async def test_agent_is_silent_when_numbers_come_from_the_query(dataset):
    provider = Scripted(sql_turn("SELECT sum(vendas) AS total FROM vendas_2026"), text_turn("O total é 425,75 em vendas."))
    events = await collect(run_agent(provider, dataset, [{"role": "user", "content": "total?"}]))
    assert "warning" not in [e.kind for e in events]
    # e números que só aparecem no histórico da conversa também não são "inventados"
    big = Scripted(text_turn("Como eu disse, foram 54.321 pedidos."))
    history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "Foram 54.321 pedidos."},
               {"role": "user", "content": "repita"}]
    assert "warning" not in [e.kind for e in await collect(run_agent(big, dataset, history))]


def test_warning_reaches_the_client_and_the_report(store):
    provider = Scripted(sql_turn("SELECT count(*) AS n FROM vendas_2026"), text_turn("São 5 linhas e R$ 96.069,57."))
    with TestClient(create_app(Settings(provider="echo"), provider, store)) as client:
        sid = client.post("/upload", files={"file": ("vendas 2026.csv", CSV, "text/csv")}).json()["session_id"]
        text = client.post("/chat", json={"session_id": sid, "message": "quantas linhas?"}).text
        names = [b.split("\n", 1)[0].removeprefix("event: ") for b in text.strip().split("\n\n")]
        assert "warning" in names and names[-1] == "done"
        report = client.get(f"/sessions/{sid}/report.md").text
    assert "Atenção" in report and "96.069,57" in report
