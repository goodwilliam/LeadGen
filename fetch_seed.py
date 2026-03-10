"""
fetch_seed.py — Fetches seed funding news from RSS feeds for design agency lead gen.

For each article:
  1. Validates via Jina AI reader that it's an actual funding raise
  2. Extracts company name, amount, and founder/CEO name from clean text
  3. Finds company website (article links → DuckDuckGo fallback)
  4. Finds company LinkedIn (website scrape → DuckDuckGo fallback)
  5. Finds founder LinkedIn via DuckDuckGo
  6. Deduplicates by company name, filters large known companies

Uses a cache so each article is only processed once.

Usage: python fetch_seed.py
Output: data/seed.json
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

import feedparser
import requests

try:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("  Warning: playwright not installed — DDG fallback disabled")

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
DDG_SLEEP = 1.5  # polite delay between DuckDuckGo searches

# Well-known large companies — not our target clients
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
    "gmpg.org",
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


# ── Playwright / DuckDuckGo ───────────────────────────────────────────────────

_playwright_ctx = None
_browser = None


def get_browser():
    global _playwright_ctx, _browser
    if not PLAYWRIGHT_AVAILABLE:
        return None
    if _browser is None:
        print("  Starting browser...")
        _playwright_ctx = sync_playwright().start()
        Stealth().hook_playwright_context(_playwright_ctx)
        _browser = _playwright_ctx.chromium.launch(headless=True)
    return _browser


def close_browser():
    global _playwright_ctx, _browser
    if _browser:
        try:
            _browser.close()
            _playwright_ctx.stop()
        except Exception:
            pass
        _browser = None
        _playwright_ctx = None


def ddg_search(query: str, want_domain: str = None) -> str:
    """Search DuckDuckGo, return first result URL matching want_domain (if given)."""
    browser = get_browser()
    if not browser:
        return ""
    page = browser.new_page()
    try:
        page.goto(
            f"https://duckduckgo.com/?q={quote(query)}&ia=web",
            wait_until="networkidle",
            timeout=15000,
        )
        time.sleep(DDG_SLEEP)
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        skip = {
            "duckduckgo", "google", "bing", "microsoft", "youtube.com",
            "twitter.com", "x.com", "facebook.com", "instagram.com",
            "apple.com", "android", "play.google",
        }
        for link in links:
            if not link.startswith("http"):
                continue
            if any(s in link for s in skip):
                continue
            if want_domain and want_domain not in link:
                continue
            return link.split("?")[0].rstrip("/")
    except Exception as e:
        print(f"    DDG error: {e}")
    finally:
        page.close()
    return ""


# ── RSS fetching ──────────────────────────────────────────────────────────────

SEED_KEYWORDS = re.compile(
    r"\bseed\b|\bpre.?seed\b|\braises?\b|\bfunding\b|\binvest",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_google_url(url: str) -> str:
    m = re.search(r"url=([^&]+)", url)
    return unquote(m.group(1)) if m else url


def fetch_rss(feed_url: str, source_name: str, pre_filtered: bool = False) -> list[dict]:
    print(f"  Fetching {source_name}...")
    try:
        feed = feedparser.parse(feed_url, request_headers={"User-Agent": USER_AGENT})
    except Exception as e:
        print(f"    Error: {e}")
        return []

    if feed.bozo and not feed.entries:
        print(f"    Parse error: {feed.bozo_exception}")
        return []

    articles = []
    for entry in feed.entries:
        title = strip_html(entry.get("title", "").strip())

        link = entry.get("link", "")
        if "google.com/url" in link or "google.com/alerts" in link:
            link = extract_google_url(link)
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

        if not pre_filtered:
            if not SEED_KEYWORDS.search(title + " " + description):
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
            "founder_name": "",
            "founder_linkedin_url": "",
            "contact_email": "",
            "twitter_url": "",
        })

    print(f"    {len(articles)} seed articles found")
    return articles


def parse_date(raw: str) -> str:
    """Return ISO 8601 datetime string (with time if available, else date only)."""
    if not raw:
        return ""
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            # Store as UTC ISO string so the browser can convert to local time
            if dt.tzinfo is None:
                from datetime import timezone as _tz
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.astimezone(__import__('datetime').timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    # Date-only fallback
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    return (m.group(1) + "T00:00:00Z") if m else ""


# ── Headline parsing ──────────────────────────────────────────────────────────

RAISE_VERBS  = r"(?:raises?|secures?|lands?|closes?|announces?|gets?|nabs?|bags?|wins?|brings?|launches?|nets?)"
AMOUNT_PAT   = r"(?:[\$£€][\d.,]+\s*[MBKmb]|[\d.,]+\s*[MBKmb]\s*(?:USD|GBP|EUR))"

HEADLINE_PATTERNS = [
    re.compile(
        rf"^(?P<company>[A-Z][^,$£€\n]{{2,50}}?)\s+{RAISE_VERBS}\s+(?P<amount>{AMOUNT_PAT})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<amount>{AMOUNT_PAT})\s+seed\s+(?:round|funding)\s+for\s+(?P<company>[A-Z][^,\n]{{2,40}})",
        re.IGNORECASE,
    ),
]

STRIP_WORDS = re.compile(
    rf"\b{RAISE_VERBS}\b.*$",
    re.IGNORECASE,
)

DESCRIPTOR_WORDS = {
    "a", "an", "the", "startup", "company", "firm", "platform", "app", "venture",
    "ai", "saas", "b2b", "b2c", "web3", "crypto", "defi", "nft", "blockchain",
    "fintech", "healthtech", "edtech", "proptech", "insurtech", "cleantech", "deeptech",
    "biotech", "medtech", "agtech", "legaltech", "logistics", "ecommerce", "cybersecurity",
    "security", "data", "analytics", "cloud", "enterprise", "consumer", "mobile",
    "gaming", "media", "tech", "software", "hardware", "digital", "social", "global",
    "early", "stage", "new", "former", "ex", "yc", "based", "backed",
    "american", "european", "british", "german", "french", "dutch", "belgian",
    "swedish", "norwegian", "finnish", "danish", "spanish", "italian", "portuguese",
    "indian", "chinese", "japanese", "korean", "singaporean", "israeli",
    "canadian", "australian", "nordic", "african", "south", "north", "us", "uk", "eu",
}

# If the extracted "company name" contains these verbs it's a headline fragment, not a name
BAD_NAME_RE = re.compile(
    r"\b(brings|launches|appoints|raises|secures|lands|closes|announces|gets|nabs|bags|wins|"
    r"just|also|announced|raised|secured|who|thinks|has|is|was|its|with|for|to|from|that|this)\b",
    re.IGNORECASE,
)


def clean_company_name(name: str) -> str:
    name = name.strip().rstrip(",").strip()
    # Strip everything before a colon (e.g. "AI vs AI: YC's Escape" → "YC's Escape")
    if ":" in name:
        after = name.split(":", 1)[1].strip()
        if after:
            name = after
    words = name.split()
    if len(words) == 0:
        return name
    last_descriptor_idx = -1
    for i, word in enumerate(words):
        w = re.sub(r"'s$", "", word.lower()).strip(".,;:-/()")
        if w in DESCRIPTOR_WORDS:
            last_descriptor_idx = i
        else:
            break
    if last_descriptor_idx >= 0 and last_descriptor_idx < len(words) - 1:
        candidate = " ".join(words[last_descriptor_idx + 1:])
        if candidate and candidate[0].isupper():
            return candidate
    return name


def is_bad_name(name: str) -> bool:
    """Return True if the extracted name looks like a headline fragment."""
    if not name or len(name) > 60:
        return True
    if BAD_NAME_RE.search(name):
        return True
    # Too many words = probably grabbed a phrase not a name
    if len(name.split()) > 6:
        return True
    return False


def parse_headline(title: str) -> tuple[str, str]:
    for pattern in HEADLINE_PATTERNS:
        m = pattern.search(title)
        if m:
            name = clean_company_name(m.group("company"))
            if not is_bad_name(name):
                amt = m.group('amount').strip()
                if amt and amt[0] not in "$£€":
                    amt = "$" + amt
                return name, amt
    m = STRIP_WORDS.search(title)
    if m:
        company = clean_company_name(title[:m.start()])
        amt = re.search(r"\$[\d.,]+\s*[MBKmb]", title, re.IGNORECASE)
        if not is_bad_name(company):
            return company, amt.group(0) if amt else ""
    return "", ""


# ── Jina AI reader ────────────────────────────────────────────────────────────

RAISE_CONFIRM_RE = re.compile(
    r"(?:raises?|raised|secures?|secured|closes?|closed|lands?|landed|bags?|bagged|nabs?|nabbed|wins?|won|announces?|announced)\s+"
    r"(?:\w+\s+){0,4}?(?:\$[\d.,]+\s*[MmBbKk]|seed\s+(?:round|funding)|pre.?seed|funding\s+round|investment\s+round)",
    re.IGNORECASE,
)

NOT_RAISE_RE = re.compile(
    r"\b(?:roundup|class\s+action|lawsuit|settlement|lays?\s*off|layoffs?|cuts?\s+jobs?|"
    r"top\s+\d+\s+startups?|best\s+startups?|\d+\s+startups?\s+to\s+watch|"
    r"weekly\s+(?:funding|roundup)|funding\s+roundup|monthly\s+(?:funding|roundup)|"
    r"how\s+to\s+raise|what\s+investors\s+(?:want|look)|says?\s+(?:nvidia|openai|google|microsoft))\b",
    re.IGNORECASE,
)

JINA_COMPANY_PATTERNS = [
    re.compile(
        r"(?:^|\n|\. )(?P<company>[A-Z][A-Za-z0-9][A-Za-z0-9 \-]{0,35}?)\s+(?:has\s+)?(?:raised?|secured?|closed?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])",
        re.MULTILINE,
    ),
    re.compile(
        r"(?:^|\n|\. )(?P<company>[A-Z][A-Za-z0-9][A-Za-z0-9 \-]{0,35}?),\s+[a-z][^.]{0,80}?,\s+(?:has\s+)?(?:raised?|secured?|closed?)\s+\$(?P<amount>[\d.,]+\s*[MBKmb])",
        re.MULTILINE,
    ),
]

# Extract founder/CEO name from article text
FOUNDER_PATTERNS = [
    # "Name, CEO/founder of Company"
    re.compile(
        r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+),?\s+(?:the\s+)?(?:founder|co-?founder|CEO|chief\s+executive|CTO|president|managing\s+director)",
        re.IGNORECASE,
    ),
    # "CEO/founder Name"
    re.compile(
        r"(?:founder|co-?founder|CEO|chief\s+executive|CTO|president)\s+(?:and\s+\w+\s+)?(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)",
        re.IGNORECASE,
    ),
]


def jina_fetch(url: str) -> str:
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
            timeout=JINA_TIMEOUT,
        )
        if r.status_code == 200:
            return r.text[:10000]
    except Exception:
        pass
    return ""


def extract_founder_name(text: str) -> str:
    """Extract founder or CEO name from article text."""
    for pattern in FOUNDER_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group("name").strip()
            # Sanity check: at least two words, not too long
            words = name.split()
            if 2 <= len(words) <= 4:
                return name
    return ""


def validate_and_extract(article: dict, text: str) -> tuple[bool, str, str, str]:
    """
    Validate article and extract company, amount, founder name from Jina text.
    Returns: (is_valid, company, amount, founder_name)
    """
    if not text:
        return True, article["company"], article["amount_str"], ""

    if NOT_RAISE_RE.search(text):
        return False, "", "", ""

    if not RAISE_CONFIRM_RE.search(text):
        return False, "", "", ""

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

    founder_name = extract_founder_name(text)

    return True, company, amount, founder_name


# ── Article scraping for company website ─────────────────────────────────────

def extract_company_website(article_url: str, company_name: str = "") -> str:
    """Scrape article page for a company website link.

    Only returns a link if the company name appears in the domain —
    this avoids picking up sponsor/event/utility links from the article page.
    """
    if not article_url:
        return ""

    article_domain = urlparse(article_url).netloc.lower().lstrip("www.")
    skip_domains = NEWS_DOMAINS | {article_domain}
    # Slug of company name for domain matching (e.g. "Roxfit" → "roxfit")
    company_slug = re.sub(r"[^a-z0-9]", "", company_name.lower()) if company_name else ""

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
        if any(domain == nd or domain.endswith("." + nd) for nd in skip_domains):
            continue
        if re.search(r"(google|apple|android|play\.google|apps\.apple|github\.com/[^/]+/[^/]+(?:/|$))", href, re.I):
            continue
        if urlparse(href).path.count("/") > 3:
            continue
        # Only accept if company name appears in the domain (high confidence)
        domain_clean = re.sub(r"[^a-z0-9]", "", domain)
        if company_slug and company_slug[:5] not in domain_clean:
            continue
        return href.split("?")[0].rstrip("/")
    return ""


# ── Site scraping for contact info ────────────────────────────────────────────

SKIP_EMAIL_PATTERNS = re.compile(
    r"noreply|no-reply|example\.|placeholder|@sentry|@github|\.png|\.jpg|\.svg",
    re.IGNORECASE,
)
CONTACT_PREFIXES = ["hello", "contact", "team", "hi", "gm", "info", "hey", "support"]


def scrape_site(url: str) -> dict:
    """Scrape company website for LinkedIn, email, Twitter."""
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

    li = re.search(r"linkedin\.com/company/([\w\-]+)", html)
    if li:
        result["linkedin_url"] = f"https://linkedin.com/company/{li.group(1)}"

    tw = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,50})(?:[\"'\/\s]|$)", html)
    if tw and tw.group(1).lower() not in ("share", "intent", "home", "search", "hashtag"):
        result["twitter_url"] = f"https://x.com/{tw.group(1)}"

    all_emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,6}", html)
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
    valid_articles = []

    for i, article in enumerate(articles, 1):
        key = article["article_url"]

        if key in cache:
            cached = cache[key]
            if cached.get("valid") is False:
                continue
            article["company"]              = cached.get("company") or article["company"]
            article["amount_str"]           = cached.get("amount_str") or article["amount_str"]
            article["website"]              = cached.get("website", "")
            article["linkedin_url"]         = cached.get("linkedin_url", "")
            article["founder_name"]         = cached.get("founder_name", "")
            article["founder_linkedin_url"] = cached.get("founder_linkedin_url", "")
            article["contact_email"]        = cached.get("contact_email", "")
            article["twitter_url"]          = cached.get("twitter_url", "")
            valid_articles.append(article)
            continue

        print(f"  [{i}/{len(articles)}] {article['title'][:65]}...")

        # ── Step 1: Jina — validate + extract company/amount/founder ──────────
        jina_text = jina_fetch(article["article_url"])
        is_valid, company, amount, founder_name = validate_and_extract(article, jina_text)
        time.sleep(SLEEP_BETWEEN)

        if not is_valid:
            print(f"    SKIP — not a funding raise")
            cache[key] = {"valid": False}
            save_cache(cache)
            new_entries += 1
            continue

        article["company"]      = company or article["company"]
        article["amount_str"]   = amount or article["amount_str"]
        article["founder_name"] = founder_name

        # ── Step 2: find website ──────────────────────────────────────────────
        website = extract_company_website(article["article_url"], article["company"])
        time.sleep(SLEEP_BETWEEN)

        if not website and article["company"]:
            print(f"    DDG: searching for {article['company']} website...")
            website = ddg_search(f'"{article["company"]}" official site', want_domain=None)
            if website:
                # Normalize to homepage (strip any path like /blog/post)
                parsed = urlparse(website)
                website = f"{parsed.scheme}://{parsed.netloc}"
                domain = parsed.netloc.lower().lstrip("www.")
                if any(domain == nd or domain.endswith("." + nd) for nd in NEWS_DOMAINS):
                    website = ""

        article["website"] = website
        if website:
            print(f"    Website: {website}")

        # ── Step 3: scrape website for LinkedIn/email/Twitter ─────────────────
        site_data = scrape_site(website) if website else {"linkedin_url": "", "contact_email": "", "twitter_url": ""}
        article["linkedin_url"]  = site_data["linkedin_url"]
        article["contact_email"] = site_data["contact_email"]
        article["twitter_url"]   = site_data["twitter_url"]
        if website:
            time.sleep(SLEEP_BETWEEN)

        # ── Step 4: DDG fallback for company LinkedIn ─────────────────────────
        if not article["linkedin_url"] and article["company"]:
            print(f"    DDG: searching for {article['company']} LinkedIn...")
            li = ddg_search(f'"{article["company"]}" linkedin', want_domain="linkedin.com/company")
            if li:
                # Clean to just linkedin.com/company/slug
                m = re.search(r"linkedin\.com/company/([\w\-]+)", li)
                if m:
                    article["linkedin_url"] = f"https://linkedin.com/company/{m.group(1)}"

        if article["linkedin_url"]:
            print(f"    LinkedIn: {article['linkedin_url']}")

        # ── Step 5: DDG founder LinkedIn ──────────────────────────────────────
        founder_linkedin = ""
        if founder_name and article["company"]:
            print(f"    DDG: searching for {founder_name} LinkedIn...")
            fl = ddg_search(
                f'"{founder_name}" "{article["company"]}" linkedin',
                want_domain="linkedin.com/in",
            )
            if fl:
                m = re.search(r"linkedin\.com/in/([\w\-]+)", fl)
                if m:
                    founder_linkedin = f"https://linkedin.com/in/{m.group(1)}"
        elif not founder_name and article["company"]:
            # No founder name found — try generic CEO search
            print(f"    DDG: searching for {article['company']} CEO LinkedIn...")
            fl = ddg_search(
                f'"{article["company"]}" CEO OR founder site:linkedin.com/in',
                want_domain="linkedin.com/in",
            )
            if fl:
                m = re.search(r"linkedin\.com/in/([\w\-]+)", fl)
                if m:
                    founder_linkedin = f"https://linkedin.com/in/{m.group(1)}"

        article["founder_linkedin_url"] = founder_linkedin
        if founder_linkedin:
            print(f"    Founder LinkedIn: {founder_linkedin}")

        # ── Cache ─────────────────────────────────────────────────────────────
        cache[key] = {
            "valid": True,
            "company":              article["company"],
            "amount_str":           article["amount_str"],
            "website":              website,
            "linkedin_url":         article["linkedin_url"],
            "founder_name":         founder_name,
            "founder_linkedin_url": founder_linkedin,
            "contact_email":        article["contact_email"],
            "twitter_url":          article["twitter_url"],
        }
        new_entries += 1
        save_cache(cache)
        valid_articles.append(article)

    if new_entries:
        print(f"  Processed {new_entries} new articles (cache: {len(cache)} entries)")
    else:
        print("  All articles already cached")

    return valid_articles


def normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


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
        print("\nEnriching articles...")
        try:
            all_articles = enrich_articles(all_articles)
        finally:
            close_browser()

    print(f"{len(all_articles)} articles passed validation")

    def contact_score(a):
        return (
            bool(a["website"]) * 3 +
            bool(a["linkedin_url"]) * 2 +
            bool(a["founder_linkedin_url"]) * 2 +
            bool(a["contact_email"])
        )

    def keep_better(existing, candidate):
        return candidate if contact_score(candidate) > contact_score(existing) else existing

    # Pass 1: deduplicate by normalized company name
    seen_by_name: dict[str, dict] = {}
    for article in all_articles:
        company = article.get("company", "").strip()
        norm = normalize_company(company)
        if not norm or norm in LARGE_COMPANY_BLACKLIST:
            continue
        if norm not in seen_by_name:
            seen_by_name[norm] = article
        else:
            seen_by_name[norm] = keep_better(seen_by_name[norm], article)

    # Pass 2: deduplicate by amount+date (catches "Yann LeCun's startup" == "AMI Labs" etc.)
    seen_by_raise: dict[str, dict] = {}
    for article in seen_by_name.values():
        amount = re.sub(r"\s+", "", (article.get("amount_str") or "").lower())
        date   = article.get("pub_date") or ""
        if amount and date:
            key = f"{amount}|{date}"
            if key not in seen_by_raise:
                seen_by_raise[key] = article
            else:
                seen_by_raise[key] = keep_better(seen_by_raise[key], article)
        else:
            # No amount/date to match on — keep as-is under a unique key
            seen_by_raise[id(article)] = article

    deduped = list(seen_by_raise.values())
    deduped.sort(key=lambda x: x["pub_date"] or "", reverse=True)

    has_contact = sum(1 for a in deduped if a["linkedin_url"] or a["contact_email"] or a["founder_linkedin_url"])
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
