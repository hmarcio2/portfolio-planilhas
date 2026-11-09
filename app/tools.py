"""Ferramentas do agente: descrever tabela, rodar SQL (somente leitura) e montar gráfico."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .data import Dataset, quote_ident
from .sql_guard import QueryResult, SQLRejected, jsonable, run_query

MODEL_ROWS = 30        # linhas que o modelo enxerga por consulta
STORED_ROWS = 1000     # linhas guardadas para gráficos
CHART_POINTS = 200
QUERY_TIMEOUT = 10.0

# `strict: true` garante argumentos válidos pelo esquema; mesmo assim validamos em `execute_tool`.
TOOLS: list[dict] = [
    {
        "name": "describe_table",
        "description": "Perfil de uma tabela: por coluna, nulos, valores distintos, mínimo, máximo e os valores mais "
        "frequentes das colunas de texto. Use antes de consultas sobre colunas que você não conhece.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Nome da tabela, como listado no esquema."}},
            "required": ["table"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sql",
        "description": "Executa UMA consulta SELECT (ou WITH ... SELECT) em DuckDB sobre as tabelas da planilha. "
        f"Somente leitura. Você vê até {MODEL_ROWS} linhas; use agregações e LIMIT em vez de listar tudo.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Consulta SQL do DuckDB."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "make_chart",
        "description": "Mostra um gráfico ao usuário usando o resultado da ÚLTIMA consulta run_sql. "
        "x e y devem ser nomes de colunas desse resultado; y deve ser numérico.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["bar", "line", "scatter", "pie"]},
                "x": {"type": "string", "description": "Coluna do eixo x (rótulos; numérica no scatter)."},
                "y": {"type": "array", "items": {"type": "string"}, "description": "Uma ou mais colunas numéricas."},
                "title": {"type": "string"},
            },
            "required": ["kind", "x", "y", "title"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    chart: dict | None = None
    sql: str | None = None


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def execute_tool(dataset: Dataset, name: str, args: object) -> ToolOutcome:
    """Nunca levanta exceção para o loop do agente: erros viram resultado com is_error."""
    if not isinstance(args, dict):
        return ToolOutcome("Argumentos inválidos.", True)
    try:
        if name == "describe_table":
            table = args.get("table")
            if not isinstance(table, str):
                return ToolOutcome("Informe 'table' como texto.", True)
            return _describe_table(dataset, table)
        if name == "run_sql":
            query = args.get("query")
            if not isinstance(query, str):
                return ToolOutcome("Informe 'query' como texto.", True)
            return _run_sql(dataset, query)
        if name == "make_chart":
            return _make_chart(dataset, args)
    except SQLRejected as exc:
        return ToolOutcome(str(exc), True)
    return ToolOutcome(f"Ferramenta desconhecida: {name}", True)


def _describe_table(dataset: Dataset, table: str) -> ToolOutcome:
    info = dataset.table(table)
    if info is None:
        names = ", ".join(t.name for t in dataset.tables)
        return ToolOutcome(f"Tabela '{table}' não existe. Tabelas: {names}", True)
    qt = quote_ident(info.name)
    profile = []
    for col in info.columns:
        qc = quote_ident(col.name)
        nulls, distinct, low, high = dataset.conn.cursor().execute(
            f"SELECT count(*) - count({qc}), approx_count_distinct({qc}), min({qc})::VARCHAR, max({qc})::VARCHAR FROM {qt}"
        ).fetchone()
        entry = {"coluna": col.name, "tipo": col.type, "nulos": nulls, "distintos": distinct,
                 "min": jsonable(low), "max": jsonable(high)}
        if col.type == "VARCHAR" and 0 < distinct <= 50:
            top = dataset.conn.cursor().execute(
                f"SELECT {qc}, count(*) AS n FROM {qt} WHERE {qc} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
            entry["mais_frequentes"] = [[jsonable(v), n] for v, n in top]
        profile.append(entry)
    return ToolOutcome(_dump({"tabela": info.name, "linhas": info.rows, "colunas": profile}))


def _run_sql(dataset: Dataset, query: str) -> ToolOutcome:
    result: QueryResult = run_query(dataset.conn, query, max_rows=STORED_ROWS, timeout=QUERY_TIMEOUT)
    dataset.last_result = result
    shown = result.rows[:MODEL_ROWS]
    payload = {"colunas": result.columns, "linhas": shown, "linhas_retornadas": len(result.rows)}
    if result.truncated:
        payload["aviso"] = f"Resultado cortado em {STORED_ROWS} linhas; agregue ou filtre."
    if len(result.rows) > len(shown):
        payload["aviso_exibicao"] = f"Mostrando as primeiras {len(shown)} de {len(result.rows)} linhas."
    return ToolOutcome(_dump(payload), sql=query)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _make_chart(dataset: Dataset, args: dict) -> ToolOutcome:
    result: QueryResult | None = dataset.last_result  # type: ignore[assignment]
    if result is None or not result.rows:
        return ToolOutcome("Rode uma consulta com run_sql que retorne linhas antes de montar o gráfico.", True)
    kind, x, ys, title = args.get("kind"), args.get("x"), args.get("y"), args.get("title", "")
    if kind not in {"bar", "line", "scatter", "pie"} or not isinstance(x, str) or not isinstance(ys, list) or not ys \
            or not all(isinstance(y, str) for y in ys):
        return ToolOutcome("Argumentos inválidos para make_chart.", True)
    missing = [c for c in [x, *ys] if c not in result.columns]
    if missing:
        return ToolOutcome(f"Colunas ausentes no último resultado: {missing}. Disponíveis: {result.columns}", True)
    if kind == "pie":
        ys = ys[:1]

    rows = result.rows[:CHART_POINTS]
    xi = result.columns.index(x)
    datasets = []
    for y in ys:
        yi = result.columns.index(y)
        if not all(_is_number(r[yi]) or r[yi] is None for r in rows):
            return ToolOutcome(f"A coluna '{y}' não é numérica.", True)
        if kind == "scatter":
            if not all(_is_number(r[xi]) for r in rows):
                return ToolOutcome(f"No scatter, '{x}' precisa ser numérica.", True)
            datasets.append({"label": y, "data": [{"x": r[xi], "y": r[yi]} for r in rows]})
        else:
            datasets.append({"label": y, "data": [r[yi] for r in rows]})
    chart = {"kind": kind, "title": str(title)[:120], "x": x, "datasets": datasets}
    if kind != "scatter":
        chart["labels"] = ["" if r[xi] is None else str(r[xi]) for r in rows]
    return ToolOutcome(f"Gráfico exibido ao usuário ({len(rows)} pontos).", chart=chart)
