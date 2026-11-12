"""Download SWE-bench Verified Mini task inputs into a user-chosen file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

DATASET = "MariusHobbhahn/swe-bench-verified-mini"
FIELDS = ("instance_id", "repo", "base_commit", "version", "problem_statement")


def fetch_tasks(dataset: str = DATASET, split: str = "test") -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        query = urlencode({"dataset": dataset, "config": "default", "split": split,
                           "offset": offset, "length": 100})
        with urlopen("https://datasets-server.huggingface.co/rows?" + query, timeout=90) as response:
            payload = json.load(response)
        batch = payload.get("rows", [])
        rows.extend({field: item["row"].get(field) for field in FIELDS} for item in batch)
        offset += len(batch)
        if not batch or offset >= int(payload.get("num_rows_total", offset)):
            return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--split", default="test")
    args = parser.parse_args()
    rows = fetch_tasks(args.dataset, args.split)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} task inputs to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
