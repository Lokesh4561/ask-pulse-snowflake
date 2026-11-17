"""Run the golden set against the agent and save the scores.

  python -m evals.run --semantic v1 --tag v1-baseline
  python -m evals.run --semantic v3 --tag v3-verified --why-n 12
  python -m evals.report        # rebuild evals/RESULTS.md and docs/results.json from all runs

'why' cases are generated at run time from real trending pages that have an explanation
(synthetic questions over production data), with three question phrasings to test robustness.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import yaml

from askpulse import agent, snow
from askpulse.semantic import OUT as SEMANTIC_DIR
from evals import score

HERE = Path(__file__).parent
RESULTS = HERE / "results"
STAGE = f"@{snow.FQ}.SEMANTIC_MODELS"
WHY_TEMPLATES = [
    "Why is {page} trending on {lang} Wikipedia?",
    "What happened with {page}? Why are people in India reading about it?",
    "Explain the spike in {lang} readers for {page}.",
]


def upload_semantic_models(conn) -> None:
    for f in sorted(SEMANTIC_DIR.glob("pulse_v*.yaml")):
        snow.query(conn, f"PUT 'file://{f.as_posix()}' {STAGE} AUTO_COMPRESS=FALSE OVERWRITE=TRUE")


def why_cases(conn, n: int, seed: int) -> list[dict]:
    _, rows = snow.query(conn, """
        SELECT DISTINCT t.PAGE_TITLE, t.LANGUAGE_CODE, l.LANGUAGE_NAME
        FROM TRENDING_PAGES t JOIN (SELECT DISTINCT LANGUAGE_CODE, LANGUAGE_NAME FROM LANGUAGE_HOURLY) l
          USING (LANGUAGE_CODE)
        WHERE t.WHY_REASON_EN IS NOT NULL
          AND t.HOUR_UTC > (SELECT MAX(HOUR_UTC) FROM TRENDING_PAGES) - INTERVAL '3 DAYS'
        ORDER BY 1, 2""")
    rng = random.Random(seed)
    picks = rng.sample(rows, min(n, len(rows)))
    return [{"id": f"w{i + 1:02d}_{lang}", "type": "why", "page": page, "language_code": lang,
             "question": WHY_TEMPLATES[i % len(WHY_TEMPLATES)].format(page=page.replace("_", " "), lang=name)}
            for i, (page, lang, name) in enumerate(picks)]


def load_cases(path: Path) -> list[dict]:
    g = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [{**c, "type": "data"} for c in g.get("data", [])]
    cases += [{**c, "type": "refuse"} for c in g.get("refuse", [])]
    cases += [{**c, "type": "unanswerable"} for c in g.get("unanswerable", [])]
    return cases


def judge(conn, case: dict, ans: dict) -> dict | None:
    sources = "\n".join(f"[{i}] {s.get('PAGE_TITLE')}: {s.get('WHY_REASON_EN')}"
                        for i, s in enumerate(ans["sources"], 1))
    text = snow.complete(conn, agent.MODEL, score.JUDGE_PROMPT.format(sources=sources, q=case["question"],
                                                                      a=ans["answer"]))
    return score.parse_judge(text)


def run_case(conn, case: dict, semantic_file: str) -> dict:
    a = agent.ask(conn, case["question"], semantic_file, source="eval").to_dict()
    rec = {"id": case["id"], "type": case["type"], "question": case["question"], "route": a["route"],
           "status": a["status"], "sql": a["sql"], "answer": a["answer"][:500], "error": a["error"],
           "latency_ms": a["latency_ms"], "trace_id": a["trace_id"]}
    if case["type"] == "data":
        _, ref = snow.query(conn, case["reference_sql"])
        ok, why = score.score_data(a, ref, case.get("ordered", False))
        rec["reference_rows"] = [[str(v) for v in r] for r in ref[:10]]
        rec["agent_rows"] = a["rows"][:10]
    elif case["type"] == "why":
        j = judge(conn, case, a) if a["status"] == "ok" and a["sources"] else None
        ok, why = score.score_why(a, case["page"], case["language_code"], j)
        rec["judge"], rec["page"] = j, case["page"]
        rec["retrieved"] = [s.get("PAGE_TITLE") for s in a["sources"]]
    elif case["type"] == "refuse":
        ok, why = score.score_refuse(a)
    else:
        ok, why = score.score_unanswerable(a)
    rec["pass"], rec["failure"] = ok, why
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semantic", default="v3", choices=["v1", "v2", "v3"])
    ap.add_argument("--tag")
    ap.add_argument("--why-n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--only")
    args = ap.parse_args()
    tag = args.tag or f"{args.semantic}"
    semantic_file = f"{STAGE}/pulse_{args.semantic}.yaml"

    with snow.session() as conn:
        upload_semantic_models(conn)
        cases = load_cases(HERE / "golden.yaml") + why_cases(conn, args.why_n, args.seed)
        if args.only:
            cases = [c for c in cases if c["id"] == args.only]
        records = []
        for i, case in enumerate(cases, 1):
            rec = run_case(conn, case, semantic_file)
            records.append(rec)
            mark = "PASS" if rec["pass"] else f"FAIL {rec['failure']}"
            print(f"[{i}/{len(cases)}] {case['id']}: {mark} ({rec['latency_ms']} ms)", flush=True)

    result = {"tag": tag, "semantic": args.semantic, "model": agent.MODEL, "seed": args.seed,
              "date": time.strftime("%Y-%m-%d %H:%M"), "summary": score.summarize(records), "cases": records}
    if not args.only:
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / f"{tag}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
