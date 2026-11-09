"""Notas automáticas (scores) de cada pergunta ao agente de planilhas, gravadas no trace do Langfuse.

São **sinais de observabilidade**, não bloqueios (o aviso ao usuário sobre números não verificados vem de
``app/grounding.py`` e já é mostrado na interface; aqui ele também vira uma nota para medir a frequência). As verificações
são determinísticas e heurísticas: o ``comment`` de cada nota diz o que disparou, para auditar.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Score:
    name: str
    value: float
    data_type: str = "BOOLEAN"  # BOOLEAN (0/1) ou NUMERIC
    comment: str | None = None


def evaluate_turn(*, answer: str, calls: list[dict], unverified: list[str], failed: bool = False) -> list[Score]:
    """Notas de um turno. ``calls`` são as chamadas de ferramenta (``name``, ``is_error``) e ``unverified`` os números
    grandes da resposta que não vieram de nenhuma consulta."""
    if failed or not answer:
        return []
    queries = [c for c in calls if c["name"] == "run_sql"]
    errors = [c for c in calls if c["is_error"]]
    ok_queries = [c for c in queries if not c["is_error"]]
    return [
        Score("unverified_numbers", len(unverified), "NUMERIC",
              ("números que não aparecem nos resultados das consultas: " + ", ".join(unverified[:10])) if unverified else None),
        Score("tool_errors", len(errors), "NUMERIC",
              f"{len(errors)} de {len(calls)} chamadas de ferramenta falharam (o modelo recebeu o erro e pôde se corrigir)" if errors else None),
        Score("no_query", not ok_queries,
              "a resposta foi dada sem nenhuma consulta SQL bem-sucedida (pode ser uma recusa correta ou um palpite)" if not ok_queries else None),
    ]
