"""
fetch_crypto_top.py — Fetches top 1000 coins by market cap from CoinGecko.

For the top 300, it enriches each coin with contact info scraped from their
website (LinkedIn company page, contact email, Twitter/X). Results are cached
so each site is only scraped once.

Uses the free CoinGecko API (no auth needed for basic endpoints).

Usage: python fetch_crypto_top.py
Output: data/crypto_top.json
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

OUTPUT_PATH = Path("data/crypto_top.json")
CACHE_PATH = Path("data/crypto_top_enrichment_cache.json")

TOTAL_COINS = 1000          # fetch top 1000 by market cap
ENRICH_TOP_N = 300          # only scrape websites for top N
COINS_PER_PAGE = 100        # CoinGecko max per page
COINS_ID_SLEEP = 6.5        # seconds between /coins/{id} calls (free tier: ~10/min)
ENRICH_TIMEOUT = 5          # seconds per site fetch
ENRICH_SLEEP = 0.2          # seconds between site scrapes


# ── Cache ─────────────────────────────────────────────────────────────────────

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


# ── Site scraping ─────────────────────────────────────────────────────────────

SKIP_EMAIL_PATTERNS = re.compile(
    r"noreply|no-reply|example\.|placeholder|@sentry|@github|\.png|\.jpg|\.svg",
    re.IGNORECASE,
)

CONTACT_PREFIXES = ["hello", "contact", "team", "hi", "gm", "info", "hey", "support"]


def scrape_contacts(url: str) -> dict:
    """Fetch a coin's website and extract LinkedIn, email, twitter."""
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
    tw = re.search(r'(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,50})(?:["\'\/\s]|$)', html)
    if tw and tw.group(1).lower() not in ("share", "intent", "home", "search", "hashtag"):
        result["twitter_url"] = f"https://x.com/{tw.group(1)}"

    # Emails — prefer contact-style ones
    all_emails = re.findall(
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}', html
    )
    all_emails = [e for e in all_emails if not SKIP_EMAIL_PATTERNS.search(e)]

    contact_email = ""
    for prefix in CONTACT_PREFIXES:
        for e in all_emails:
            if e.lower().startswith(prefix + "@") or e.lower().startswith(prefix + "."):
                contact_email = e
                break
        if contact_email:
            break
    if not contact_email:
        for e in all_emails:
            if not re.search(r"@(gmail|yahoo|hotmail|outlook|proton)", e, re.I):
                contact_email = e
                break

    result["contact_email"] = contact_email
    return result


# ── CoinGecko API ─────────────────────────────────────────────────────────────

def fetch_markets_page(page: int) -> list[dict]:
    """Fetch one page of top coins by market cap."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": COINS_PER_PAGE,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "24h,7d",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"  Warning page {page}: {e}")
        return []


def fetch_coin_website(coin_id: str) -> str:
    """Fetch homepage URL from /coins/{id} endpoint."""
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "market_data": "false",
                    "community_data": "false", "developer_data": "false"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code == 429:
            print("  Rate limited on /coins/{id}, sleeping 30s...")
            time.sleep(30)
            return ""
        r.raise_for_status()
        data = r.json()
        links = data.get("links", {})
        homepages = links.get("homepage", [])
        for hp in homepages:
            if hp and hp.strip():
                return hp.strip()
    except requests.RequestException:
        pass
    return ""


def fetch_all_markets() -> list[dict]:
    """Fetch top TOTAL_COINS coins across multiple pages."""
    coins = []
    total_pages = TOTAL_COINS // COINS_PER_PAGE
    for page in range(1, total_pages + 1):
        print(f"  Fetching markets page {page}/{total_pages}...")
        results = fetch_markets_page(page)
        if not results:
            break
        for coin in results:
            change_24h = coin.get("price_change_percentage_24h") or 0
            change_7d = coin.get("price_change_percentage_7d_in_currency") or 0
            coins.append({
                "id": coin.get("id", ""),
                "name": coin.get("name", ""),
                "symbol": (coin.get("symbol", "") or "").upper(),
                "market_cap_rank": coin.get("market_cap_rank"),
                "market_cap": coin.get("market_cap") or 0,
                "price_usd": float(coin.get("current_price") or 0),
                "change_24h": round(float(change_24h), 2),
                "change_7d": round(float(change_7d), 2),
                "coingecko_url": f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                "website": "",
                "linkedin_url": "",
                "contact_email": "",
                "twitter_url": "",
            })
        time.sleep(1.2)  # respect rate limit between pages
    return coins


def enrich_top_coins(coins: list[dict]) -> list[dict]:
    """Fetch websites for top ENRICH_TOP_N coins, then scrape for contacts."""
    cache = load_cache()
    to_enrich = coins[:ENRICH_TOP_N]
    new_entries = 0

    for i, coin in enumerate(to_enrich, 1):
        coin_id = coin["id"]

        # Check cache first
        if coin_id in cache:
            cached = cache[coin_id]
            coin["website"] = cached.get("website", "")
            coin["linkedin_url"] = cached.get("linkedin_url", "")
            coin["contact_email"] = cached.get("contact_email", "")
            coin["twitter_url"] = cached.get("twitter_url", "")
            continue

        # Fetch website URL
        print(f"  [{i}/{len(to_enrich)}] Fetching website for {coin['name']}...")
        website = fetch_coin_website(coin_id)
        coin["website"] = website
        time.sleep(COINS_ID_SLEEP)

        # Scrape contacts from website
        if website:
            print(f"    Scraping {website[:60]}...")
            contacts = scrape_contacts(website)
            coin["linkedin_url"] = contacts["linkedin_url"]
            coin["contact_email"] = contacts["contact_email"]
            coin["twitter_url"] = contacts.get("twitter_url", "") or coin["twitter_url"]
            time.sleep(ENRICH_SLEEP)
        else:
            contacts = {"linkedin_url": "", "contact_email": "", "twitter_url": ""}

        cache[coin_id] = {
            "website": website,
            "linkedin_url": contacts["linkedin_url"],
            "contact_email": contacts["contact_email"],
            "twitter_url": contacts.get("twitter_url", ""),
        }
        new_entries += 1

        # Save cache periodically
        if new_entries % 10 == 0:
            save_cache(cache)

    if new_entries:
        save_cache(cache)
        print(f"  Enriched {new_entries} new coins (cache now {len(cache)} entries)")
    else:
        print("  All top coins already cached")

    return coins


def main():
    print(f"Fetching top {TOTAL_COINS} coins by market cap...")
    coins = fetch_all_markets()
    print(f"  {len(coins)} coins fetched")

    print(f"\nEnriching top {ENRICH_TOP_N} coins with contact info...")
    coins = enrich_top_coins(coins)

    has_contact = sum(1 for c in coins if c["linkedin_url"] or c["contact_email"])
    print(f"  {has_contact}/{len(coins)} coins have contact info")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "count": len(coins),
                "enriched_count": has_contact,
                "coins": coins,
            },
            f,
            indent=2,
        )
    print(f"\nDone. {len(coins)} coins written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
