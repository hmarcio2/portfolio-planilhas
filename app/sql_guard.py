"""Execução segura de SQL do agente sobre o DuckDB da sessão.

Camadas de defesa (nenhuma depende da outra):
1. Só uma instrução, do tipo SELECT (``WITH ... SELECT`` incluso), validada pelo parser do DuckDB.
2. O DuckDB da sessão é travado depois de carregar os dados: sem acesso a arquivos/rede,
   sem autoload de extensões e com a configuração bloqueada (ver ``data.py``).
3. Timeout por consulta (interrupção) e limite de linhas devolvidas.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import threading
from dataclasses import dataclass

import duckdb

MAX_CELL_CHARS = 200


class SQLRejected(ValueError):
    """Consulta recusada ou que falhou; a mensagem é segura para devolver ao modelo."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool  # há mais linhas do que o limite


def jsonable(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def validate(sql: str) -> None:
    if not sql or not sql.strip():
        raise SQLRejected("Consulta vazia.")
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise SQLRejected(f"SQL inválido: {_short(exc)}") from None
    if len(statements) != 1:
        raise SQLRejected("Envie exatamente uma instrução SQL por chamada.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SQLRejected("Somente consultas SELECT (ou WITH ... SELECT) são permitidas.")


def run_query(conn: duckdb.DuckDBPyConnection, sql: str, *, max_rows: int, timeout: float) -> QueryResult:
    validate(sql)
    cursor = conn.cursor()
    timer = threading.Timer(timeout, cursor.interrupt)
    timer.start()
    try:
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException:
        raise SQLRejected(f"A consulta excedeu o tempo limite de {timeout:g}s. Simplifique ou filtre mais.") from None
    except duckdb.Error as exc:
        raise SQLRejected(f"Erro ao executar: {_short(exc)}") from None
    finally:
        timer.cancel()
        cursor.close()
    truncated = len(fetched) > max_rows
    rows = [[jsonable(v) for v in row] for row in fetched[:max_rows]]
    return QueryResult(columns=columns, rows=rows, truncated=truncated)


def _short(exc: Exception, limit: int = 500) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"
