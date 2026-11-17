"""Semantic model for Cortex Analyst, in three versions (the 'hill-climb'):

  v1 - tables and columns only (what you get from a quick auto-generated model)
  v2 - + descriptions, business rules, language synonyms and sample values
  v3 - + verified queries (worked examples Cortex Analyst can learn from)

python -m askpulse.semantic  -> writes semantic/pulse_v1.yaml .. pulse_v3.yaml
The eval report (evals/RESULTS.md) scores every version on the same golden set.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parent.parent / "semantic"
BASE = {"database": "ASK_PULSE", "schema": "CORE"}

LANGS = {"en": "English", "hi": "Hindi", "bn": "Bengali", "te": "Telugu", "mr": "Marathi", "ta": "Tamil",
         "ur": "Urdu", "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "or": "Odia",
         "as": "Assamese", "sa": "Sanskrit"}
CODE_HELP = "Language code: " + ", ".join(f"{c}={n}" for c, n in LANGS.items()) + "."

RULES = (
    "Rules: (1) Times are UTC hours. 'Latest hour', 'now', 'last 24 hours', 'today' and 'yesterday' are "
    "relative to the newest loaded hour, MAX(HOUR_UTC), not CURRENT_TIMESTAMP, because data arrives with a delay. "
    "'Last 24 hours' means HOUR_UTC > MAX(HOUR_UTC) - 24 hours. "
    "(2) 'Trending now' or 'right now' means trending in the last 6 hours. "
    "(3) Questions that name a language (Telugu, Hindi...) must filter LANGUAGE_CODE with the code. "
    "(4) Page titles use underscores instead of spaces."
)


def _dim(name, expr, desc, dtype="VARCHAR", synonyms=None, samples=None):
    d = {"name": name, "expr": expr, "data_type": dtype, "description": desc}
    if synonyms:
        d["synonyms"] = synonyms
    if samples:
        d["sample_values"] = samples
    return d


def _time(name, expr, desc):
    return {"name": name, "expr": expr, "data_type": "TIMESTAMP_NTZ", "description": desc}


def _measure(name, expr, desc, agg="sum", synonyms=None, dtype="NUMBER"):
    m = {"name": name, "expr": expr, "data_type": dtype, "default_aggregation": agg, "description": desc}
    if synonyms:
        m["synonyms"] = synonyms
    return m


LANG_CODE = lambda: _dim("language_code", "LANGUAGE_CODE", CODE_HELP,  # noqa: E731
                         synonyms=["language", "wiki", "Wikipedia language"], samples=list(LANGS)[:8])


def full_model() -> dict:
    return {
        "name": "ask_pulse",
        "description": "What people in India read on Wikipedia, hourly, in English and 13 Indian languages: "
                       "views per language, most-read pages, and trending pages with AI explanations. " + RULES,
        "tables": [
            {
                "name": "language_hourly",
                "description": "Total page views from India per language per UTC hour. " + RULES,
                "base_table": {**BASE, "table": "LANGUAGE_HOURLY"},
                "primary_key": {"columns": ["hour_utc", "language_code"]},
                "dimensions": [
                    LANG_CODE(),
                    _dim("language_name", "LANGUAGE_NAME", "Language name in English, e.g. Telugu.",
                         synonyms=["language"], samples=list(LANGS.values())[:8]),
                    _dim("native_name", "NATIVE_NAME", "Language name written in that language."),
                ],
                "time_dimensions": [_time("hour_utc", "HOUR_UTC", "Start of the UTC hour.")],
                "measures": [_measure("total_views", "TOTAL_VIEWS", "Page views in the hour.",
                                      synonyms=["views", "page views", "traffic", "readership"])],
            },
            {
                "name": "top_pages_hourly",
                "description": "The 10 most-read pages per language for each loaded hour (rank 1 = most read). "
                               + RULES,
                "base_table": {**BASE, "table": "TOP_PAGES_HOURLY"},
                "primary_key": {"columns": ["hour_utc", "language_code", "rank"]},
                "dimensions": [
                    LANG_CODE(),
                    _dim("page_title", "PAGE_TITLE", "Wikipedia page title (underscores for spaces).",
                         synonyms=["page", "article", "topic"]),
                    _dim("page_rank", "RANK", "1 = most read page that hour.", dtype="NUMBER",
                         synonyms=["rank", "position"]),
                ],
                "time_dimensions": [_time("hour_utc", "HOUR_UTC", "Start of the UTC hour.")],
                "measures": [_measure("page_views", "VIEWS", "Views of the page in that hour.",
                                      synonyms=["views", "reads"])],
            },
            {
                "name": "trending_pages",
                "description": "Pages whose hourly views jumped to 5x or more of their 24-hour average, with an "
                               "AI explanation (category and reason) when available. " + RULES,
                "base_table": {**BASE, "table": "TRENDING_PAGES"},
                "primary_key": {"columns": ["hour_utc", "language_code", "page_title"]},
                "dimensions": [
                    LANG_CODE(),
                    _dim("page_title", "PAGE_TITLE", "Wikipedia page title (underscores for spaces).",
                         synonyms=["page", "article", "topic"]),
                    _dim("why_category", "WHY_CATEGORY",
                         "Why it is trending: death, sports, film_tv_music, politics_government, crime_accident, "
                         "festival_religion, science_tech, business, anniversary, other.",
                         synonyms=["category", "reason type", "topic type"],
                         samples=["sports", "film_tv_music", "death", "politics_government"]),
                    _dim("why_reason_en", "WHY_REASON_EN", "AI explanation in English, grounded in news.",
                         synonyms=["reason", "explanation", "why"]),
                ],
                "time_dimensions": [_time("hour_utc", "HOUR_UTC", "Hour of the spike.")],
                "measures": [
                    _measure("trend_views", "VIEWS", "Views in the spike hour.", synonyms=["views"]),
                    _measure("spike_ratio", "SPIKE_RATIO", "Views divided by the 24-hour average; 10 means 10x.",
                             agg="max", dtype="FLOAT", synonyms=["spike", "jump", "how many times usual"]),
                    _measure("baseline_avg", "BASELINE_AVG", "Average hourly views in the previous 24 hours.",
                             agg="avg", dtype="FLOAT", synonyms=["usual views", "normal views"]),
                ],
            },
        ],
        "verified_queries": [
            {"name": "views_last_24h_by_language",
             "question": "Rank the languages by total views over the last 24 hours.",
             "sql": "SELECT language_name, SUM(total_views) AS views FROM __language_hourly "
                    "WHERE hour_utc > (SELECT MAX(hour_utc) FROM __language_hourly) - INTERVAL '24 HOURS' "
                    "GROUP BY language_name ORDER BY views DESC",
             "verified_by": "Lokesh", "verified_at": 1763337600, "use_as_onboarding_question": True},
            {"name": "trending_now_telugu",
             "question": "Which Telugu pages are trending right now?",
             "sql": "SELECT page_title, MAX(spike_ratio) AS spike, MAX(trend_views) AS views FROM __trending_pages "
                    "WHERE language_code = 'te' AND hour_utc > (SELECT MAX(hour_utc) FROM __trending_pages) "
                    "- INTERVAL '6 HOURS' GROUP BY page_title ORDER BY spike DESC",
             "verified_by": "Lokesh", "verified_at": 1763337600, "use_as_onboarding_question": True},
            {"name": "top_page_latest_hour",
             "question": "What was the most-read Hindi page in the latest hour?",
             "sql": "SELECT page_title, page_views FROM __top_pages_hourly WHERE language_code = 'hi' "
                    "AND page_rank = 1 AND hour_utc = (SELECT MAX(hour_utc) FROM __top_pages_hourly)",
             "verified_by": "Lokesh", "verified_at": 1763337600, "use_as_onboarding_question": False},
            {"name": "views_per_day",
             "question": "Show total views per day for English over the last 7 days.",
             "sql": "SELECT DATE_TRUNC('DAY', hour_utc) AS day, SUM(total_views) AS views FROM __language_hourly "
                    "WHERE language_code = 'en' AND hour_utc > (SELECT MAX(hour_utc) FROM __language_hourly) "
                    "- INTERVAL '7 DAYS' GROUP BY day ORDER BY day",
             "verified_by": "Lokesh", "verified_at": 1763337600, "use_as_onboarding_question": False},
        ],
    }


def version(v: int) -> dict:
    m = copy.deepcopy(full_model())
    if v >= 3:
        return m
    m.pop("verified_queries", None)
    if v >= 2:
        return m
    # v1: bare structure, no descriptions, rules, synonyms or sample values
    m["description"] = "Wikipedia page views."
    for t in m["tables"]:
        t["description"] = t["name"].replace("_", " ")
        for key in ("dimensions", "time_dimensions", "measures"):
            for col in t.get(key, []):
                col.pop("synonyms", None)
                col.pop("sample_values", None)
                col["description"] = col["name"].replace("_", " ")
    return m


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for v in (1, 2, 3):
        path = OUT / f"pulse_v{v}.yaml"
        path.write_text(yaml.safe_dump(version(v), sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
