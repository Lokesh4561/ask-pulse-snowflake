"""Snowflake connection (key-pair login) and small helpers."""
from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager

DB, SCHEMA = "ASK_PULSE", "CORE"
FQ = f"{DB}.{SCHEMA}"


def _private_key() -> bytes:
    """Private key from SNOWFLAKE_PRIVATE_KEY (PEM text) as DER bytes, as the connector expects."""
    from cryptography.hazmat.primitives import serialization

    pem = os.environ["SNOWFLAKE_PRIVATE_KEY"].replace("\\n", "\n").encode()
    password = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    key = serialization.load_pem_private_key(pem, password=password.encode() if password else None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def connect():
    import snowflake.connector

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.getenv("SNOWFLAKE_USER", "ASK_PULSE_SVC"),
        private_key=_private_key(),
        role=os.getenv("SNOWFLAKE_ROLE", "ASK_PULSE_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "ASK_PULSE_WH"),
        database=DB, schema=SCHEMA,
        session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 60, "QUERY_TAG": "ask-pulse"},
    )
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def query(conn, sql: str, params=None) -> tuple[list[str], list[tuple]]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return cols, cur.fetchall() if cur.description else []
    finally:
        cur.close()


def complete(conn, model: str, prompt: str) -> str:
    """Call an LLM inside Snowflake (Cortex COMPLETE)."""
    _, rows = query(conn, "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (model, prompt))
    return rows[0][0] if rows else ""


STOPWORDS = set("a an the is are was were why what who how did does do in on of for to and or it its this that "
                "page pages trending trend spike spiking popular wikipedia right now today people reading so many".split())
COLUMNS = ["PAGE_TITLE", "LANGUAGE_CODE", "LANGUAGE_NAME", "HOUR_UTC", "WHY_CATEGORY", "WHY_REASON_EN", "SOURCES"]


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " ")) if t not in STOPWORDS]


def rank(query_text: str, rows: list[dict], limit: int = 5) -> list[dict]:
    """Keyword retrieval (BM25-style): title matches count 3x, newer hours win ties. Pure Python, testable."""
    q = set(tokens(query_text))
    if not q or not rows:
        return []
    docs = [(r, tokens(r.get("PAGE_TITLE")), tokens(r.get("WHY_REASON_EN"))) for r in rows]
    n = len(docs)
    df = {t: sum(1 for _, ti, bo in docs if t in ti or t in bo) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    scored = []
    for r, ti, bo in docs:
        score = sum(idf[t] * (3 * (t in ti) + min(bo.count(t), 3) / (1 + len(bo) / 40)) for t in q)
        if score > 0:
            scored.append((score, str(r.get("HOUR_UTC")), r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored[:limit]]


def search(conn, query_text: str, limit: int = 5, language_code: str | None = None) -> list[dict]:
    """Find AI explanations for a "why" question.

    Default: keyword retrieval over the EXPLANATIONS view (works on trial accounts).
    Set ASK_PULSE_SEARCH=cortex to use a Cortex Search service instead (needs a paid account,
    see sql/optional_cortex_search.sql).
    """
    if os.getenv("ASK_PULSE_SEARCH", "keyword").lower() == "cortex":
        body = {"query": query_text, "limit": limit, "columns": COLUMNS}
        if language_code:
            body["filter"] = {"@eq": {"LANGUAGE_CODE": language_code}}
        _, rows = query(conn, "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)",
                        (f"{FQ}.EXPLAIN_SEARCH", json.dumps(body)))
        return json.loads(rows[0][0]).get("results", []) if rows else []
    sql = (f"SELECT {', '.join(COLUMNS)} FROM {FQ}.EXPLANATIONS "
           "WHERE HOUR_UTC >= DATEADD(day, -14, (SELECT MAX(HOUR_UTC) FROM " + FQ + ".EXPLANATIONS))")
    params = None
    if language_code:
        sql += " AND LANGUAGE_CODE = %s"
        params = (language_code,)
    cols, rows = query(conn, sql, params)
    return rank(query_text, [dict(zip(cols, (str(v) if v is not None else None for v in r))) for r in rows], limit)
