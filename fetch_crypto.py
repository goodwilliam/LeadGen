"""
fetch_crypto.py — Fetches crypto growth signals for design agency lead gen.

Sources:
  1. DeFiLlama — protocols with strong 7-day TVL growth (active, funded projects)
  2. CoinGecko  — trending coins (market attention = outreach window)

Also enriches DeFiLlama protocols by scraping their website for:
  - LinkedIn company page URL
  - Contact email (hello@, contact@, team@, etc.)
  - Twitter/X URL (bonus)

Uses a cache (data/crypto_enrichment_cache.json) so each site is only
scraped once — subsequent runs only fetch new protocols.

Usage: python fetch_crypto.py
Output: data/crypto.json
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

CONTACT_EMAIL = "something123@gmail.com"
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
SCRAPE_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

OUTPUT_PATH = Path("data/crypto.json")
CACHE_PATH  = Path("data/crypto_enrichment_cache.json")

# DeFiLlama filters
MIN_TVL = 500_000
MIN_7D_GROWTH = 15.0
SKIP_CATEGORIES = {"CEX", "Chain", "Bridge", "Oracle", "RWA", "Indexes"}

# Site scraping
ENRICH_TIMEOUT = 4       # seconds per site fetch
ENRICH_SLEEP   = 0.15    # seconds between site fetches


# ── Enrichment cache ──────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


# ── Site scraping for contact info ────────────────────────────────────────────

SKIP_EMAIL_PATTERNS = re.compile(
    r"noreply|no-reply|example\.|placeholder|@sentry|@github|\.png|\.jpg|\.svg",
    re.IGNORECASE,
)

CONTACT_PREFIXES = ["hello", "contact", "team", "hi", "gm", "info", "hey", "support"]


def scrape_contacts(url: str) -> dict:
    """Fetch a protocol's website and extract LinkedIn, email, twitter."""
    result = {"linkedin_url": "", "contact_email": "", "twitter_url": ""}
    if not url:
        return result
    try:
        r = requests.get(
            url,
            headers={"User-Agent": SCRAPE_AGENT, "Accept": "text/html"},
            timeout=ENRICH_TIMEOUT,
            allow_redirects=True,
        )
        html = r.text
    except Exception:
        return result

    # LinkedIn company page
    li = re.search(r'linkedin\.com/company/([\w\-]+)', html)
    if li:
        result["linkedin_url"] = f"https://linkedin.com/company/{li.group(1)}"

    # Twitter / X
    tw = re.search(r'(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,50})(?:["\'/\s]|$)', html)
    if tw and tw.group(1).lower() not in ("share", "intent", "home", "search", "hashtag"):
        result["twitter_url"] = f"https://x.com/{tw.group(1)}"

    # Emails — prefer contact-style ones
    all_emails = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}', html
    )
    # Filter junk
    all_emails = [e for e in all_emails if not SKIP_EMAIL_PATTERNS.search(e)]

    contact_email = ""
    for prefix in CONTACT_PREFIXES:
        for e in all_emails:
            if e.lower().startswith(prefix + "@") or e.lower().startswith(prefix + "."):
                contact_email = e
                break
        if contact_email:
            break
    # Fallback: first non-junk email that isn't a common domain
    if not contact_email:
        for e in all_emails:
            if not re.search(r"@(gmail|yahoo|hotmail|outlook|proton)", e, re.I):
                contact_email = e
                break

    result["contact_email"] = contact_email
    return result


def enrich_protocols(protocols: list[dict]) -> list[dict]:
    cache = load_cache()
    enriched_count = 0

    for p in protocols:
        key = p.get("slug") or p.get("url") or p["name"]
        if key in cache:
            p.update(cache[key])
            continue

        url = p.get("url", "")
        print(f"  Enriching {p['name']} ({url[:50] if url else 'no url'})")
        contacts = scrape_contacts(url)
        cache[key] = contacts
        p.update(contacts)
        enriched_count += 1
        time.sleep(ENRICH_SLEEP)

    if enriched_count:
        save_cache(cache)
        print(f"  Enriched {enriched_count} new protocols (cache now {len(cache)} entries)")
    else:
        print("  All protocols already cached")

    return protocols


# ── DeFiLlama ─────────────────────────────────────────────────────────────────

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

        results.append({
            "source": "defillama",
            "name": p.get("name", ""),
            "url": p.get("url", "") or "",
            "twitter_url": f"https://x.com/{p['twitter']}" if p.get("twitter") else "",
            "linkedin_url": "",
            "contact_email": "",
            "category": category,
            "chain": p.get("chain", "") or "",
            "tvl": tvl,
            "change_1d": round(p.get("change_1d") or 0, 2),
            "change_7d": round(change_7d, 2),
            "slug": p.get("slug", "") or "",
            "defillama_url": f"https://defillama.com/protocol/{p.get('slug', '')}",
        })

    results.sort(key=lambda x: x["change_7d"], reverse=True)
    results = results[:150]
    print(f"  {len(results)} protocols with >{MIN_7D_GROWTH}% 7d TVL growth")
    return results


# ── CoinGecko ─────────────────────────────────────────────────────────────────

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
            "score": coin.get("score", 0),
            "twitter_url": "",
            "linkedin_url": "",
            "contact_email": "",
        })

    print(f"  {len(results)} trending coins")
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    defi = fetch_defillama()
    print("Enriching DeFiLlama protocols with contact info...")
    defi = enrich_protocols(defi)

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
    print(f"\nDone. {len(defi)} DeFi + {len(trending)} trending → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
