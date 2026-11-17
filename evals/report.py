"""Combine all eval runs into evals/RESULTS.md and docs/results.json (read by the report page)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"

ROWS = [("overall", "Overall pass rate (%)"), ("data_accuracy", "Data questions: execution accuracy (%)"),
        ("why_accuracy", "Why questions: correct (%)"), ("retrieval_hit", "Why questions: retrieval hit (%)"),
        ("faithfulness", "Why answers judged faithful (%)"), ("refusal_accuracy", "Out-of-scope refused (%)"),
        ("unanswerable_accuracy", "Unanswerable: said so (%)"), ("made_up_answers", "Made-up answers"),
        ("latency_p50_ms", "Latency p50 (ms)"), ("latency_p95_ms", "Latency p95 (ms)")]


def load_runs() -> list[dict]:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RESULTS.glob("*.json"))]
    return sorted(runs, key=lambda r: r["date"])


def build() -> dict:
    runs = load_runs()
    if not runs:
        print("No runs yet.")
        return {}
    tags = [r["tag"] for r in runs]
    lines = ["# Ask Pulse: evaluation", "",
             f"Golden set: {runs[-1]['summary']['cases']} questions (data, why, out-of-scope, unanswerable). "
             f"Model `{runs[-1]['model']}`. Each column is one version of the agent, oldest first.", "",
             "| Metric | " + " | ".join(tags) + " |", "|---|" + "---|" * len(tags)]
    for key, label in ROWS:
        lines.append(f"| {label} | " + " | ".join(str(r["summary"].get(key, "")) for r in runs) + " |")

    modes = sorted({m for r in runs for m in r["summary"]["failures"]})
    lines += ["", "## Failure modes", "", "| Failure | " + " | ".join(tags) + " |", "|---|" + "---|" * len(tags)]
    for m in modes:
        lines.append(f"| {m} | " + " | ".join(str(r["summary"]["failures"].get(m, 0)) for r in runs) + " |")

    last = runs[-1]
    lines += ["", f"## Cases in {last['tag']}", "", "| Case | Type | Result |", "|---|---|---|"]
    for c in last["cases"]:
        lines.append(f"| {c['id']} | {c['type']} | {'✅' if c['pass'] else '❌ ' + c['failure']} |")
    (ROOT / "evals" / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    site = {"runs": [{"tag": r["tag"], "semantic": r["semantic"], "model": r["model"], "date": r["date"],
                      "summary": r["summary"],
                      "cases": [{k: c.get(k) for k in ("id", "type", "question", "pass", "failure", "sql", "answer")}
                                for c in r["cases"]]} for r in runs]}
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "results.json").write_text(json.dumps(site, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines[:16]))
    return site


if __name__ == "__main__":
    build()
