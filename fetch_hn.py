"""
fetch_hn.py — Fetches recent HN Show HN posts for design agency lead gen.

Show HN posts are technical founders building in public — almost always
no designer, open to outreach, direct contact via HN profile.

Uses the free Algolia HN search API (no auth needed).

Usage: python fetch_hn.py
Output: data/hn.json
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CONTACT_EMAIL = "something123@gmail.com"
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
OUTPUT_PATH = Path("data/hn.json")

LOOKBACK_DAYS = 30
MIN_POINTS = 5          # filter out noise / dead posts
MAX_PAGES = 10          # Algolia paginates at 100/page → up to 1000 posts


def strip_show_hn(title: str) -> str:
    """'Show HN: Acme – AI thing' → 'Acme – AI thing'"""
    return re.sub(r"^Show HN:\s*", "", title, flags=re.IGNORECASE).strip()


def fetch_posts(cutoff_ts: int) -> list[dict]:
    posts = []
    page = 0
    while page < MAX_PAGES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "tags": "show_hn",
                    "numericFilters": f"created_at_i>{cutoff_ts},points>={MIN_POINTS}",
                    "hitsPerPage": 100,
                    "page": page,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            print(f"  Warning page {page}: {e}")
            break

        hits = data.get("hits", [])
        nb_pages = data.get("nbPages", 1)

        for hit in hits:
            url = hit.get("url", "")
            if not url:
                continue  # skip Ask HN style without external link
            hn_id = hit.get("objectID", "")
            created_raw = hit.get("created_at", "")
            # Normalize to YYYY-MM-DD
            try:
                created_date = datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                ).strftime("%Y-%m-%d")
            except Exception:
                created_date = ""

            posts.append({
                "title": strip_show_hn(hit.get("title", "")),
                "full_title": hit.get("title", ""),
                "url": url,
                "author": hit.get("author", ""),
                "points": hit.get("points", 0),
                "num_comments": hit.get("num_comments", 0),
                "created_date": created_date,
                "hn_url": f"https://news.ycombinator.com/item?id={hn_id}",
                "author_url": f"https://news.ycombinator.com/user?id={hit.get('author', '')}",
            })

        if page >= nb_pages - 1:
            break
        page += 1
        time.sleep(0.15)

    return posts


def main():
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cutoff_ts = int(cutoff_dt.timestamp())
    print(f"Fetching Show HN posts since {cutoff_dt.date()} (min {MIN_POINTS} points)...")

    posts = fetch_posts(cutoff_ts)

    # Sort by points descending
    posts.sort(key=lambda x: x["points"], reverse=True)

    print(f"  {len(posts)} posts found")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "count": len(posts),
                "posts": posts,
            },
            f,
            indent=2,
        )
    print(f"Done. {len(posts)} Show HN posts written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
