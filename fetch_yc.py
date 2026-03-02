"""
fetch_yc.py — Fetches recent YC batch companies for design agency lead gen.

Pulls W25, S24, W24 batches from the public YC API and writes to data/yc.json.

Usage: python fetch_yc.py
Output: data/yc.json
"""

import json
import time
from datetime import datetime
from pathlib import Path

import requests

CONTACT_EMAIL = "something123@gmail.com"
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
OUTPUT_PATH = Path("data/yc.json")

# Recent batches — add more as new ones launch
TARGET_BATCHES = ["W25", "S24", "W24"]

YC_API = "https://api.ycombinator.com/v0.1/companies"


def fetch_batch(batch: str) -> list[dict]:
    companies = []
    page = 1
    while True:
        try:
            r = requests.get(
                YC_API,
                params={"batch": batch, "page": page, "per_page": 100},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            r.raise_for_status()
            items = r.json().get("companies", [])
        except requests.RequestException as e:
            print(f"  Warning: {e}")
            break

        if not items:
            break
        companies.extend(items)
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)

    return companies


def normalize(c: dict, batch: str) -> dict:
    slug = c.get("slug", "")
    return {
        "name": c.get("name", ""),
        "batch": batch,
        "website": c.get("website", ""),
        "one_liner": c.get("oneLiner", ""),
        "industries": c.get("industries", []),
        "tags": c.get("tags", []),
        "team_size": c.get("teamSize") or 0,
        "status": c.get("status", ""),
        "yc_url": f"https://www.ycombinator.com/companies/{slug}" if slug else "",
        "regions": c.get("regions", []),
    }


def main():
    all_companies = []
    for batch in TARGET_BATCHES:
        print(f"Fetching batch {batch}...")
        raw = fetch_batch(batch)
        print(f"  {len(raw)} companies")
        for c in raw:
            all_companies.append(normalize(c, batch))

    # Sort: newest batch first, then by team size desc
    batch_order = {b: i for i, b in enumerate(TARGET_BATCHES)}
    all_companies.sort(key=lambda c: (batch_order.get(c["batch"], 99), -(c["team_size"] or 0)))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "count": len(all_companies),
                "companies": all_companies,
            },
            f,
            indent=2,
        )
    print(f"\nDone. {len(all_companies)} YC companies written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
