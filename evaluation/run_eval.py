"""Avalia o agente de planilhas contra perguntas com gabarito.

Uso (na pasta 02-agente-planilhas, com GOOGLE_API_KEY, ou a chave do provedor escolhido, definida):

    ../.venv/Scripts/python -m evaluation.run_eval                      # Gemini Flash-Lite (padrão)
    ../.venv/Scripts/python -m evaluation.run_eval --only receita_total regiao_top
    ../.venv/Scripts/python -m evaluation.run_eval --provider ollama --model llama3.1:8b
    ../.venv/Scripts/python -m evaluation.run_eval --provider anthropic --model claude-sonnet-5-5 --runs 3

Gasta tokens de verdade: cada pergunta usa vários passos do agente. Comece com --only e --runs 1.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from app.agent import run_agent
from app.data import DatasetStore, Limits
from portfolio_core import LLMError, Settings, get_provider

from .cases import Case, build_cases, build_sales, to_xlsx_bytes

RESULTS_DIR = Path(__file__).resolve().parent / "results"


async def run_case(provider, store: DatasetStore, xlsx: bytes, case: Case) -> dict:
    dataset = store.create("vendas.xlsx", xlsx)  # sessão nova por pergunta: sem contaminação entre casos
    started = time.monotonic()
    answer, error, queries, charts, usage, unverified = "", None, [], 0, None, []
    try:
        async for ev in run_agent(provider, dataset, [{"role": "user", "content": case.question}]):
            if ev.kind == "done":
                answer, queries, charts, usage = ev.data["answer"], ev.data["queries"], ev.data["charts"], ev.data["usage"]
                unverified = ev.data["unverified"]
            elif ev.kind == "error":
                error = ev.data["error"]
    except LLMError as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        store.delete(dataset.session_id)
    passed = error is None and bool(answer) and case.check(answer) and not unverified  # número inventado reprova
    return {
        "id": case.id, "question": case.question, "expected": case.expected, "note": case.note,
        "passed": passed, "unverified": unverified, "error": error, "answer": answer, "queries": queries, "charts": charts,
        "seconds": round(time.monotonic() - started, 1),
        "input_tokens": usage.input_tokens if usage else 0, "output_tokens": usage.output_tokens if usage else 0,
    }


async def main_async(args) -> int:
    base = Settings.from_env()
    settings = dataclasses.replace(
        base,
        provider="echo" if args.offline else (args.provider or base.provider),
        model=args.model or base.model,
        effort=args.effort,
        max_tokens=4000,
    ).resolved()
    if settings.provider == "echo" and not args.offline:
        print("Nenhum provedor configurado. Defina GOOGLE_API_KEY (padrão: gemini-flash-lite-latest) ou "
              "ANTHROPIC_API_KEY, ou LLM_PROVIDER/LLM_MODEL, e rode de novo "
              "(ou use --offline para só testar o runner).", file=sys.stderr)
        return 2
    provider = get_provider(settings)

    df = build_sales()
    xlsx = to_xlsx_bytes(df)
    cases = [c for c in build_cases(df) if not args.only or c.id in args.only]
    if not cases:
        print("Nenhum caso corresponde a --only.", file=sys.stderr)
        return 2

    print(f"Provedor: {settings.provider} · modelo: {settings.model or '-'} · {len(cases)} casos × {args.runs} execução(ões)"
          f"{' · OFFLINE' if args.offline else ''}\n")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        store = DatasetStore(Limits(max_sessions=2), root=Path(tmp))
        for case in cases:
            for run in range(args.runs):
                r = await run_case(provider, store, xlsx, case)
                r["run"] = run + 1
                results.append(r)
                mark = "OK  " if r["passed"] else "FALHA"
                print(f"[{mark}] {r['id']:<14} {r['seconds']:>5}s  {len(r['queries'])} SQL  "
                      f"{r['input_tokens']}→{r['output_tokens']} tok  esperado: {r['expected']}")
                if not r["passed"]:
                    print(f"         resposta: {(r['error'] or r['answer'])[:300]!r}")
                    if r["unverified"]:
                        print(f"         números que nenhuma consulta produziu: {r['unverified']}")
        store.close_all()

    passed = sum(r["passed"] for r in results)
    print(f"\nAcertos: {passed}/{len(results)}")
    print(f"Tokens: {sum(r['input_tokens'] for r in results)} entrada · {sum(r['output_tokens'] for r in results)} saída "
          f"(somados dos passos; sem contar cache)")
    manual = [r["id"] for r in results if r["note"] == "Revisão manual recomendada"]
    if manual:
        print(f"Revise a mão: {', '.join(manual)} (a checagem automática é aproximada).")

    RESULTS_DIR.mkdir(exist_ok=True)
    tag = f"{settings.provider}-{settings.model or 'default'}".replace("/", "_").replace(":", "_")
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}{'-offline' if args.offline else ''}.json"
    out.write_text(json.dumps({"provider": settings.provider, "model": settings.model, "effort": settings.effort,
                               "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Detalhes: {out}")
    return 0 if passed == len(results) else 1


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # consoles Windows (cp1252) não têm todos os caracteres
        stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", help="google_genai | anthropic | openai | ollama (padrão: LLM_PROVIDER / automático)")
    p.add_argument("--model", help="padrão: LLM_MODEL ou o modelo mínimo do provedor (gemini-flash-lite-latest)")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                   help="só vale para Anthropic")
    p.add_argument("--runs", type=int, default=1, help="repetições por pergunta (mede a variação)")
    p.add_argument("--only", nargs="*", help="ids de casos a rodar")
    p.add_argument("--offline", action="store_true", help="usa o provedor echo, sem API (só testa o runner)")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
