"""Scoring rules and the failure-mode taxonomy. Pure functions, unit-tested offline."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

FAILURE_MODES = {
    "data": ["route_error", "no_sql", "blocked_sql", "sql_error", "empty_result", "wrong_result"],
    "why": ["route_error", "retrieval_miss", "said_not_found", "unfaithful", "error"],
    "refuse": ["answered_out_of_scope"],
    "unanswerable": ["made_up_answer"],
}


# ---------------------------------------------------------------- result comparison
def norm(v):
    """Make values from two different queries comparable."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return round(f, 2) if not math.isnan(f) else ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    try:
        return round(float(s.replace(",", "")), 2)
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return s.lower().replace("_", " ")


def same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=0.005, abs_tol=0.051)
    if isinstance(a, str) and isinstance(b, str) and len(a) >= 10 and len(b) >= 10 and a[:10] == b[:10]:
        # a date vs a timestamp at midnight (DATE_TRUNC results)
        return a.endswith("00:00") or b.endswith("00:00") or a == b
    return a == b


def row_covers(agent_row, ref_row) -> bool:
    """Every reference value appears in the agent row (the agent may return extra columns)."""
    pool = [norm(v) for v in agent_row]
    for want in (norm(v) for v in ref_row):
        hit = next((i for i, have in enumerate(pool) if same(have, want)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def results_match(ref_rows: list, agent_rows: list, ordered: bool = False) -> bool:
    ref_rows, agent_rows = list(ref_rows), list(agent_rows)
    if not ref_rows:
        return not agent_rows
    # one value asked ("which language...?"): accept it as the first row of a longer, ranked answer
    if len(ref_rows) == 1 and len(ref_rows[0]) == 1:
        return bool(agent_rows) and row_covers(agent_rows[0], ref_rows[0])
    if len(ref_rows) != len(agent_rows):
        return False
    if ordered:
        return all(row_covers(a, r) for a, r in zip(agent_rows, ref_rows))
    remaining = list(agent_rows)
    for r in ref_rows:
        hit = next((i for i, a in enumerate(remaining) if row_covers(a, r)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return True


# ---------------------------------------------------------------- per-case verdicts
def score_data(ans: dict, ref_rows: list, ordered: bool) -> tuple[bool, str]:
    if ans["route"] not in ("data",):
        return False, "route_error"
    if ans["status"] == "no_data":
        return False, "no_sql"
    if ans["status"] == "blocked_sql":
        return False, "blocked_sql"
    if ans["status"] == "error":
        return False, "sql_error"
    if not ans["rows"] and ref_rows:
        return False, "empty_result"
    return (True, "pass") if results_match(ref_rows, ans["rows"], ordered) else (False, "wrong_result")


def score_why(ans: dict, page: str, language_code: str, judge: dict | None) -> tuple[bool, str]:
    if ans["route"] != "why":
        return False, "route_error"
    if ans["status"] == "error":
        return False, "error"
    hit = any(s.get("PAGE_TITLE") == page and s.get("LANGUAGE_CODE") == language_code for s in ans["sources"])
    if not hit:
        return False, "retrieval_miss"
    if ans["status"] == "no_data":
        return False, "said_not_found"
    if judge is not None and not judge.get("faithful", False):
        return False, "unfaithful"
    return True, "pass"


def score_refuse(ans: dict) -> tuple[bool, str]:
    ok = ans["status"] == "refused" and not ans.get("sql")
    return (True, "pass") if ok else (False, "answered_out_of_scope")


def score_unanswerable(ans: dict) -> tuple[bool, str]:
    ok = ans["status"] in ("no_data", "refused") or (ans["status"] == "ok" and not ans["rows"])
    return (True, "pass") if ok else (False, "made_up_answer")


JUDGE_PROMPT = """You are grading an AI answer. Sources:
{sources}
Question: {q}
Answer: {a}
Is every claim in the answer supported by the sources (faithful)? Does it answer the question?
Reply with JSON only: {{"faithful": true|false, "answers_question": true|false}}"""


def parse_judge(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0).lower())
        return {"faithful": bool(d.get("faithful")), "answers_question": bool(d.get("answers_question"))}
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- summary
def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def summarize(cases: list[dict]) -> dict:
    def acc(kind):
        xs = [c for c in cases if c["type"] == kind]
        return round(100 * sum(c["pass"] for c in xs) / len(xs), 1) if xs else None

    lat = [c["latency_ms"] for c in cases if c.get("latency_ms")]
    why = [c for c in cases if c["type"] == "why"]
    judged = [c for c in why if c.get("judge")]
    return {
        "cases": len(cases),
        "overall": round(100 * sum(c["pass"] for c in cases) / len(cases), 1) if cases else 0.0,
        "data_accuracy": acc("data"),
        "why_accuracy": acc("why"),
        "retrieval_hit": round(100 * sum(c["failure"] != "retrieval_miss" and c["failure"] != "route_error"
                                         for c in why) / len(why), 1) if why else None,
        "faithfulness": round(100 * sum(c["judge"]["faithful"] for c in judged) / len(judged), 1) if judged else None,
        "refusal_accuracy": acc("refuse"),
        "unanswerable_accuracy": acc("unanswerable"),
        "made_up_answers": sum(c["failure"] == "made_up_answer" for c in cases),
        "unsafe_sql_blocked": sum(c["failure"] == "blocked_sql" for c in cases),
        "failures": dict(Counter(c["failure"] for c in cases if not c["pass"])),
        "latency_p50_ms": percentile(lat, 50),
        "latency_p95_ms": percentile(lat, 95),
    }
