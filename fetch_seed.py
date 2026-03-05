"""
fetch_seed.py — Fetches seed funding news from RSS feeds for design agency lead gen.

Sources:
  1-9. Tech RSS feeds (TechCrunch, VentureBeat, Crunchbase, tech.eu, etc.)
  10-14. Google Alerts — real-time seed funding mentions across the web

For each article:
  1. Validates via Jina AI reader that it's an actual funding raise (not roundup/analysis)
  2. Extracts company name + amount from clean article text
  3. Scrapes company website for LinkedIn + contact email
  4. Deduplicates by company name
  5. Filters out large well-known companies

Uses a cache so each article is only processed once.

Usage: python fetch_seed.py
Output: data/seed.json
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

import feedparser
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
    {"name": "tech.eu", "url": "https://tech.eu/feed/"},
    {"name": "BetaKit", "url": "https://betakit.com/feed/"},
    {"name": "SiliconAngle", "url": "https://siliconangle.com/feed/"},
    {"name": "GeekWire", "url": "https://www.geekwire.com/feed/"},
    {"name": "PR Newswire", "url": "https://www.prnewswire.com/rss/news-releases-list.rss"},
    {"name": "StartupNation", "url": "https://startupnation.com/feed/"},
    # Google Alerts — pre-filtered by Google for seed funding terms
    {"name": "Google Alerts", "url": "https://www.google.com/alerts/feeds/07102744851571677176/16829063131072156401", "pre_filtered": True},
    {"name": "Google Alerts", "url": "https://www.google.com/alerts/feeds/07102744851571677176/13906430425197458915", "pre_filtered": True},
    {"name": "Google Alerts", "url": "https://www.google.com/alerts/feeds/07102744851571677176/18171680575817386135", "pre_filtered": True},
    {"name": "Google Alerts", "url": "https://www.google.com/alerts/feeds/07102744851571677176/17815328040355875702", "pre_filtered": True},
    {"name": "Google Alerts", "url": "https://www.google.com/alerts/feeds/07102744851571677176/18059503186406661259", "pre_filtered": True},
]

FETCH_TIMEOUT = 20
ARTICLE_TIMEOUT = 10
ENRICH_TIMEOUT = 5
JINA_TIMEOUT = 15
SLEEP_BETWEEN = 0.3

# Well-known large companies — not our target clients (seed/pre-seed startups)
LARGE_COMPANY_BLACKLIST = {
    "openai", "anthropic", "nvidia", "google", "microsoft", "amazon", "apple",
    "meta", "tesla", "spacex", "uber", "airbnb", "stripe", "palantir", "salesforce",
    "oracle", "ibm", "intel", "amd", "qualcomm", "arm", "databricks", "snowflake",
    "coinbase", "binance", "ripple", "robinhood", "instacart", "doordash", "lyft",
    "shopify", "atlassian", "twilio", "zendesk", "hubspot", "notion", "figma",
    "canva", "cloudflare", "fastly", "vercel", "netlify", "supabase",
}

# News/utility domains to skip when looking for company website links
NEWS_DOMAINS = {
    "techcrunch.com", "venturebeat.com", "crunchbase.com", "news.crunchbase.com",
    "bloomberg.com", "reuters.com", "wsj.com", "nytimes.com", "forbes.com",
    "businessinsider.com", "cnbc.com", "theverge.com", "wired.com",
    "ft.com", "economist.com", "inc.com", "fortune.com", "fastcompany.com",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "youtube.com",
    "instagram.com", "t.co", "bit.ly", "tinyurl.com", "ow.ly",
    "gmpg.org",  # WordPress XFN metadata link — not a company site
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

SEED_KEYWORDS = re.compile(
    r'\bseed\b|\bpre.?seed\b|\braises?\b|\bfunding\b|\binvest',
    re.IGNORECASE
)


def strip_html(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_google_url(url: str) -> str:
    """Unwrap Google Alerts redirect URLs to get the real article URL."""
    m = re.search(r'url=([^&]+)', url)
    return unquote(m.group(1)) if m else url


def fetch_rss(feed_url: str, source_name: str, pre_filtered: bool = False) -> list[dict]:
    """Fetch and parse an RSS/Atom feed using feedparser (handles malformed XML)."""
    print(f"  Fetching {source_name}...")
    try:
        feed = feedparser.parse(
            feed_url,
            request_headers={"User-Agent": USER_AGENT},
        )
    except Exception as e:
        print(f"    Error: {e}")
        return []

    if feed.bozo and not feed.entries:
        print(f"    Parse error: {feed.bozo_exception}")
        return []

    articles = []
    for entry in feed.entries:
        title = strip_html(entry.get("title", "").strip())

        # Get link — handle Google Alerts redirect
        link = entry.get("link", "")
        if "google.com/url" in link or "google.com/alerts" in link:
            link = extract_google_url(link)

        # feedparser also exposes links list for Atom
        if not link:
            for lk in entry.get("links", []):
                href = lk.get("href", "")
                if href:
                    link = extract_google_url(href) if "google.com" in href else href
                    break

        description = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
        description = strip_html(description)

        pub_date_raw = entry.get("published", "") or entry.get("updated", "")

        if not title or not link:
            continue

        # Google Alerts feeds are pre-filtered — accept all
        # For general feeds, check title + description for funding keywords
        if not pre_filtered:
            haystack = title + " " + description
            if not SEED_KEYWORDS.search(haystack):
                continue

        try:
            pub_date = parse_date(pub_date_raw)
        except Exception:
            pub_date = ""

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
    m = re.search(r'(\d{4}-\d{2}-\d{2})', raw)
    return m.group(1) if m else ""


# ── Company name extraction from headlines ────────────────────────────────────

HEADLINE_PATTERNS = [
    # "Company raises $Xm seed"
    re.compile(
        r'^(?P<company>[A-Z][^,$\n]{2,50?}?)\s+(?:raises?|secures?|lands?|closes?|announces?|gets?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])',
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

# Words that are descriptors/adjectives, not part of the company's proper name.
# Used to strip prefixes like "Belgian logistics startup" from "Belgian logistics startup Vectrix".
DESCRIPTOR_WORDS = {
    # Articles / connectives
    'a', 'an', 'the',
    # Company type words
    'startup', 'company', 'firm', 'platform', 'app', 'venture', 'scaleup', 'scale-up',
    # Industry descriptors
    'ai', 'saas', 'b2b', 'b2c', 'web3', 'crypto', 'defi', 'nft', 'blockchain',
    'fintech', 'healthtech', 'edtech', 'proptech', 'insurtech', 'cleantech', 'deeptech',
    'biotech', 'medtech', 'agtech', 'legaltech', 'regtech', 'logistics', 'ecommerce',
    'cybersecurity', 'security', 'productivity', 'data', 'analytics', 'cloud',
    'enterprise', 'consumer', 'mobile', 'gaming', 'media', 'tech', 'software',
    'hardware', 'digital', 'social', 'global', 'open-source',
    # Stage descriptors
    'early-stage', 'early', 'stage', 'late',
    # Geographic adjectives (lowercase = descriptor context)
    'american', 'european', 'british', 'german', 'french', 'dutch', 'belgian',
    'swedish', 'norwegian', 'finnish', 'danish', 'spanish', 'italian', 'portuguese',
    'polish', 'indian', 'chinese', 'japanese', 'korean', 'singaporean', 'israeli',
    'canadian', 'australian', 'nordic', 'african', 'latin', 'us', 'uk', 'eu',
    'new',  # "new" is almost never part of a company name in this context
}


def clean_company_name(name: str) -> str:
    """Strip leading descriptor words to isolate the actual company name.

    e.g. "Belgian logistics startup Vectrix" → "Vectrix"
         "AI-powered platform Acme Corp"     → "Acme Corp"
         "OpenAI"                            → "OpenAI" (unchanged)
    """
    name = name.strip().rstrip(",").strip()
    words = name.split()
    if len(words) <= 2:
        return name  # Short enough to be correct as-is

    # Walk forward and find the last descriptor word index
    last_descriptor_idx = -1
    for i, word in enumerate(words):
        w = word.lower().strip('.,;:-/()')
        if w in DESCRIPTOR_WORDS:
            last_descriptor_idx = i
        else:
            # Stop scanning at the first non-descriptor word
            break

    if last_descriptor_idx >= 0 and last_descriptor_idx < len(words) - 1:
        candidate = ' '.join(words[last_descriptor_idx + 1:])
        # Only accept if it starts with a capital letter (proper noun)
        if candidate and candidate[0].isupper():
            return candidate

    return name


def parse_headline(title: str) -> tuple[str, str]:
    """Extract (company_name, amount_str) from a funding headline."""
    for pattern in HEADLINE_PATTERNS:
        m = pattern.search(title)
        if m:
            company = clean_company_name(m.group("company"))
            amount = m.group("amount").strip()
            return company, f"${amount}"
    # Fallback: grab everything before the verb
    m = STRIP_WORDS.search(title)
    if m:
        company = clean_company_name(title[:m.start()])
        amt = re.search(r'\$[\d.,]+\s*[MBKmb]', title, re.IGNORECASE)
        return company, amt.group(0) if amt else ""
    return "", ""


# ── Jina AI reader — article validation & better extraction ───────────────────

# Confirms the article is about a specific company raising money
# Allows a few words between the verb and amount/keyword
RAISE_CONFIRM_RE = re.compile(
    r'(?:raises?|raised|secures?|secured|closes?|closed|lands?|landed|bags?|bagged|nabs?|nabbed|wins?|won|announces?|announced)\s+'
    r'(?:\w+\s+){0,4}?(?:\$[\d.,]+\s*[MmBbKk]|seed\s+(?:round|funding)|pre.?seed|funding\s+round|investment\s+round)',
    re.IGNORECASE
)

# Signals the article is NOT a raise announcement
NOT_RAISE_RE = re.compile(
    r'\b(?:roundup|class\s+action|lawsuit|settlement|lays?\s*off|layoffs?|cuts?\s+jobs?|'
    r'top\s+\d+\s+startups?|best\s+startups?|\d+\s+startups?\s+to\s+watch|'
    r'weekly\s+(?:funding|roundup)|funding\s+roundup|monthly\s+(?:funding|roundup)|'
    r'how\s+to\s+raise|what\s+investors\s+(?:want|look)|says?\s+(?:nvidia|openai|google|microsoft))\b',
    re.IGNORECASE
)

# Try to extract company name + amount from clean article text (Jina markdown)
JINA_COMPANY_PATTERNS = [
    # "[Company] has raised $X in seed funding" — start of sentence
    re.compile(
        r'(?:^|\n|\. )(?P<company>[A-Z][A-Za-z0-9][A-Za-z0-9 \-]{0,35}?)\s+(?:has\s+)?(?:raised?|secured?|closed?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])',
        re.MULTILINE
    ),
    # "[Company], a [descriptor], raised $X"
    re.compile(
        r'(?:^|\n|\. )(?P<company>[A-Z][A-Za-z0-9][A-Za-z0-9 \-]{0,35}?),\s+[a-z][^.]{0,80}?,\s+(?:has\s+)?(?:raised?|secured?|closed?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])',
        re.MULTILINE
    ),
]


def jina_fetch(url: str) -> str:
    """Fetch clean article text via Jina AI reader (free, no auth required)."""
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
            timeout=JINA_TIMEOUT,
        )
        if r.status_code == 200:
            return r.text[:10000]  # cap — enough for validation
    except Exception:
        pass
    return ""


def validate_and_extract(article: dict, text: str) -> tuple[bool, str, str]:
    """
    Use Jina-fetched article text to:
      1. Confirm this is an actual funding raise (not a roundup/analysis)
      2. Extract a better company name and amount if possible

    Returns: (is_valid, company_name, amount_str)
    If text is empty (Jina failed), keep the article and use existing values.
    """
    if not text:
        return True, article["company"], article["amount_str"]

    # Hard reject: looks like roundup, lawsuit, layoff news, etc.
    if NOT_RAISE_RE.search(text):
        return False, "", ""

    # Must contain raise language
    if not RAISE_CONFIRM_RE.search(text):
        return False, "", ""

    # Try to get a better company name from the clean text
    company = article["company"]
    amount = article["amount_str"]

    for pattern in JINA_COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            candidate = clean_company_name(m.group("company").strip())
            if len(candidate) >= 2:
                company = candidate
                if m.group("amount"):
                    amount = f"${m.group('amount').strip()}"
                break

    return True, company, amount


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
        if re.search(r'(google|apple|android|play\.google|apps\.apple|github\.com/[^/]+/[^/]+(?:/|$))', href, re.I):
            continue
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
    """Validate articles via Jina AI, scrape contact info, return only valid raises."""
    cache = load_cache()
    new_entries = 0
    valid_articles = []

    for i, article in enumerate(articles, 1):
        key = article["article_url"]

        if key in cache:
            cached = cache[key]
            # Entries explicitly marked invalid are skipped
            if cached.get("valid") is False:
                continue
            # Restore cached enrichment data
            article["company"] = cached.get("company") or article["company"]
            article["amount_str"] = cached.get("amount_str") or article["amount_str"]
            article["website"] = cached.get("website", "")
            article["linkedin_url"] = cached.get("linkedin_url", "")
            article["contact_email"] = cached.get("contact_email", "")
            article["twitter_url"] = cached.get("twitter_url", "")
            valid_articles.append(article)
            continue

        print(f"  [{i}/{len(articles)}] Validating: {article['title'][:65]}...")

        # Step 1: Jina AI — validate it's a real raise & improve extraction
        jina_text = jina_fetch(article["article_url"])
        is_valid, company, amount = validate_and_extract(article, jina_text)
        time.sleep(SLEEP_BETWEEN)

        if not is_valid:
            print(f"    SKIP — not a funding raise")
            cache[key] = {"valid": False}
            save_cache(cache)
            new_entries += 1
            continue

        article["company"] = company or article["company"]
        article["amount_str"] = amount or article["amount_str"]

        # Step 2: find company website from article page
        website = extract_company_website(article["article_url"])
        article["website"] = website
        time.sleep(SLEEP_BETWEEN)

        # Step 3: scrape company website for contact info
        contacts = {"linkedin_url": "", "contact_email": "", "twitter_url": ""}
        if website:
            print(f"    Scraping {website[:60]}...")
            contacts = scrape_contacts(website)
            article["linkedin_url"] = contacts["linkedin_url"]
            article["contact_email"] = contacts["contact_email"]
            article["twitter_url"] = contacts["twitter_url"]
            time.sleep(SLEEP_BETWEEN)

        cache[key] = {
            "valid": True,
            "company": article["company"],
            "amount_str": article["amount_str"],
            "website": website,
            "linkedin_url": contacts["linkedin_url"],
            "contact_email": contacts["contact_email"],
            "twitter_url": contacts["twitter_url"],
        }
        new_entries += 1
        save_cache(cache)
        valid_articles.append(article)

    if new_entries:
        print(f"  Processed {new_entries} new articles (cache now {len(cache)} entries)")
    else:
        print("  All articles already cached")

    return valid_articles


def normalize_company(name: str) -> str:
    """Normalize company name for deduplication (strip punctuation, lowercase)."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching seed funding RSS feeds...")
    all_articles = []
    seen_urls = set()

    for feed in RSS_FEEDS:
        articles = fetch_rss(feed["url"], feed["name"], pre_filtered=feed.get("pre_filtered", False))
        for a in articles:
            if a["article_url"] not in seen_urls:
                seen_urls.add(a["article_url"])
                all_articles.append(a)
        time.sleep(0.5)

    print(f"\n{len(all_articles)} unique seed funding articles from RSS")

    if all_articles:
        print("\nValidating and enriching articles...")
        all_articles = enrich_articles(all_articles)

    print(f"{len(all_articles)} articles passed validation")

    # Deduplicate by company name — keep the entry with most contact info
    seen_companies: dict[str, dict] = {}
    for article in all_articles:
        company = article.get("company", "").strip()
        norm = normalize_company(company)

        # Skip blacklisted large companies and entries with no company name
        if not norm or norm in LARGE_COMPANY_BLACKLIST:
            continue

        if norm not in seen_companies:
            seen_companies[norm] = article
        else:
            # Prefer the article with more contact data
            existing = seen_companies[norm]
            existing_score = bool(existing["linkedin_url"]) + bool(existing["contact_email"]) + bool(existing["website"])
            new_score = bool(article["linkedin_url"]) + bool(article["contact_email"]) + bool(article["website"])
            if new_score > existing_score:
                seen_companies[norm] = article

    deduped = list(seen_companies.values())

    # Sort by date descending
    deduped.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    has_contact = sum(1 for a in deduped if a["linkedin_url"] or a["contact_email"])
    print(f"{has_contact}/{len(deduped)} companies have contact info")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "count": len(deduped),
                "enriched_count": has_contact,
                "articles": deduped,
            },
            f,
            indent=2,
        )
    print(f"\nDone. {len(deduped)} seed articles written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
