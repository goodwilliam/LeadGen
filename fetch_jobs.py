"""
fetch_jobs.py — Daily ATS job board tracker

Monitors Ashby, Greenhouse, and Lever job boards for ~5,900 companies.
Tracks first-seen date per URL so we can show both daily AND weekly new postings.

Signals per company:
  design_signal  — new role in marketing/growth/brand (incoming design budget)
  senior_hire    — new VP/Head/C-level hire (budget decision-maker arriving)
  no_designer    — company has ZERO design/UX roles currently (clear gap)
  remote         — company is hiring remote roles
  small_company  — <15 total open roles (startup, no in-house team)
  first_seen     — company never appeared in our snapshot before

Usage: python fetch_jobs.py [--limit N]
Output: data/jobs.json, data/jobs_snapshot.json
"""

import csv
import io
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

USER_AGENT    = "DesignAgencyLeadGen/1.0 (contact@yourdesignagency.com)"
HEADERS       = {"User-Agent": USER_AGENT, "Accept": "application/json"}
OUTPUT_PATH   = Path("data/jobs.json")
SNAPSHOT_PATH = Path("data/jobs_snapshot.json")
SLEEP         = 0.25   # seconds between requests
MAX_COMPANIES = 300    # cap output (sorted by new_week_count)
MAX_ROLES_OUT = 5      # max roles stored per company in output
SMALL_CO_MAX  = 15     # total_open_roles threshold for "small company"
WEEK_DAYS     = 7      # "new this week" window
# If >20% of all scraped jobs are "new", snapshot is stale → rebuild baseline
STALE_RATIO   = 0.20

COMPANY_LISTS = {
    "ashby":      "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/ashby/companies.csv",
    "greenhouse": "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/greenhouse/greenhouse_companies.csv",
    "lever":      "https://raw.githubusercontent.com/stapply-ai/ats-scrapers/main/lever/lever_companies.csv",
}

# New role titles → incoming design budget signal
DESIGN_SIGNAL_KW = [
    "marketing", "growth", "brand", "content", "creative", "social media",
    "communications", "demand gen", "revenue", "head of", "vp ", "vice president",
    "cmo ", " ux", " ui ", "user experience", "visual design",
    "product manager", "product lead", "go-to-market", "gtm",
]

# New role is a senior decision-maker
SENIOR_KW = [
    "head of", "vp ", "vice president", "director", "cto", "coo", "cmo", "cpo",
    "chief ", "svp", "evp", "president", "partner", "principal",
]

# Any current role that looks like in-house design work
DESIGNER_ROLE_KW = [
    "designer", " design", "ux", "ui ", "user experience", "visual",
    "creative director", "art director", "motion", "graphic",
]

# Locations that suggest remote-friendly
REMOTE_KW = ["remote", "anywhere", "distributed", "work from home", "wfh"]


# ── Snapshot ──────────────────────────────────────────────────────────────────
# Format: { "date": "YYYY-MM-DD", "jobs": { url: first_seen_date }, "companies_seen": [slug, ...] }

def load_snapshot() -> tuple[dict, str, set]:
    """Returns (jobs_dict {url: date}, snapshot_date, companies_seen_set)."""
    if SNAPSHOT_PATH.exists():
        try:
            data = json.loads(SNAPSHOT_PATH.read_text())
            # Migrate old format: { "urls": [...] }
            if "urls" in data and "jobs" not in data:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                jobs = {u: today for u in data["urls"]}
                return jobs, data.get("date", today), set(data.get("companies_seen", []))
            jobs = data.get("jobs", {})
            companies_seen = set(data.get("companies_seen", []))
            return jobs, data.get("date", ""), companies_seen
        except Exception:
            pass
    return {}, "", set()


def save_snapshot(jobs: dict, companies_seen: set, date: str):
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "date":           date,
        "count":          len(jobs),
        "jobs":           jobs,
        "companies_seen": sorted(companies_seen),
    }))


# ── Company lists ─────────────────────────────────────────────────────────────

def extract_slug_name(row: dict) -> tuple[str, str]:
    slug = (row.get("slug") or row.get("company_slug") or "").strip()
    name = (row.get("name") or row.get("company_name") or row.get("company") or "").strip()
    if not slug:
        url_val = (row.get("url") or row.get("job_board_url") or row.get("link") or "").strip()
        if url_val:
            slug = url_val.rstrip("/").split("/")[-1]
    if not slug and row:
        for v in row.values():
            v = v.strip()
            if v and "/" not in v and "." not in v and len(v) < 80:
                slug = v
                break
    return slug.strip(), (name or slug).strip()


def fetch_company_list(ats: str) -> list[dict]:
    print(f"  Downloading {ats} companies...", flush=True)
    try:
        r = requests.get(COMPANY_LISTS[ats], headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        companies = []
        for row in reader:
            slug, name = extract_slug_name(row)
            if not slug:
                continue
            if ats == "greenhouse" and slug.isdigit():
                continue  # skip legacy numeric board IDs
            companies.append({"slug": slug, "name": name})
        print(f"    {len(companies)} companies", flush=True)
        return companies
    except Exception as e:
        print(f"    Error: {e}", flush=True)
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
        data = r.json()
        if not isinstance(data, list):
            return []
        jobs = []
        for job in data:
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

def kw_match(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(kw in t for kw in keywords)

def calc_signals(new_roles: list[dict], all_roles: list[dict],
                 slug: str, companies_seen: set) -> dict:
    all_titles = [r["title"] for r in all_roles]
    all_locations = [r["location"] for r in all_roles]

    has_designer = any(kw_match(t, DESIGNER_ROLE_KW) for t in all_titles)
    has_remote   = any(kw_match(loc, REMOTE_KW) for loc in all_locations)

    return {
        "design_signal":  any(kw_match(r["title"], DESIGN_SIGNAL_KW) for r in new_roles),
        "senior_hire":    any(kw_match(r["title"], SENIOR_KW) for r in new_roles),
        "no_designer":    not has_designer,
        "remote":         has_remote,
        "small_company":  len(all_roles) < SMALL_CO_MAX,
        "first_seen":     slug not in companies_seen,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    print("Loading snapshot...", flush=True)
    old_jobs, snapshot_date, companies_seen = load_snapshot()
    old_urls = set(old_jobs.keys())
    is_first_run = not old_urls

    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago  = (datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%d")

    if is_first_run:
        print("First run — building baseline.", flush=True)
    else:
        print(f"Snapshot: {len(old_urls)} jobs from {snapshot_date}", flush=True)

    # new_jobs tracks {url: first_seen_date} for everything we find this run
    new_jobs: dict = dict(old_jobs)  # start with existing, update below
    new_companies_seen: set = set(companies_seen)
    companies_out: list[dict] = []
    total_new_today = 0

    for ats in ("ashby", "greenhouse", "lever"):
        print(f"\n── {ats.title()} ──", flush=True)
        company_list = fetch_company_list(ats)
        if limit:
            company_list = company_list[:limit]

        for company in company_list:
            slug = company["slug"]
            name = company["name"]

            all_roles = FETCHERS[ats](slug)
            if not all_roles:
                time.sleep(SLEEP)
                continue

            current_urls = {j["url"] for j in all_roles if j.get("url")}

            # Mark new URLs with today's date
            for url in current_urls:
                if url not in new_jobs:
                    new_jobs[url] = today

            # Track this company as seen
            new_companies_seen.add(slug)

            if is_first_run:
                time.sleep(SLEEP)
                continue

            # New today = URLs not in old snapshot at all
            today_urls = current_urls - old_urls
            # New this week = URLs first seen within the last 7 days
            week_urls  = {u for u in current_urls if new_jobs.get(u, "0") >= week_ago}

            if not week_urls:
                time.sleep(SLEEP)
                continue

            today_roles = [j for j in all_roles if j.get("url") in today_urls]
            week_roles  = [j for j in all_roles if j.get("url") in week_urls]

            total_new_today += len(today_roles)

            signals = calc_signals(week_roles, all_roles, slug, companies_seen)

            job_board_url = JOB_BOARD_BASE[ats].format(slug=slug)
            companies_out.append({
                "company":          name,
                "ats":              ats,
                "new_today":        today_roles[:MAX_ROLES_OUT],
                "new_this_week":    week_roles[:MAX_ROLES_OUT],
                "new_today_count":  len(today_roles),
                "new_week_count":   len(week_urls),
                "total_open_roles": len(all_roles),
                "job_board_url":    job_board_url,
                "linkedin_search":  f"https://www.linkedin.com/search/results/all/?keywords={quote(name)}",
                "signals":          signals,
            })

            if today_roles:
                print(f"  {name}: +{len(today_roles)} today, +{len(week_urls)} this week", flush=True)
            time.sleep(SLEEP)

    # Stale snapshot guard
    total_current = len({u for u in new_jobs if new_jobs[u] == today})
    if not is_first_run and total_current > 0 and total_new_today > total_current * STALE_RATIO:
        print(f"\nSnapshot stale ({total_new_today}/{total_current} = {total_new_today/total_current:.0%}). Rebuilding baseline.", flush=True)
        is_first_run = True
        companies_out = []
        total_new_today = 0

    # Cap + sort by weekly new count
    companies_out.sort(key=lambda x: (x["new_week_count"], x["new_today_count"]), reverse=True)
    companies_out = companies_out[:MAX_COMPANIES]

    # Save snapshot
    save_snapshot(new_jobs, new_companies_seen, today)
    print(f"\nSnapshot: {len(new_jobs)} total URLs tracked", flush=True)

    # Purge URLs older than 60 days to keep snapshot lean
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    pruned = {u: d for u, d in new_jobs.items() if d >= cutoff}
    if len(pruned) < len(new_jobs):
        save_snapshot(pruned, new_companies_seen, today)
        print(f"Pruned snapshot to {len(pruned)} URLs (removed entries older than 60 days)", flush=True)

    output = {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "snapshot_date":     snapshot_date,
        "today":             today,
        "is_first_run":      is_first_run,
        "new_today_count":   total_new_today,
        "companies_count":   len(companies_out),
        "companies_hiring":  companies_out,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))

    if is_first_run:
        print(f"Baseline built. Run again tomorrow to see new postings.")
    else:
        print(f"Done. {total_new_today} new today across {len(companies_out)} companies → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
