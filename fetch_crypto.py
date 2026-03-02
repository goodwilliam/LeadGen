"""
fetch_crypto.py — Fetches crypto growth signals for design agency lead gen.

Sources:
  1. DeFiLlama — protocols with strong 7-day TVL growth (active, funded projects)
  2. CoinGecko  — trending coins (market attention = outreach window)

Usage: python fetch_crypto.py
Output: data/crypto.json
"""

import json
import time
from datetime import datetime
from pathlib import Path

import requests

CONTACT_EMAIL = "something123@gmail.com"
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
OUTPUT_PATH = Path("data/crypto.json")

# DeFiLlama filters
MIN_TVL = 500_000        # $500K minimum TVL — small enough to still need design help
MIN_7D_GROWTH = 15.0    # 15%+ 7-day TVL growth

# Skip pure infrastructure categories that rarely need brand/product design
SKIP_CATEGORIES = {"CEX", "Chain", "Bridge", "Oracle", "RWA", "Indexes"}


def fetch_defillama() -> list[dict]:
    print("Fetching DeFiLlama protocols...")
    try:
        r = requests.get(
            "https://api.llama.fi/protocols",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        protocols = r.json()
    except requests.RequestException as e:
        print(f"  DeFiLlama error: {e}")
        return []

    results = []
    for p in protocols:
        tvl = p.get("tvl") or 0
        change_7d = p.get("change_7d") or 0
        category = p.get("category", "") or ""

        if tvl < MIN_TVL:
            continue
        if change_7d < MIN_7D_GROWTH:
            continue
        if category in SKIP_CATEGORIES:
            continue

        twitter = p.get("twitter", "") or ""
        twitter_url = f"https://twitter.com/{twitter}" if twitter else ""

        results.append({
            "source": "defillama",
            "name": p.get("name", ""),
            "url": p.get("url", "") or "",
            "twitter_url": twitter_url,
            "category": category,
            "chain": p.get("chain", "") or "",
            "chains": p.get("chains", []),
            "tvl": tvl,
            "change_1d": round(p.get("change_1d") or 0, 2),
            "change_7d": round(change_7d, 2),
            "slug": p.get("slug", "") or "",
            "defillama_url": f"https://defillama.com/protocol/{p.get('slug', '')}",
        })

    results.sort(key=lambda x: x["change_7d"], reverse=True)
    print(f"  {len(results)} protocols with >{MIN_7D_GROWTH}% 7d TVL growth")
    return results[:150]


def fetch_coingecko_trending() -> list[dict]:
    print("Fetching CoinGecko trending...")
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"  CoinGecko error: {e}")
        return []

    results = []
    for item in data.get("coins", []):
        coin = item.get("item", {})
        coin_data = coin.get("data", {})

        # 24h price change — pick USD value from the nested dict
        pct_24h_obj = coin_data.get("price_change_percentage_24h", {})
        pct_24h = 0.0
        if isinstance(pct_24h_obj, dict):
            pct_24h = round(float(pct_24h_obj.get("usd", 0) or 0), 2)
        elif isinstance(pct_24h_obj, (int, float)):
            pct_24h = round(float(pct_24h_obj), 2)

        slug = coin.get("slug", coin.get("id", ""))
        results.append({
            "source": "coingecko",
            "name": coin.get("name", ""),
            "symbol": (coin.get("symbol", "") or "").upper(),
            "market_cap_rank": coin.get("market_cap_rank"),
            "price_usd": float(coin_data.get("price", 0) or 0),
            "change_24h": pct_24h,
            "market_cap": coin_data.get("market_cap", ""),
            "url": f"https://www.coingecko.com/en/coins/{slug}",
            "thumb": coin.get("thumb", ""),
            "score": coin.get("score", 0),
        })

    print(f"  {len(results)} trending coins")
    return results


def main():
    defi = fetch_defillama()
    time.sleep(1)
    trending = fetch_coingecko_trending()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "defi_count": len(defi),
                "trending_count": len(trending),
                "defi_protocols": defi,
                "trending_coins": trending,
            },
            f,
            indent=2,
        )
    print(f"\nDone. {len(defi)} DeFi protocols + {len(trending)} trending coins written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
