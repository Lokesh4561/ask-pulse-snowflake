"""Load the latest Wikipedia Pulse snapshot into Snowflake (run hourly).

  python -m askpulse.load                      # from the live site
  python -m askpulse.load --file data.json     # from a local file

Each snapshot covers the last 24 hours, so hourly runs overlap; MERGE keeps one row per key and
lets an AI explanation that arrives later be added to an existing trending row.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

import requests

from askpulse import snow

DATA_URL = "https://lokesh4561.github.io/wikipedia-pulse/data.json"


def ts(value: str) -> str:
    """'2026-11-17T09:00:00Z' -> '2026-11-17 09:00:00' (Snowflake TIMESTAMP_NTZ, UTC)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")


def rows_from_snapshot(data: dict) -> dict[str, list[tuple]]:
    hours = [ts(h) for h in data.get("hours", [])]
    names = {l["code"]: (l["name"], l.get("native")) for l in data.get("languages", [])}

    lang = []
    for l in data.get("languages", []):
        for hour, views in zip(hours, l.get("series", [])):
            if views is not None:
                lang.append((hour, l["code"], l["name"], l.get("native"), int(views)))

    latest = ts(data["latest_hour"]) if data.get("latest_hour") else None
    top = []
    for code, pages in (data.get("top") or {}).items():
        if code in names and latest:
            for rank, p in enumerate(pages, 1):
                top.append((latest, code, rank, p["page"], int(p["views"])))

    trend = []
    for code, pages in (data.get("trending") or {}).items():
        for p in pages:
            why = p.get("why") or {}
            trend.append((ts(p["hour"]), code, p["page"], int(p["views"]),
                          float(p.get("baseline") or 0), float(p.get("ratio") or 0),
                          why.get("category"), why.get("reason_en"),
                          why.get("reason") if code != "en" else why.get("reason_en"),
                          why.get("confidence"), json.dumps(why.get("sources") or [], ensure_ascii=False)))
    return {"LANGUAGE_HOURLY": lang, "TOP_PAGES_HOURLY": top, "TRENDING_PAGES": trend}


MERGES = {
    "LANGUAGE_HOURLY": (
        "HOUR_UTC TIMESTAMP_NTZ, LANGUAGE_CODE VARCHAR, LANGUAGE_NAME VARCHAR, NATIVE_NAME VARCHAR, TOTAL_VIEWS NUMBER",
        """MERGE INTO LANGUAGE_HOURLY t USING {tmp} s
           ON t.HOUR_UTC = s.HOUR_UTC AND t.LANGUAGE_CODE = s.LANGUAGE_CODE
           WHEN MATCHED THEN UPDATE SET TOTAL_VIEWS = s.TOTAL_VIEWS, LOADED_AT = CURRENT_TIMESTAMP()
           WHEN NOT MATCHED THEN INSERT (HOUR_UTC, LANGUAGE_CODE, LANGUAGE_NAME, NATIVE_NAME, TOTAL_VIEWS)
             VALUES (s.HOUR_UTC, s.LANGUAGE_CODE, s.LANGUAGE_NAME, s.NATIVE_NAME, s.TOTAL_VIEWS)"""),
    "TOP_PAGES_HOURLY": (
        "HOUR_UTC TIMESTAMP_NTZ, LANGUAGE_CODE VARCHAR, RANK NUMBER, PAGE_TITLE VARCHAR, VIEWS NUMBER",
        """MERGE INTO TOP_PAGES_HOURLY t USING {tmp} s
           ON t.HOUR_UTC = s.HOUR_UTC AND t.LANGUAGE_CODE = s.LANGUAGE_CODE AND t.RANK = s.RANK
           WHEN MATCHED THEN UPDATE SET PAGE_TITLE = s.PAGE_TITLE, VIEWS = s.VIEWS, LOADED_AT = CURRENT_TIMESTAMP()
           WHEN NOT MATCHED THEN INSERT (HOUR_UTC, LANGUAGE_CODE, RANK, PAGE_TITLE, VIEWS)
             VALUES (s.HOUR_UTC, s.LANGUAGE_CODE, s.RANK, s.PAGE_TITLE, s.VIEWS)"""),
    "TRENDING_PAGES": (
        "HOUR_UTC TIMESTAMP_NTZ, LANGUAGE_CODE VARCHAR, PAGE_TITLE VARCHAR, VIEWS NUMBER, BASELINE_AVG FLOAT, "
        "SPIKE_RATIO FLOAT, WHY_CATEGORY VARCHAR, WHY_REASON_EN VARCHAR, WHY_REASON_NATIVE VARCHAR, "
        "WHY_CONFIDENCE VARCHAR, WHY_SOURCES VARCHAR",
        """MERGE INTO TRENDING_PAGES t USING {tmp} s
           ON t.HOUR_UTC = s.HOUR_UTC AND t.LANGUAGE_CODE = s.LANGUAGE_CODE AND t.PAGE_TITLE = s.PAGE_TITLE
           WHEN MATCHED THEN UPDATE SET VIEWS = s.VIEWS, BASELINE_AVG = s.BASELINE_AVG, SPIKE_RATIO = s.SPIKE_RATIO,
             WHY_CATEGORY = COALESCE(s.WHY_CATEGORY, t.WHY_CATEGORY),
             WHY_REASON_EN = COALESCE(s.WHY_REASON_EN, t.WHY_REASON_EN),
             WHY_REASON_NATIVE = COALESCE(s.WHY_REASON_NATIVE, t.WHY_REASON_NATIVE),
             WHY_CONFIDENCE = COALESCE(s.WHY_CONFIDENCE, t.WHY_CONFIDENCE),
             WHY_SOURCES = IFF(s.WHY_REASON_EN IS NULL, t.WHY_SOURCES, PARSE_JSON(s.WHY_SOURCES)),
             LOADED_AT = CURRENT_TIMESTAMP()
           WHEN NOT MATCHED THEN INSERT (HOUR_UTC, LANGUAGE_CODE, PAGE_TITLE, VIEWS, BASELINE_AVG, SPIKE_RATIO,
             WHY_CATEGORY, WHY_REASON_EN, WHY_REASON_NATIVE, WHY_CONFIDENCE, WHY_SOURCES)
             VALUES (s.HOUR_UTC, s.LANGUAGE_CODE, s.PAGE_TITLE, s.VIEWS, s.BASELINE_AVG, s.SPIKE_RATIO,
               s.WHY_CATEGORY, s.WHY_REASON_EN, s.WHY_REASON_NATIVE, s.WHY_CONFIDENCE, PARSE_JSON(s.WHY_SOURCES))"""),
}


def merge(conn, table: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    cols, sql = MERGES[table]
    tmp = f"TMP_{table}"
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {tmp} ({cols})")
        placeholders = ", ".join(["%s"] * len(rows[0]))
        cur.executemany(f"INSERT INTO {tmp} VALUES ({placeholders})", rows)
        cur.execute(sql.format(tmp=tmp))
        return len(rows)
    finally:
        cur.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--url", default=DATA_URL)
    args = ap.parse_args()
    if args.file:
        data = json.load(open(args.file, encoding="utf-8"))
    else:
        r = requests.get(args.url, timeout=60)
        r.raise_for_status()
        data = r.json()
    rows = rows_from_snapshot(data)
    with snow.session() as conn:
        for table, tr in rows.items():
            print(f"{table}: merged {merge(conn, table, tr)} rows", flush=True)
    print(f"Snapshot latest hour: {data.get('latest_hour')}")


if __name__ == "__main__":
    main()
