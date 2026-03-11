"""
fetch_waas.py — Fetches YC companies hiring designers from Work at a Startup.

Uses Inertia.js server-rendered data embedded in the page HTML.
No authentication or API key required.

Discovery notes: see memory/data-gathering.md

Usage: python fetch_waas.py
Output: data/waas.json
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUTPUT_PATH = Path("data/waas.json")

ROLES_URL = "https://www.workatastartup.com/jobs?jobType=any&role=design"


def fetch_waas_jobs() -> list[dict]:
    r = requests.get(ROLES_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()

    dp = re.search(r'data-page="([^"]+)"', r.text)
    if not dp:
        raise RuntimeError("No data-page attribute found in WAAS response")

    raw = (dp.group(1)
           .replace("&quot;", '"')
           .replace("&amp;", "&")
           .replace("&#039;", "'")
           .replace("&lt;", "<")
           .replace("&gt;", ">"))

    data = json.loads(raw)
    return data.get("props", {}).get("jobs", [])


def main():
    print("Fetching WAAS designer jobs...")
    jobs = fetch_waas_jobs()
    print(f"  {len(jobs)} designer roles found")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(jobs),
                "jobs": jobs,
            },
            f,
            indent=2,
        )
    print(f"Done. {len(jobs)} jobs written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
