"""
fetch_jobs.py — Daily ATS job board tracker

Monitors Ashby, Greenhouse, and Lever job boards for ~4,500 companies.
Diffs against yesterday's snapshot to surface companies with new openings.

First run builds a baseline snapshot — no output companies yet.
Second run onwards shows what's actually new since yesterday.

Usage: python fetch_jobs.py [--limit N]
Output: data/jobs.json, data/jobs_snapshot.json
"""

import csv
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

USER_AGENT = "DesignAgencyLeadGen/1.0 (contact@yourdesignagency.com)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

OUTPUT_PATH   = Path("data/jobs.json")
SNAPSHOT_PATH = Path("data/jobs_snapshot.json")
SLEEP         = 0.25  # seconds between requests per platform

# Company list CSVs from the public stapply-ai/ats-scrapers repo
COMPANY_LISTS = {
    "ashby":      "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/ashby/companies.csv",
    "greenhouse": "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/greenhouse/greenhouse_companies.csv",
    "lever":      "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/lever/lever_companies.csv",
}

# Titles that suggest an incoming design need (company is spending on marketing/brand)
DESIGN_SIGNAL_KEYWORDS = [
    "marketing", "growth", "brand", "content", "creative", "social media",
    "communications", "demand gen", "revenue", "head of", "vp ", "vice president",
    "cmo ", "designer", " ux", " ui ", "user experience", "visual design",
    "product manager", "product lead", "go-to-market", "gtm",
]


# ── Snapshot ──────────────────────────────────────────────────────────────────

def load_snapshot() -> tuple[set, str]:
    if SNAPSHOT_PATH.exists():
        try:
            data = json.loads(SNAPSHOT_PATH.read_text())
            return set(data.get("urls", [])), data.get("date", "")
        except Exception:
            pass
    return set(), ""


def save_snapshot(urls: set, date: str):
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "date": date,
        "count": len(urls),
        "urls": sorted(urls),
    }))


# ── Company lists ─────────────────────────────────────────────────────────────

def extract_slug_name(row: dict, ats: str) -> tuple[str, str]:
    """Flexibly extract (slug, name) from a CSV row regardless of column names."""
    slug = (row.get("slug") or row.get("company_slug") or "").strip()
    name = (row.get("name") or row.get("company_name") or row.get("company") or "").strip()

    if not slug:
        url_val = (row.get("url") or row.get("job_board_url") or row.get("link") or "").strip()
        if url_val:
            slug = url_val.rstrip("/").split("/")[-1]

    if not slug and row:
        # Last resort: first column value that looks like a slug
        for v in row.values():
            v = v.strip()
            if v and "/" not in v and "." not in v and len(v) < 80:
                slug = v
                break

    return slug.strip(), (name or slug).strip()


def fetch_company_list(ats: str) -> list[dict]:
    url = COMPANY_LISTS[ats]
    print(f"  Downloading {ats} companies...", flush=True)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        companies = []
        for row in reader:
            slug, name = extract_slug_name(row, ats)
            if not slug:
                continue
            # Skip numeric Greenhouse board IDs — they're legacy entries with no readable name
            if ats == "greenhouse" and slug.isdigit():
                continue
            companies.append({"slug": slug, "name": name})
        print(f"    {len(companies)} companies loaded", flush=True)
        return companies
    except Exception as e:
        print(f"    Error fetching {ats} company list: {e}", flush=True)
        return []


# ── ATS API fetchers ──────────────────────────────────────────────────────────

def get_ashby_jobs(slug: str) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
            headers=HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return []
        jobs = []
        for job in r.json().get("jobs", []):
            if not job.get("isListed", True):
                continue
            url = job.get("applyUrl") or job.get("jobUrl") or ""
            if not url:
                continue
            jobs.append({
                "title":      job.get("title", ""),
                "location":   job.get("locationName") or job.get("location") or "",
                "url":        url,
                "department": job.get("departmentName") or job.get("teamName") or "",
            })
        return jobs
    except Exception:
        return []


def get_greenhouse_jobs(slug: str) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.greenhouse.io/v1/boards/{slug}/jobs",
            headers=HEADERS, timeout=10,
        )
        if r.status_code != 200:
            # Try the newer boards-api endpoint
            r = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                headers=HEADERS, timeout=10,
            )
        if r.status_code != 200:
            return []
        jobs = []
        for job in r.json().get("jobs", []):
            url = job.get("absolute_url", "")
            if not url:
                continue
            loc = job.get("location", {})
            location = loc.get("name", "") if isinstance(loc, dict) else str(loc)
            depts = job.get("departments", [])
            dept = depts[0].get("name", "") if depts else ""
            jobs.append({
                "title":      job.get("title", ""),
                "location":   location,
                "url":        url,
                "department": dept,
            })
        return jobs
    except Exception:
        return []


def get_lever_jobs(slug: str) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.lever.co/v0/postings/{slug}",
            headers=HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return []
        jobs = []
        for job in r.json() if isinstance(r.json(), list) else []:
            url = job.get("hostedUrl", "")
            if not url:
                continue
            cats = job.get("categories", {})
            jobs.append({
                "title":      job.get("text", ""),
                "location":   cats.get("location") or job.get("workplaceType") or "",
                "url":        url,
                "department": cats.get("team") or cats.get("department") or "",
            })
        return jobs
    except Exception:
        return []


FETCHERS = {
    "ashby":      get_ashby_jobs,
    "greenhouse": get_greenhouse_jobs,
    "lever":      get_lever_jobs,
}

JOB_BOARD_BASE = {
    "ashby":      "https://jobs.ashbyhq.com/{slug}",
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever":      "https://jobs.lever.co/{slug}",
}


# ── Signals ───────────────────────────────────────────────────────────────────

def has_design_signal(roles: list[dict]) -> bool:
    """True if any new role title suggests upcoming design budget."""
    for role in roles:
        t = role["title"].lower()
        if any(kw in t for kw in DESIGN_SIGNAL_KEYWORDS):
            return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    print("Loading snapshot...", flush=True)
    old_urls, snapshot_date = load_snapshot()
    is_first_run = not old_urls

    if is_first_run:
        print("First run — building baseline. No diff output this run.", flush=True)
    else:
        print(f"Snapshot: {len(old_urls)} jobs from {snapshot_date}", flush=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_current_urls: set = set()
    companies_with_new: list[dict] = []
    total_new = 0

    for ats in ("ashby", "greenhouse", "lever"):
        print(f"\n── {ats.title()} ──", flush=True)
        company_list = fetch_company_list(ats)
        if limit:
            company_list = company_list[:limit]
        fetcher = FETCHERS[ats]

        for i, company in enumerate(company_list):
            slug = company["slug"]
            name = company["name"]

            jobs = fetcher(slug)
            if not jobs:
                time.sleep(SLEEP)
                continue

            current_urls = {j["url"] for j in jobs if j.get("url")}
            all_current_urls.update(current_urls)

            if is_first_run:
                time.sleep(SLEEP)
                continue

            new_urls = current_urls - old_urls
            if not new_urls:
                time.sleep(SLEEP)
                continue

            new_roles = [j for j in jobs if j.get("url") in new_urls]
            total_new += len(new_roles)

            job_board_url = JOB_BOARD_BASE[ats].format(slug=slug)
            companies_with_new.append({
                "company":         name,
                "ats":             ats,
                "new_roles":       new_roles,
                "new_role_count":  len(new_roles),
                "total_open_roles": len(jobs),
                "job_board_url":   job_board_url,
                "design_signal":   has_design_signal(new_roles),
                "linkedin_search": f"https://www.linkedin.com/search/results/all/?keywords={quote(name)}",
            })
            print(f"  {name}: +{len(new_roles)} new", flush=True)
            time.sleep(SLEEP)

    # Sort by new role count
    companies_with_new.sort(key=lambda x: x["new_role_count"], reverse=True)

    # Save snapshot
    save_snapshot(all_current_urls if all_current_urls else old_urls, today)
    print(f"\nSnapshot saved: {len(all_current_urls or old_urls)} total URLs", flush=True)

    # Save output
    output = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "snapshot_date":    snapshot_date,
        "today":            today,
        "is_first_run":     is_first_run,
        "new_jobs_count":   total_new,
        "companies_count":  len(companies_with_new),
        "companies_hiring": companies_with_new,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))

    if is_first_run:
        print(f"Baseline of {len(all_current_urls)} jobs saved. Run again tomorrow for diffs.")
    else:
        print(f"Done. {total_new} new jobs across {len(companies_with_new)} companies → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
