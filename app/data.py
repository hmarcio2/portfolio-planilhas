"""Upload de planilhas: validação, conversão para Parquet e carga num DuckDB travado por sessão."""

from __future__ import annotations

import io
import re
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd


class UploadError(ValueError):
    """Arquivo recusado; a mensagem é mostrada ao usuário."""


class StoreFull(RuntimeError):
    """Limite de sessões simultâneas atingido."""


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 5 * 1024 * 1024
    max_rows: int = 200_000
    max_columns: int = 100
    max_sheets: int = 5
    ttl_seconds: float = 1800
    max_sessions: int = 30


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    rows: int
    columns: tuple[ColumnInfo, ...]

    def as_dict(self) -> dict:
        return {"name": self.name, "rows": self.rows, "columns": [{"name": c.name, "type": c.type} for c in self.columns]}


@dataclass
class Dataset:
    session_id: str
    filename: str
    path: Path
    conn: duckdb.DuckDBPyConnection
    tables: list[TableInfo]
    created: float
    last_used: float
    last_result: object | None = None  # QueryResult mais recente (usado por make_chart)
    report: list[dict] = field(default_factory=list)

    def table(self, name: str) -> TableInfo | None:
        return next((t for t in self.tables if t.name == name), None)

    def schema_text(self) -> str:
        lines = []
        for t in self.tables:
            cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
            lines.append(f"- {t.name} ({t.rows} linhas): {cols}")
        return "\n".join(lines)

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.path, ignore_errors=True)


# ---------- nomes ----------

def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(path: Path) -> str:
    return str(path).replace("'", "''")


def _ident(text: object, fallback: str) -> str:
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    if not t:
        t = fallback
    if t[0].isdigit():
        t = "c_" + t
    return t[:60]


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        count = seen.get(n, 0)
        seen[n] = count + 1
        out.append(n if count == 0 else f"{n}_{count + 1}")
    return out


# ---------- leitura ----------

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
_BR_DATE = re.compile(r"\d{2}/\d{2}/\d{4}")


def _as_dates(values: pd.Series) -> pd.Series | None:
    """Converte colunas de texto que são inteiramente datas (ISO ou dd/mm/aaaa); senão, None."""
    non_null = values.dropna()
    if non_null.empty:
        return None
    if non_null.map(lambda v: bool(_ISO_DATE.fullmatch(v.strip()))).all():
        dayfirst = False
    elif non_null.map(lambda v: bool(_BR_DATE.fullmatch(v.strip()))).all():
        dayfirst = True
    else:
        return None
    try:
        return pd.to_datetime(values, dayfirst=dayfirst, errors="raise")
    except (ValueError, TypeError):
        return None


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    df.columns = _unique([_ident(c, f"coluna_{i + 1}") for i, c in enumerate(df.columns)])
    for col in df.columns:
        # pandas 3 lê texto como dtype "str"; versões anteriores usam "object".
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            values = df[col].dropna()
            if not values.map(lambda v: isinstance(v, str)).all():  # tipos mistos viram texto
                df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
            elif (dates := _as_dates(df[col])) is not None:
                df[col] = dates
    return df


def parse_upload(filename: str, data: bytes, limits: Limits) -> dict[str, pd.DataFrame]:
    ext = Path(filename).suffix.lower()
    stem = _ident(Path(filename).stem, "planilha")
    if not data:
        raise UploadError("O arquivo está vazio.")
    if len(data) > limits.max_bytes:
        raise UploadError(f"O arquivo excede o limite de {limits.max_bytes // (1024 * 1024)} MB.")

    raw: dict[str, pd.DataFrame]
    if ext == ".csv":
        raw = {stem: _read_csv(data)}
    elif ext == ".xlsx":
        raw = _read_xlsx(data, stem, limits)
    else:
        raise UploadError("Formato não suportado. Envie um arquivo .csv ou .xlsx.")

    frames: dict[str, pd.DataFrame] = {}
    names = _unique([_ident(n, f"aba_{i + 1}") for i, n in enumerate(raw)])
    for name, df in zip(names, raw.values()):
        df = _clean(df)
        if df.empty or len(df.columns) == 0:
            continue
        if len(df) > limits.max_rows:
            raise UploadError(f"A tabela '{name}' tem mais de {limits.max_rows} linhas.")
        if len(df.columns) > limits.max_columns:
            raise UploadError(f"A tabela '{name}' tem mais de {limits.max_columns} colunas.")
        frames[name] = df
    if not frames:
        raise UploadError("Não encontrei dados na planilha.")
    if len(frames) > limits.max_sheets:
        raise UploadError(f"A planilha tem mais de {limits.max_sheets} abas com dados.")
    return frames


def _read_csv(data: bytes) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), sep=None, engine="python", encoding=encoding)
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    raise UploadError("Não consegui ler o CSV. Verifique o separador e a codificação.")


def _read_xlsx(data: bytes, stem: str, limits: Limits) -> dict[str, pd.DataFrame]:
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise UploadError("O arquivo não parece um .xlsx válido.")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:  # evita "zip bomb"
        if sum(i.file_size for i in zf.infolist()) > limits.max_bytes * 20:
            raise UploadError("O conteúdo descompactado do arquivo é grande demais.")
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, engine="openpyxl")
    except Exception:
        raise UploadError("Não consegui ler o .xlsx.") from None
    return sheets


# ---------- carga ----------

def _load(session_id: str, filename: str, path: Path, frames: dict[str, pd.DataFrame]) -> Dataset:
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("SET memory_limit='512MB'")
        conn.execute("SET threads=2")
        writer = duckdb.connect(":memory:")
        try:
            for name, df in frames.items():
                parquet = path / f"{name}.parquet"
                writer.register("df", df)
                writer.execute(f"COPY df TO '{_literal(parquet)}' (FORMAT PARQUET)")
                writer.unregister("df")
                conn.execute(f"CREATE TABLE {quote_ident(name)} AS SELECT * FROM read_parquet('{_literal(parquet)}')")
        finally:
            writer.close()

        tables = []
        for name in frames:
            rows = conn.execute(f"SELECT count(*) FROM {quote_ident(name)}").fetchone()[0]
            cols = conn.execute(f"DESCRIBE {quote_ident(name)}").fetchall()
            tables.append(TableInfo(name, rows, tuple(ColumnInfo(c[0], c[1]) for c in cols)))

        # Travar: depois disto o SQL do agente não toca em disco, rede nem configuração.
        conn.execute("SET autoinstall_known_extensions=false")
        conn.execute("SET autoload_known_extensions=false")
        conn.execute("SET enable_external_access=false")
        conn.execute("SET lock_configuration=true")
    except Exception:
        conn.close()
        raise
    now = time.monotonic()
    return Dataset(session_id, filename, path, conn, tables, created=now, last_used=now)


class DatasetStore:
    def __init__(self, limits: Limits | None = None, root: Path | None = None):
        self.limits = limits or Limits()
        self._root = root
        self._items: dict[str, Dataset] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, data: bytes) -> Dataset:
        frames = parse_upload(filename, data, self.limits)
        self.cleanup()
        with self._lock:
            if len(self._items) >= self.limits.max_sessions:
                raise StoreFull("Muitas análises em andamento. Tente novamente em alguns minutos.")
            session_id = secrets.token_urlsafe(16)
            path = Path(tempfile.mkdtemp(prefix="sheets_", dir=self._root))
            try:
                dataset = _load(session_id, filename, path, frames)
            except Exception:
                shutil.rmtree(path, ignore_errors=True)
                raise
            self._items[session_id] = dataset
            return dataset

    def get(self, session_id: str) -> Dataset | None:
        with self._lock:
            item = self._items.get(session_id)
            if item and time.monotonic() - item.last_used > self.limits.ttl_seconds:
                self._items.pop(session_id).close()
                return None
            if item:
                item.last_used = time.monotonic()
            return item

    def delete(self, session_id: str) -> bool:
        with self._lock:
            item = self._items.pop(session_id, None)
        if item:
            item.close()
        return item is not None

    def cleanup(self) -> int:
        now = time.monotonic()
        with self._lock:
            expired = [s for s, d in self._items.items() if now - d.last_used > self.limits.ttl_seconds]
            items = [self._items.pop(s) for s in expired]
        for item in items:
            item.close()
        return len(items)

    def close_all(self) -> None:
        with self._lock:
            items, self._items = list(self._items.values()), {}
        for item in items:
            item.close()

    def __len__(self) -> int:
        return len(self._items)
