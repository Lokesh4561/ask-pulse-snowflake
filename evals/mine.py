"""Turn real usage into new eval cases (closing the loop from production traces).

  python -m evals.mine --days 7
  -> evals/candidates.csv: app questions that got a thumbs-down, errored, were blocked,
     or returned no data. Review them, write the correct reference SQL, and add the good
     ones to evals/golden.yaml so the next eval run covers them.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from askpulse import snow

OUT = Path(__file__).parent / "candidates.csv"

SQL = """
SELECT ASKED_AT, QUESTION, ROUTE, STATUS, FEEDBACK, ERROR, SQL_TEXT
FROM TRACES
WHERE SOURCE = 'app' AND ASKED_AT > DATEADD('day', -%s, CURRENT_TIMESTAMP())
  AND (FEEDBACK = 'down' OR STATUS IN ('error', 'blocked_sql', 'no_data'))
QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(QUESTION)) ORDER BY ASKED_AT DESC) = 1
ORDER BY ASKED_AT DESC
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    with snow.session() as conn:
        cols, rows = snow.query(conn, SQL, (args.days,))
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c.lower() for c in cols] + ["reference_sql", "add_to_golden"])
        for r in rows:
            w.writerow([*r, "", ""])
    print(f"{len(rows)} candidate questions written to {OUT}")


if __name__ == "__main__":
    main()
