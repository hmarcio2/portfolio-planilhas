"""Relatório em Markdown da análise (perguntas, respostas e SQL usado)."""

from __future__ import annotations

from datetime import datetime, timezone

from .data import Dataset


def build_report(dataset: Dataset) -> str:
    lines = [
        f"# Análise de {dataset.filename}",
        "",
        f"Gerado em {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC.",
        "",
        "## Tabelas",
        "",
        dataset.schema_text(),
        "",
    ]
    if not dataset.report:
        lines += ["_Nenhuma pergunta feita ainda._", ""]
    for i, item in enumerate(dataset.report, 1):
        lines += [f"## {i}. {item['question']}", "", item["answer"].strip(), ""]
        if item.get("unverified"):
            lines += [f"> **Atenção:** os números {', '.join(item['unverified'])} não aparecem nos resultados das "
                      "consultas desta resposta. Confira-os antes de usar.", ""]
        if item.get("charts"):
            lines += [f"_{item['charts']} gráfico(s) exibido(s) na interface._", ""]
        for sql in item.get("queries", []):
            lines += ["```sql", sql.strip(), "```", ""]
    lines += ["---", "Respostas geradas por IA a partir de consultas SQL sobre a planilha; confira os números críticos."]
    return "\n".join(lines) + "\n"
