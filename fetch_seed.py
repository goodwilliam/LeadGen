"""
fetch_seed.py — Fetches seed funding news from RSS feeds for design agency lead gen.

Sources:
  1. TechCrunch  — https://techcrunch.com/feed/
  2. VentureBeat — https://venturebeat.com/feed/
  3. Crunchbase News — https://news.crunchbase.com/feed/

Filters articles that mention "seed" in the title, then:
  - Extracts company name and funding amount from headline
  - Scrapes the article page to find the company's own website
  - Scrapes the company website for LinkedIn company page + contact email

Uses a cache so each company site is only scraped once.

Usage: python fetch_seed.py
Output: data/seed.json
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

CONTACT_EMAIL = "something123@gmail.com"
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
SCRAPE_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

OUTPUT_PATH = Path("data/seed.json")
CACHE_PATH = Path("data/seed_enrichment_cache.json")

RSS_FEEDS = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "VentureBeat", "url": "https://venturebeat.com/feed/"},
    {"name": "Crunchbase News", "url": "https://news.crunchbase.com/feed/"},
]

FETCH_TIMEOUT = 20
ARTICLE_TIMEOUT = 10
ENRICH_TIMEOUT = 5
SLEEP_BETWEEN = 0.3

# News domains to skip when looking for company website links
NEWS_DOMAINS = {
    "techcrunch.com", "venturebeat.com", "crunchbase.com", "news.crunchbase.com",
    "bloomberg.com", "reuters.com", "wsj.com", "nytimes.com", "forbes.com",
    "businessinsider.com", "cnbc.com", "theverge.com", "wired.com",
    "ft.com", "economist.com", "inc.com", "fortune.com", "fastcompany.com",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "youtube.com",
    "instagram.com", "t.co", "bit.ly", "tinyurl.com", "ow.ly",
}


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


# ── RSS fetching ──────────────────────────────────────────────────────────────

def fetch_rss(feed_url: str, source_name: str) -> list[dict]:
    """Fetch and parse an RSS feed, returning seed-related articles."""
    print(f"  Fetching {source_name}...")
    try:
        r = requests.get(
            feed_url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
        r.raise_for_status()
        raw = r.text
    except requests.RequestException as e:
        print(f"    Error: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"    Parse error: {e}")
        return []

    articles = []
    # Handle both RSS 2.0 and Atom
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for item in items:
        def get_text(tag):
            el = item.find(tag)
            if el is None:
                el = item.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
            return (el.text or "").strip() if el is not None else ""

        title = get_text("title")
        link = get_text("link")
        pub_date_raw = get_text("pubDate") or get_text("published") or get_text("updated")
        description = get_text("description") or get_text("summary")

        if not title or not link:
            continue

        # Filter: must mention "seed" in title
        if not re.search(r'\bseed\b', title, re.IGNORECASE):
            continue

        # Parse date
        try:
            pub_date = parse_date(pub_date_raw)
        except Exception:
            pub_date = ""

        # Parse company name and amount from headline
        company_name, amount_str = parse_headline(title)

        articles.append({
            "source": source_name,
            "title": title,
            "article_url": link,
            "company": company_name,
            "amount_str": amount_str,
            "pub_date": pub_date,
            "website": "",
            "linkedin_url": "",
            "contact_email": "",
            "twitter_url": "",
        })

    print(f"    {len(articles)} seed articles found")
    return articles


def parse_date(raw: str) -> str:
    """Try to parse an RSS pubDate string to YYYY-MM-DD."""
    if not raw:
        return ""
    # Try RFC 2822 (RSS standard)
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: grab YYYY-MM-DD pattern
    m = re.search(r'(\d{4}-\d{2}-\d{2})', raw)
    return m.group(1) if m else ""


# Patterns for extracting company name and amount from seed funding headlines
# e.g. "Acme raises $5M seed round", "Acme secures $2.5M in seed funding"
HEADLINE_PATTERNS = [
    # "Company raises $Xm seed"
    re.compile(
        r'^(?P<company>[A-Z][^,$\n]{2,40?}?)\s+(?:raises?|secures?|lands?|closes?|announces?|gets?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])',
        re.IGNORECASE
    ),
    # "$Xm seed round for Company"
    re.compile(
        r'^\$(?P<amount>[\d.,]+\s*[MBKmb])\s+seed\s+(?:round|funding)\s+for\s+(?P<company>[A-Z][^,\n]{2,40})',
        re.IGNORECASE
    ),
]

STRIP_WORDS = re.compile(
    r'\b(raises?|secures?|lands?|closes?|announces?|gets?|nabs?|bags?|wins?)\b.*$',
    re.IGNORECASE
)


def parse_headline(title: str) -> tuple[str, str]:
    """Extract (company_name, amount_str) from a funding headline. Returns ('', '') if not found."""
    for pattern in HEADLINE_PATTERNS:
        m = pattern.search(title)
        if m:
            company = m.group("company").strip().rstrip(",").strip()
            amount = m.group("amount").strip()
            return company, f"${amount}"
    # Fallback: grab everything before the verb as company name
    m = STRIP_WORDS.search(title)
    if m:
        company = title[:m.start()].strip().rstrip(",").strip()
        # Look for amount anywhere in title
        amt = re.search(r'\$[\d.,]+\s*[MBKmb]', title, re.IGNORECASE)
        return company, amt.group(0) if amt else ""
    return "", ""


# ── Article scraping for company website ─────────────────────────────────────

def extract_company_website(article_url: str) -> str:
    """Fetch article page, find a company website link (non-news domain)."""
    if not article_url:
        return ""
    try:
        r = requests.get(
            article_url,
            headers={"User-Agent": SCRAPE_AGENT, "Accept": "text/html"},
            timeout=ARTICLE_TIMEOUT,
            allow_redirects=True,
        )
        html = r.text
    except Exception:
        return ""

    # Find all href links
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    for href in hrefs:
        if not href.startswith("http"):
            continue
        try:
            domain = urlparse(href).netloc.lower().lstrip("www.")
        except Exception:
            continue
        if any(domain == nd or domain.endswith("." + nd) for nd in NEWS_DOMAINS):
            continue
        # Skip common utility links
        if re.search(r'(google|apple|android|play\.google|apps\.apple|github\.com/[^/]+/[^/]+(?:/|$))', href, re.I):
            continue
        # Must look like a proper homepage (not a deep path with lots of slashes)
        path = urlparse(href).path
        if path.count("/") > 3:
            continue
        return href.split("?")[0].rstrip("/")

    return ""


# ── Site scraping for contact info ────────────────────────────────────────────

SKIP_EMAIL_PATTERNS = re.compile(
    r"noreply|no-reply|example\.|placeholder|@sentry|@github|\.png|\.jpg|\.svg",
    re.IGNORECASE,
)

CONTACT_PREFIXES = ["hello", "contact", "team", "hi", "gm", "info", "hey", "support"]


def scrape_contacts(url: str) -> dict:
    """Fetch a company website and extract LinkedIn, email, twitter."""
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

    li = re.search(r'linkedin\.com/company/([\w\-]+)', html)
    if li:
        result["linkedin_url"] = f"https://linkedin.com/company/{li.group(1)}"

    tw = re.search(r'(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,50})(?:["\'\/\s]|$)', html)
    if tw and tw.group(1).lower() not in ("share", "intent", "home", "search", "hashtag"):
        result["twitter_url"] = f"https://x.com/{tw.group(1)}"

    all_emails = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}', html)
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


# ── Enrichment ────────────────────────────────────────────────────────────────

def enrich_articles(articles: list[dict]) -> list[dict]:
    cache = load_cache()
    new_entries = 0

    for i, article in enumerate(articles, 1):
        key = article["article_url"]

        if key in cache:
            cached = cache[key]
            article["website"] = cached.get("website", "")
            article["linkedin_url"] = cached.get("linkedin_url", "")
            article["contact_email"] = cached.get("contact_email", "")
            article["twitter_url"] = cached.get("twitter_url", "")
            continue

        print(f"  [{i}/{len(articles)}] Enriching: {article['title'][:60]}...")

        # Step 1: find company website from article
        website = extract_company_website(article["article_url"])
        article["website"] = website
        time.sleep(SLEEP_BETWEEN)

        # Step 2: scrape website for contact info
        contacts = {"linkedin_url": "", "contact_email": "", "twitter_url": ""}
        if website:
            print(f"    Scraping {website[:60]}...")
            contacts = scrape_contacts(website)
            article["linkedin_url"] = contacts["linkedin_url"]
            article["contact_email"] = contacts["contact_email"]
            article["twitter_url"] = contacts["twitter_url"]
            time.sleep(SLEEP_BETWEEN)

        cache[key] = {
            "website": website,
            "linkedin_url": contacts["linkedin_url"],
            "contact_email": contacts["contact_email"],
            "twitter_url": contacts["twitter_url"],
        }
        new_entries += 1

        if new_entries % 10 == 0:
            save_cache(cache)

    if new_entries:
        save_cache(cache)
        print(f"  Enriched {new_entries} articles (cache now {len(cache)} entries)")
    else:
        print("  All articles already cached")

    return articles


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching seed funding RSS feeds...")
    all_articles = []
    seen_urls = set()

    for feed in RSS_FEEDS:
        articles = fetch_rss(feed["url"], feed["name"])
        for a in articles:
            if a["article_url"] not in seen_urls:
                seen_urls.add(a["article_url"])
                all_articles.append(a)
        time.sleep(0.5)

    print(f"\n{len(all_articles)} unique seed funding articles")

    if all_articles:
        print("\nEnriching articles with company contact info...")
        all_articles = enrich_articles(all_articles)

    # Sort by date descending
    all_articles.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    has_contact = sum(1 for a in all_articles if a["linkedin_url"] or a["contact_email"])
    print(f"{has_contact}/{len(all_articles)} articles have contact info")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "count": len(all_articles),
                "enriched_count": has_contact,
                "articles": all_articles,
            },
            f,
            indent=2,
        )
    print(f"\nDone. {len(all_articles)} seed articles written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
