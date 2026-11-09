import io

import pandas as pd
import pytest

from app.data import DatasetStore, Limits

CSV = (
    "Região,Produto,Vendas,Data\n"
    "Norte,Café,100.5,2026-01-05\n"
    "Sul,Café,200,2026-01-20\n"
    "Norte,Chá,50,2026-02-03\n"
    "Sul,Chá,75.25,2026-02-14\n"
    "Sul,Chá,,2026-03-01\n"
).encode("utf-8")


@pytest.fixture
def store(tmp_path):
    s = DatasetStore(Limits(max_bytes=200_000, ttl_seconds=60), root=tmp_path)
    yield s
    s.close_all()


@pytest.fixture
def dataset(store):
    return store.create("vendas 2026.csv", CSV)


def make_xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()
