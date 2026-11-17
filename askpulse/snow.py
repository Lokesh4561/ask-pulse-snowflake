"""Snowflake connection (key-pair login) and small helpers."""
from __future__ import annotations

import json
import os
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


def search(conn, query_text: str, limit: int = 5, language_code: str | None = None) -> list[dict]:
    """Query the Cortex Search service over AI explanations."""
    body = {"query": query_text, "limit": limit,
            "columns": ["PAGE_TITLE", "LANGUAGE_CODE", "LANGUAGE_NAME", "HOUR_UTC", "WHY_CATEGORY",
                        "WHY_REASON_EN", "SOURCES"]}
    if language_code:
        body["filter"] = {"@eq": {"LANGUAGE_CODE": language_code}}
    _, rows = query(conn, "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)",
                    (f"{FQ}.EXPLAIN_SEARCH", json.dumps(body)))
    return json.loads(rows[0][0]).get("results", []) if rows else []
