"""Checagem de ancoragem: números grandes da resposta que não aparecem nos resultados das consultas.

Modelos pequenos às vezes "completam" a resposta com um valor inventado, sem rodar SQL (medido com o Gemini
Flash-Lite: uma receita de R$ 96.069,57 que não existia nos dados). Não dá para provar que um número está certo,
mas dá para apontar os que **não vieram de nenhuma consulta** e avisar o usuário. Só números grandes entram:
percentuais e contagens pequenas costumam ser derivados pelo próprio modelo (falsos alarmes).
"""

from __future__ import annotations

import json
import re

MIN_CHECKED = 1000  # abaixo disso, não verifica
REL_TOL = 0.005      # "128 mil" e arredondamentos de centavos passam

_NUM = re.compile(r"\d[\d.,]*")


def parse_numbers(text: str) -> list[float]:
    """Extrai números de um texto em formato brasileiro (1.234,56) ou americano (1,234.56)."""
    out: list[float] = []
    for token in _NUM.findall(text):
        token = token.rstrip(".,")
        if not token:
            continue
        if "," in token and "." in token:
            decimal = "," if token.rfind(",") > token.rfind(".") else "."
            thousand = "." if decimal == "," else ","
            token = token.replace(thousand, "").replace(decimal, ".")
        elif "," in token:
            head, _, tail = token.rpartition(",")
            token = token.replace(",", "") if len(tail) == 3 and head else token.replace(",", ".")
        elif token.count(".") > 1 or (token.count(".") == 1 and len(token.rpartition(".")[2]) == 3 and token.rpartition(".")[0] != "0"):
            token = token.replace(".", "")
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def numbers_in_tool_result(content: str) -> list[float]:
    """Números de um resultado de ferramenta (JSON): lê os valores numéricos de verdade, sem adivinhar formato."""
    try:
        data = json.loads(content)
    except ValueError:
        return parse_numbers(content)
    found: list[float] = []

    def walk(node) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.append(float(node))
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):  # datas, textos com número (ex.: "2026-01-05", "1.234")
            found.extend(parse_numbers(node))

    walk(data)
    return found


def _is_year(n: float) -> bool:
    return n == int(n) and 1900 <= n <= 2100


def unverified_numbers(answer: str, evidence: list[float], question: str = "") -> list[str]:
    """Números grandes de ``answer`` ausentes de ``evidence`` (e da pergunta), na forma como aparecem no texto."""
    allowed = evidence + parse_numbers(question)
    flagged: list[str] = []
    for token in dict.fromkeys(_NUM.findall(answer)):  # preserva o texto original, sem repetir
        token = token.rstrip(".,")
        for n in parse_numbers(token):
            if abs(n) < MIN_CHECKED or _is_year(n):
                continue
            if not any(abs(n - e) <= max(abs(e) * REL_TOL, 0.01) for e in allowed):
                flagged.append(token)
    return flagged
