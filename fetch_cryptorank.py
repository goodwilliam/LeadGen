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

# Only keep raises from the last N days
DAYS_BACK = 90
CUTOFF_MS = None  # set in main()

# Seed / early-stage rounds we care about
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
    all_rounds = []
    offset = 0
    limit  = 100

    while True:
        try:
            resp = api_get(
                "/currencies/fundraising",
                {"limit": limit, "offset": offset},
                api_key,
            )
        except requests.HTTPError as e:
            print(f"  API error at offset {offset}: {e}")
            break

        # Debug: print top-level keys on first page so we can see the shape
        if offset == 0:
            print(f"  Response top-level keys: {list(resp.keys())}")
            # Try to find the data array under any common key
            for k, v in resp.items():
                if isinstance(v, list):
                    print(f"  Found list under '{k}': {len(v)} items")
                    if v:
                        print(f"  First item keys: {list(v[0].keys()) if isinstance(v[0], dict) else type(v[0])}")

        # Try to find the data array — CryptoRank may use different keys
        data = (
            resp.get("data")
            or resp.get("currencies")
            or resp.get("rounds")
            or resp.get("items")
            or []
        )

        if not data:
            print(f"  No data found at offset {offset} — stopping")
            break

        all_rounds.extend(data)
        print(f"  Fetched {len(all_rounds)} rounds so far...")

        meta  = resp.get("meta", {})
        total = meta.get("total") or meta.get("count") or 0
        if total and offset + limit >= total:
            break
        if len(data) < limit:
            break

        offset += limit
        time.sleep(0.5)

    return all_rounds


def ms_to_date(ms) -> str:
    """Convert millisecond timestamp to YYYY-MM-DD string."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ms)[:10]


def normalize(item: dict) -> dict:
    """Flatten a CryptoRank fundraising item into our schema."""
    # Fundraising data may be nested or flat
    fr = item.get("fundraising") or item.get("fundRaising") or {}

    # Stage — can be nested or top-level
    stage = (
        fr.get("stage")
        or item.get("stage")
        or ""
    ).lower().replace("-", "_").replace(" ", "_")

    # Date — millisecond timestamp (nested or top-level)
    date_ms = fr.get("date") or item.get("date") or 0
    date    = ms_to_date(date_ms)

    # Amount raised — field is "raise" not "totalRaised"
    raise_raw = (
        fr.get("raise")
        or fr.get("totalRaised")
        or item.get("raise")
        or item.get("totalRaised")
        or 0
    )
    if isinstance(raise_raw, dict):
        amount_usd = raise_raw.get("USD") or raise_raw.get("usd") or 0
    else:
        amount_usd = float(raise_raw) if raise_raw else 0

    investors = fr.get("investors") or item.get("investors") or []
    investor_names = [
        inv.get("name", "") for inv in investors if isinstance(inv, dict)
    ][:5]

    links       = item.get("links") or {}
    website     = item.get("website") or links.get("website") or ""
    twitter_url = item.get("twitter") or links.get("twitter") or ""
    if twitter_url and not twitter_url.startswith("http"):
        twitter_url = f"https://x.com/{twitter_url.lstrip('@')}"

    return {
        "id":          item.get("id"),
        "name":        item.get("name", ""),
        "slug":        item.get("slug", ""),
        "symbol":      item.get("symbol", ""),
        "category":    item.get("category") or "",
        "stage":       stage,
        "amount_usd":  amount_usd,
        "date":        date,
        "investors":   investor_names,
        "website":     website,
        "twitter_url": twitter_url,
    }


def main():
    api_key = get_api_key()
    cutoff  = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    print(f"Fetching CryptoRank fundraising rounds (last {DAYS_BACK} days, since {cutoff.strftime('%Y-%m-%d')})...")

    raw = fetch_all_rounds(api_key)
    print(f"  {len(raw)} total rounds fetched")

    if not raw:
        print("  WARNING: No rounds returned — check API key and endpoint response above")

    rounds = [normalize(r) for r in raw]

    # Filter to seed/early stage and within date window
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    seed_rounds = [
        r for r in rounds
        if (not r["stage"] or r["stage"] in TARGET_STAGES)
        and (not r["date"] or r["date"] >= cutoff_str)
    ]
    print(f"  {len(seed_rounds)} seed/early-stage rounds in date window")

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
