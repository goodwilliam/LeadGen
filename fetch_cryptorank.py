"""
fetch_cryptorank.py — Fetches recent crypto seed funding rounds from CryptoRank API.

Requires CRYPTORANK_API_KEY environment variable (set as GitHub Actions secret).

Usage: python fetch_cryptorank.py
Output: data/cryptorank.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

OUTPUT_PATH = Path("data/cryptorank.json")
API_BASE    = "https://api.cryptorank.io/v1"
USER_AGENT  = "DesignAgencyLeadGen/1.0 (something123@gmail.com)"

# Fetch rounds from last N days
DAYS_BACK = 90

# Only keep seed / early-stage raises
TARGET_STAGES = {
    "seed", "pre-seed", "pre_seed", "preseed",
    "angel", "strategic", "private", "initial",
    "pre_sale", "private_sale",
}


def get_api_key() -> str:
    key = os.environ.get("CRYPTORANK_API_KEY", "").strip()
    if not key:
        print("ERROR: CRYPTORANK_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)
    return key


def api_get(path: str, params: dict, api_key: str) -> dict:
    params = {**params, "api_key": api_key}
    url = f"{API_BASE}{path}?{urlencode(params)}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_all_rounds(api_key: str) -> list[dict]:
    date_from = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    all_rounds = []
    offset = 0
    limit  = 100

    while True:
        try:
            resp = api_get(
                "/currencies/fundraising",
                {"limit": limit, "offset": offset, "dateFrom": date_from},
                api_key,
            )
        except requests.HTTPError as e:
            print(f"  API error at offset {offset}: {e}")
            break

        data = resp.get("data", [])
        if not data:
            break

        all_rounds.extend(data)
        print(f"  Fetched {len(all_rounds)} rounds so far...")

        # Pagination: stop when we have fewer results than the page size
        meta  = resp.get("meta", {})
        total = meta.get("total") or meta.get("count") or 0
        if total and offset + limit >= total:
            break
        if len(data) < limit:
            break

        offset += limit
        time.sleep(0.5)

    return all_rounds


def normalize(item: dict) -> dict:
    """Flatten a CryptoRank fundraising item into our schema."""
    fr = item.get("fundraising") or item.get("fundRaising") or {}

    stage = (fr.get("stage") or item.get("stage") or "").lower().replace("-", "_").replace(" ", "_")

    # totalRaised can be {"USD": 5000000} or a plain number
    raised_raw = (
        fr.get("totalRaised")
        or fr.get("amount")
        or item.get("totalRaised")
        or 0
    )
    if isinstance(raised_raw, dict):
        raised_usd = raised_raw.get("USD") or raised_raw.get("usd") or 0
    else:
        raised_usd = float(raised_raw) if raised_raw else 0

    date = fr.get("date") or item.get("date") or item.get("launchDate") or ""
    if date:
        date = str(date)[:10]  # YYYY-MM-DD

    investors = fr.get("investors") or item.get("investors") or []
    investor_names = [
        inv.get("name", "") for inv in investors if isinstance(inv, dict)
    ][:5]

    links = item.get("links") or {}
    website     = item.get("website") or links.get("website") or ""
    twitter_url = item.get("twitter") or links.get("twitter") or ""
    if twitter_url and not twitter_url.startswith("http"):
        twitter_url = f"https://x.com/{twitter_url.lstrip('@')}"

    return {
        "id":           item.get("id"),
        "name":         item.get("name", ""),
        "slug":         item.get("slug", ""),
        "symbol":       item.get("symbol", ""),
        "category":     item.get("category") or "",
        "stage":        stage,
        "amount_usd":   raised_usd,
        "date":         date,
        "investors":    investor_names,
        "website":      website,
        "twitter_url":  twitter_url,
    }


def main():
    api_key = get_api_key()
    print(f"Fetching CryptoRank fundraising rounds (last {DAYS_BACK} days)...")

    raw = fetch_all_rounds(api_key)
    print(f"  {len(raw)} total rounds fetched")

    rounds = [normalize(r) for r in raw]

    seed_rounds = [
        r for r in rounds
        if not r["stage"] or r["stage"] in TARGET_STAGES
    ]
    print(f"  {len(seed_rounds)} seed/early-stage rounds kept")

    # Sort newest first
    seed_rounds.sort(key=lambda r: r["date"] or "", reverse=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count":        len(seed_rounds),
                "all_count":    len(rounds),
                "rounds":       seed_rounds,
            },
            f,
            indent=2,
        )
    print(f"Done. {len(seed_rounds)} rounds written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
