"""Ask Pulse agent: one question in, a grounded answer out.

  guardrails -> route -> data question:  Cortex Analyst (text-to-SQL on the semantic model)
                                         -> SQL safety check -> run SQL -> answer from the rows
                      -> why question:   Cortex Search over AI explanations -> answer citing sources
                      -> out of scope:   polite refusal
Every call is written to the TRACES table, so failures in real use become new eval cases.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

import requests

from askpulse import snow

MODEL = os.getenv("ASK_PULSE_MODEL") or "llama3.1-70b"
ALLOWED_TABLES = {"LANGUAGE_HOURLY", "TOP_PAGES_HOURLY", "TRENDING_PAGES"}
MAX_ROWS = 200

REFUSAL = "I can only answer questions about what India reads on Wikipedia (views, top pages, trending pages and why)."
NO_DATA = "I don't have data to answer that."

# ---------------------------------------------------------------- guardrails
INJECTION = re.compile(r"(ignore (all|your|previous) (instructions|rules)|system prompt|password|api key|"
                       r"secret|credential|jailbreak)", re.I)
WRITE_INTENT = re.compile(r"\b(delete|drop|truncate|update|insert|alter|grant|revoke|create|merge)\b", re.I)
SQL_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|truncate|alter|create|grant|revoke|call|"
                           r"execute)\b", re.I)


def check_input(question: str) -> str | None:
    """Return a refusal reason, or None if the question may proceed."""
    if not question.strip():
        return "empty question"
    if len(question) > 500:
        return "question too long"
    if INJECTION.search(question):
        return "prompt injection or secret request"
    if WRITE_INTENT.search(question) and re.search(r"\b(table|row|data|database|schema)s?\b", question, re.I):
        return "write or admin request"
    return None


def check_sql(sql: str) -> str | None:
    """Only a single read-only SELECT on the three allowed tables may run. Returns a problem or None."""
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";")
    if ";" in s:
        return "more than one statement"
    if not re.match(r"^\s*(with|select)\b", s, re.I):
        return "not a SELECT"
    if SQL_FORBIDDEN.search(s):
        return "write or admin keyword"
    # "FROM" also appears inside EXTRACT(HOUR FROM x), TRIM(... FROM x) and IS DISTINCT FROM: drop those first
    scan = re.sub(r"\b(extract|trim|substring|position)\s*\([^()]*\)", " ", s, flags=re.I)
    scan = re.sub(r"\bdistinct\s+from\b", " ", scan, flags=re.I)
    tables = {t.upper().split(".")[-1].strip('"') for t in
              re.findall(r"\b(?:from|join)\s+([A-Za-z0-9_.\"]+)", scan, re.I)} - {"TABLE", "LATERAL"}
    ctes = {c.upper() for c in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", s, re.I)}
    unknown = tables - ALLOWED_TABLES - ctes
    if unknown:
        return f"table not allowed: {', '.join(sorted(unknown))}"
    return None


# ---------------------------------------------------------------- routing
ROUTER_PROMPT = """Classify the user's question about Wikipedia readership in India. Reply with one word:
DATA  - numbers, rankings, lists, comparisons, counts or trends over time (views, top pages, trending pages)
WHY   - asks why a specific page or topic is trending or popular
OTHER - anything else (weather, coding, jokes, personal advice, requests to change data)
Question: {q}
Answer:"""

WHY_HINT = re.compile(r"\bwhy\b|\breason\b|\bexplain\b|\bwhat happened\b", re.I)


def route(conn, question: str) -> str:
    text = snow.complete(conn, MODEL, ROUTER_PROMPT.format(q=question)).strip().upper()
    for label in ("DATA", "WHY", "OTHER"):
        if label in text[:20]:
            return label.lower()
    return "why" if WHY_HINT.search(question) else "data"


# ---------------------------------------------------------------- data questions (Cortex Analyst)
def analyst(conn, question: str, semantic_file: str) -> tuple[str, str | None]:
    """Call the Cortex Analyst REST API. Returns (text, sql or None)."""
    url = f"https://{conn.host}/api/v2/cortex/analyst/message"
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
            "semantic_model_file": semantic_file}
    r = requests.post(url, json=body, timeout=90, headers={
        "Authorization": f'Snowflake Token="{conn.rest.token}"', "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Cortex Analyst HTTP {r.status_code}: {r.text[:300]}")
    text, sql = [], None
    for part in r.json().get("message", {}).get("content", []):
        if part.get("type") == "text":
            text.append(part.get("text", ""))
        elif part.get("type") == "sql":
            sql = part.get("statement")
    return " ".join(text).strip(), sql


ANSWER_PROMPT = """Answer the question in 1-3 short sentences using ONLY these query results.
Use exact numbers from the results. If the results are empty, say no matching data was found.
Page titles use underscores; show them with spaces.
Question: {q}
Columns: {cols}
Rows (first 20): {rows}
Answer:"""

# ---------------------------------------------------------------- why questions (Cortex Search)
WHY_PROMPT = """Answer why the page is trending using ONLY the numbered sources below.
Cite sources like [1]. If none of the sources is about the page asked, reply exactly: NOT_FOUND
Question: {q}
Sources:
{sources}
Answer:"""

LANG_WORDS = {"english": "en", "hindi": "hi", "bengali": "bn", "telugu": "te", "marathi": "mr", "tamil": "ta",
              "urdu": "ur", "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa",
              "odia": "or", "assamese": "as", "sanskrit": "sa"}


@dataclass
class Answer:
    question: str
    route: str = ""
    status: str = "ok"           # ok | refused | no_data | blocked_sql | error
    answer: str = ""
    sql: str | None = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0
    config: str = ""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [[str(v) for v in r] for r in self.rows[:50]]
        return d


def ask(conn, question: str, semantic_file: str, source: str = "app", log: bool = True) -> Answer:
    started = time.perf_counter()
    a = Answer(question=question, config=f"{semantic_file.rsplit('/', 1)[-1]}|{MODEL}")
    try:
        reason = check_input(question)
        if reason:
            a.route, a.status, a.answer, a.error = "guard", "refused", REFUSAL, reason
        else:
            a.route = route(conn, question)
            if a.route == "other":
                a.status, a.answer = "refused", REFUSAL
            elif a.route == "why":
                _answer_why(conn, a)
            else:
                _answer_data(conn, a, semantic_file)
    except Exception as e:  # noqa: BLE001  report, never crash the app
        a.status, a.error = "error", f"{type(e).__name__}: {str(e)[:300]}"
        a.answer = a.answer or "Something went wrong answering that."
    a.latency_ms = int((time.perf_counter() - started) * 1000)
    if log:
        _trace(conn, a, source)
    return a


def _answer_data(conn, a: Answer, semantic_file: str) -> None:
    text, sql = analyst(conn, a.question, semantic_file)
    if not sql:
        a.status, a.answer = "no_data", text or NO_DATA
        return
    problem = check_sql(sql)
    a.sql = sql
    if problem:
        a.status, a.error, a.answer = "blocked_sql", problem, "I can't run that query safely."
        return
    a.columns, a.rows = snow.query(conn, f"SELECT * FROM ({sql.strip().rstrip(';')}) LIMIT {MAX_ROWS}")
    a.answer = snow.complete(conn, MODEL, ANSWER_PROMPT.format(
        q=a.question, cols=a.columns, rows=json.dumps([[str(v) for v in r] for r in a.rows[:20]])))


def _answer_why(conn, a: Answer) -> None:
    lang = next((c for w, c in LANG_WORDS.items() if w in a.question.lower()), None)
    hits = snow.search(conn, a.question, limit=5, language_code=lang)
    a.sources = hits
    if not hits:
        a.status, a.answer = "no_data", "I don't have an explanation for that page yet."
        return
    numbered = "\n".join(
        f"[{i}] {h.get('PAGE_TITLE', '').replace('_', ' ')} ({h.get('LANGUAGE_NAME') or h.get('LANGUAGE_CODE')}, "
        f"{h.get('HOUR_UTC')}): {h.get('WHY_REASON_EN')}" for i, h in enumerate(hits, 1))
    text = snow.complete(conn, MODEL, WHY_PROMPT.format(q=a.question, sources=numbered)).strip()
    if "NOT_FOUND" in text:
        a.status, a.answer = "no_data", "I don't have an explanation for that page yet."
    else:
        a.answer = text


def _trace(conn, a: Answer, source: str) -> None:
    try:
        snow.query(conn, "INSERT INTO TRACES (TRACE_ID, SOURCE, QUESTION, ROUTE, CONFIG, SQL_TEXT, ROW_COUNT, "
                         "ANSWER, STATUS, ERROR, LATENCY_MS) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (a.trace_id, source, a.question, a.route, a.config, a.sql, len(a.rows), a.answer[:4000],
                    a.status, a.error, a.latency_ms))
    except Exception as e:  # noqa: BLE001  logging must never break answering
        print(f"trace failed: {e}")


def feedback(conn, trace_id: str, value: str) -> None:
    snow.query(conn, "UPDATE TRACES SET FEEDBACK = %s WHERE TRACE_ID = %s", (value, trace_id))
