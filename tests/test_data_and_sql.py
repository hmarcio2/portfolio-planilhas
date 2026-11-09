import pandas as pd
import pytest

from app.data import DatasetStore, Limits, UploadError, StoreFull
from app.sql_guard import SQLRejected, run_query

from conftest import CSV, make_xlsx


def q(dataset, sql, **kw):
    return run_query(dataset.conn, sql, max_rows=kw.pop("max_rows", 100), timeout=kw.pop("timeout", 5))


# ---------- upload ----------

def test_csv_is_loaded_with_clean_names(dataset):
    assert [t.name for t in dataset.tables] == ["vendas_2026"]
    table = dataset.tables[0]
    assert table.rows == 5
    assert [c.name for c in table.columns] == ["regiao", "produto", "vendas", "data"]
    assert (dataset.path / "vendas_2026.parquet").exists()


def test_iso_and_brazilian_dates_become_date_columns(store):
    ds = store.create("d.csv", "iso,br,texto\n2026-01-05,31/01/2026,a\n2026-02-10,01/02/2026,b\n".encode())
    types = {c.name: c.type for c in ds.tables[0].columns}
    assert types["iso"].startswith("TIMESTAMP") and types["br"].startswith("TIMESTAMP") and types["texto"] == "VARCHAR"
    assert q(ds, "SELECT month(br) FROM d ORDER BY br LIMIT 1").rows == [[1]]  # 31/01 lido como dia 31


def test_dates_are_jsonable(dataset):
    assert q(dataset, "SELECT data FROM vendas_2026 LIMIT 1").rows[0][0].startswith("2026-01-05")


def test_latin1_csv(store):
    data = "nome;valor\nJosé;1\nMaçã;2\n".encode("latin-1")
    ds = store.create("a.csv", data)
    assert q(ds, "SELECT count(*) FROM a").rows == [[2]]


def test_xlsx_multiple_sheets_become_tables(store):
    data = make_xlsx({"Vendas 2026": pd.DataFrame({"a": [1, 2]}), "Clientes": pd.DataFrame({"b": ["x", "y", "z"]})})
    ds = store.create("pasta.xlsx", data)
    assert sorted(t.name for t in ds.tables) == ["clientes", "vendas_2026"]
    assert q(ds, "SELECT count(*) FROM clientes").rows == [[3]]


def test_mixed_type_column_does_not_break(store):
    data = make_xlsx({"s": pd.DataFrame({"v": [1, "dois", 3.5]})})
    ds = store.create("m.xlsx", data)
    assert ds.tables[0].columns[0].type == "VARCHAR"


@pytest.mark.parametrize(
    "name,data",
    [("a.txt", b"x"), ("a.csv", b""), ("a.xlsx", b"nao eh zip"), ("a.csv", b"\n\n")],
)
def test_bad_uploads_are_rejected(store, name, data):
    with pytest.raises(UploadError):
        store.create(name, data)


def test_size_rows_and_columns_limits(tmp_path):
    s = DatasetStore(Limits(max_bytes=50, max_rows=2, max_columns=2), root=tmp_path)
    with pytest.raises(UploadError, match="limite"):
        s.create("a.csv", b"x" * 51)
    s2 = DatasetStore(Limits(max_rows=2, max_columns=2), root=tmp_path)
    with pytest.raises(UploadError, match="linhas"):
        s2.create("a.csv", b"a,b\n1,2\n3,4\n5,6\n")
    with pytest.raises(UploadError, match="colunas"):
        s2.create("a.csv", b"a,b,c\n1,2,3\n")


def test_session_cap_and_ttl_cleanup(tmp_path):
    s = DatasetStore(Limits(max_sessions=1, ttl_seconds=0.05), root=tmp_path)
    ds = s.create("a.csv", CSV)
    path = ds.path
    with pytest.raises(StoreFull):
        s.create("b.csv", CSV)
    import time
    time.sleep(0.1)
    assert s.get(ds.session_id) is None
    assert not path.exists()  # arquivos apagados
    s.create("b.csv", CSV)  # volta a caber


def test_delete_removes_files(store):
    ds = store.create("a.csv", CSV)
    assert store.delete(ds.session_id)
    assert not ds.path.exists()
    assert not store.delete(ds.session_id)


# ---------- SQL ----------

def test_select_and_aggregate(dataset):
    r = q(dataset, "SELECT regiao, sum(vendas) AS total FROM vendas_2026 GROUP BY 1 ORDER BY 1")
    assert r.columns == ["regiao", "total"]
    assert r.rows == [["Norte", 150.5], ["Sul", 275.25]]


def test_with_select_allowed(dataset):
    assert q(dataset, "WITH a AS (SELECT 1 AS x) SELECT * FROM a").rows == [[1]]


def test_row_limit_truncates(dataset):
    r = q(dataset, "SELECT * FROM range(50)", max_rows=10)
    assert len(r.rows) == 10 and r.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE vendas_2026",
        "DELETE FROM vendas_2026",
        "INSERT INTO vendas_2026 SELECT * FROM vendas_2026",
        "UPDATE vendas_2026 SET vendas = 0",
        "CREATE TABLE x AS SELECT 1",
        "COPY vendas_2026 TO 'x.csv'",
        "COPY (SELECT 1) TO 'x.csv'",
        "ATTACH 'outro.db'",
        "PRAGMA database_list",
        "SET enable_external_access=true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPLAIN SELECT 1",
        "SELECT 1; DROP TABLE vendas_2026",
        "SELECT 1; SELECT 2",
        "",
        "   ",
        "isto nao eh sql",
    ],
)
def test_non_select_and_garbage_rejected(dataset, sql):
    with pytest.raises(SQLRejected):
        q(dataset, sql)
    assert q(dataset, "SELECT count(*) FROM vendas_2026").rows == [[5]]  # dados intactos


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('qualquer.csv')",
        "SELECT * FROM read_text('C:/Windows/win.ini')",
        "SELECT * FROM read_parquet('*.parquet')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM 'dados.csv'",
        "SELECT * FROM read_json('http://example.com/x.json')",
    ],
)
def test_file_and_network_access_blocked_even_in_select(dataset, sql):
    with pytest.raises(SQLRejected):
        q(dataset, sql)


def test_cannot_change_config_via_select(dataset):
    with pytest.raises(SQLRejected):
        q(dataset, "SELECT * FROM query('SET enable_external_access=true')")


def test_timeout_interrupts_heavy_query(dataset):
    with pytest.raises(SQLRejected, match="tempo limite"):
        q(dataset, "SELECT count(*) FROM range(100000000) a, range(100000000) b", timeout=0.5)
    assert q(dataset, "SELECT 1").rows == [[1]]  # conexão continua usável


def test_error_message_is_returned_for_self_correction(dataset):
    with pytest.raises(SQLRejected, match="(?i)nao_existe|not found|does not exist"):
        q(dataset, "SELECT nao_existe FROM vendas_2026")
