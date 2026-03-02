"""
fetch_leads.py — SEC EDGAR Form D scraper for design agency lead generation.

Scrapes Form D filings from the last 60 days, filters for seed/pre-seed tech
startups, and writes results to data/leads.json for display in index.html.

Usage: python fetch_leads.py
Output: data/leads.json
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CONTACT_EMAIL = "something123@gmail.com"  # Required by SEC fair-use policy
USER_AGENT = f"DesignAgencyLeadGen/1.0 ({CONTACT_EMAIL})"
EDGAR_BASE = "https://www.sec.gov"
LOOKBACK_DAYS = 30
RATE_LIMIT_SLEEP = 0.11  # seconds between XML fetches (SEC limit: 10 req/s)
OUTPUT_PATH = Path("data/leads.json")
MAX_CANDIDATES = 3_000  # most recent 3k filings — ~6 min runtime, freshest leads

# ── Industry filters (applied at XML level, not name level) ───────────────────
# Only accept these EDGAR industryGroupType values. Empty/missing = keep (many
# legit startups leave it blank or pick "Other").
KEEP_INDUSTRIES = {
    "Technology",
    "Computers",
    "Telecommunications",
    "Biotechnology",
    "Health Sciences",
    "Finance",
    "Business Services",
    "Other",
    "",  # blank = keep
}

# Amount range: $0 – $15M (no floor — early stage companies often file tiny rounds)
MIN_AMOUNT = 0
MAX_AMOUNT = 15_000_000

# Skip these entity types (funds, etc.)
SKIP_ENTITY_TYPES = re.compile(
    r"investment company|hedge fund|private equity|venture capital|"
    r"real estate investment|reit|trust",
    re.IGNORECASE,
)


def get_quarters_to_fetch() -> list[tuple[int, str]]:
    """Return list of (year, 'QTR#') tuples covering the lookback window."""
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    quarters = []
    for d in [today, cutoff]:
        q = f"QTR{(d.month - 1) // 3 + 1}"
        entry = (d.year, q)
        if entry not in quarters:
            quarters.append(entry)
    return quarters


def fetch_form_idx(year: int, quarter: str) -> str:
    """Download and return raw form.idx text."""
    url = f"{EDGAR_BASE}/Archives/edgar/full-index/{year}/{quarter}/form.idx"
    print(f"  Fetching index: {url}")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def parse_form_idx(raw: str, cutoff: date) -> list[dict]:
    """
    Parse fixed-width form.idx and return Form D entries filed after cutoff.

    Actual column positions (verified from raw data — form type field is 17
    chars wide in data, 5 more than the header text implies):
      Form Type:  0–16
      Company:    17–78
      CIK:        79–90
      Date Filed: 91–100
      Filename:   103+
    """
    candidates = []
    in_data = False
    for line in raw.splitlines():
        if line.startswith("---"):
            in_data = True
            continue
        if not in_data or len(line) < 103:
            continue
        form_type = line[0:17].strip()
        if form_type not in ("D", "D/A"):
            continue
        company = line[17:79].strip()
        cik = line[79:91].strip()
        date_str = line[91:101].strip()
        filename = line[103:].strip()
        try:
            filed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if filed_date < cutoff:
            continue
        candidates.append(
            {
                "company": company,
                "cik": cik,
                "filed_date": date_str,
                "filename": filename,
                "form_type": form_type,
            }
        )
    return candidates


def accession_from_filename(filename: str) -> str:
    """Convert EDGAR filename path to accession number (no dashes)."""
    # filename like: edgar/data/1234567/0001234567-24-000001.txt
    basename = filename.split("/")[-1]
    return basename.replace("-", "").replace(".txt", "")


def build_xml_url(cik: str, accession_nodash: str) -> str:
    return (
        f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml"
    )


def build_filing_url(cik: str, accession_nodash: str) -> str:
    accession_dashed = f"{accession_nodash[:10]}-{accession_nodash[10:12]}-{accession_nodash[12:]}"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=D&dateb=&owner=include&count=10"


def strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from tag name."""
    return tag.split("}")[-1] if "}" in tag else tag


def find_text(root: ET.Element, *tags: str) -> str:
    """Walk a chain of tag names (namespace-stripped) and return text or ''."""
    node = root
    for tag in tags:
        found = None
        for child in node:
            if strip_ns(child.tag) == tag:
                found = child
                break
        if found is None:
            return ""
        node = found
    return (node.text or "").strip()


def find_all(root: ET.Element, tag: str) -> list[ET.Element]:
    """Return all direct children matching tag (namespace-stripped)."""
    return [c for c in root if strip_ns(c.tag) == tag]


def parse_xml(xml_text: str, cik: str, accession: str, filed_date: str) -> dict | None:
    """
    Parse Form D primary_doc.xml and extract lead fields.
    Returns None if the filing should be skipped.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # ── Flatten helper: find any descendant by stripped tag ──────────────────
    def find_desc(tag: str, node: ET.Element = root) -> ET.Element | None:
        for child in node:
            if strip_ns(child.tag) == tag:
                return child
            result = find_desc(tag, child)
            if result is not None:
                return result
        return None

    def desc_text(tag: str) -> str:
        el = find_desc(tag)
        return (el.text or "").strip() if el is not None else ""

    # ── Industry filter ───────────────────────────────────────────────────────
    industry = desc_text("industryGroupType")
    if industry not in KEEP_INDUSTRIES:
        return None

    # ── Entity type filter ────────────────────────────────────────────────────
    entity_type = desc_text("entityType")
    if entity_type and SKIP_ENTITY_TYPES.search(entity_type):
        return None

    # ── Offering amount filter ────────────────────────────────────────────────
    def parse_amount(tag: str) -> float:
        raw = desc_text(tag).replace(",", "")
        try:
            return float(raw)
        except ValueError:
            return 0.0

    total_offered = parse_amount("totalOfferingAmount")
    total_sold = parse_amount("totalAmountSold")
    amount = total_sold if total_sold > 0 else total_offered

    if amount > 0 and (amount < MIN_AMOUNT or amount > MAX_AMOUNT):
        return None

    # ── Company info ──────────────────────────────────────────────────────────
    company_name = desc_text("entityName") or desc_text("companyName") or ""
    city = desc_text("city") or ""
    state = desc_text("stateOrCountry") or desc_text("state") or ""
    state_of_inc = desc_text("stateOfIncorporation") or ""
    date_first_sale = desc_text("dateOfFirstSale") or ""

    # ── Founders / related persons ────────────────────────────────────────────
    founders = []
    rp_list_el = find_desc("relatedPersonsList")
    if rp_list_el is not None:
        for rp in rp_list_el:
            if strip_ns(rp.tag) != "relatedPersonInfo":
                continue
            name_el = find_desc("relatedPersonName", rp)
            first = ""
            last = ""
            if name_el is not None:
                first = find_text(name_el, "firstName") or ""
                last = find_text(name_el, "lastName") or ""
            full_name = f"{first} {last}".strip()
            if not full_name:
                full_name = desc_text("relatedPersonName")

            rels = []
            rel_list = find_desc("relatedPersonRelationshipList", rp)
            if rel_list is not None:
                for rel_el in rel_list:
                    if rel_el.text:
                        rels.append(rel_el.text.strip())

            title = desc_text("relatedPersonTitle")
            founders.append(
                {"name": full_name, "title": title, "relationships": rels}
            )

    # ── Exemptions ────────────────────────────────────────────────────────────
    exemptions = []
    ex_list = find_desc("exemptionsUsed")
    if ex_list is not None:
        for ex in ex_list:
            if ex.text:
                exemptions.append(ex.text.strip())

    return {
        "company": company_name or "",
        "cik": cik,
        "accession": accession,
        "filed_date": filed_date,
        "industry": industry,
        "entity_type": entity_type,
        "amount": amount,
        "city": city,
        "state": state,
        "state_of_inc": state_of_inc,
        "date_first_sale": date_first_sale,
        "founders": founders,
        "exemptions": exemptions,
        "filing_url": build_filing_url(cik, accession),
    }


def fetch_xml(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None


def main():
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    print(f"Fetching Form D leads filed {cutoff} → {today}")

    quarters = get_quarters_to_fetch()
    print(f"Quarters to scan: {quarters}")

    # ── Step 1: Collect candidates from form.idx ──────────────────────────────
    raw_candidates: list[dict] = []
    for year, quarter in quarters:
        try:
            raw = fetch_form_idx(year, quarter)
        except requests.RequestException as e:
            print(f"  Warning: could not fetch {year}/{quarter}: {e}")
            continue
        entries = parse_form_idx(raw, cutoff)
        print(f"  {year}/{quarter}: {len(entries)} Form D entries in window")
        raw_candidates.extend(entries)

    # Deduplicate by CIK + accession
    seen = set()
    unique_candidates = []
    for c in raw_candidates:
        acc = accession_from_filename(c["filename"])
        key = (c["cik"], acc)
        if key not in seen:
            seen.add(key)
            c["accession"] = acc
            unique_candidates.append(c)

    # Sort newest-first then cap so we always process the freshest filings
    unique_candidates.sort(key=lambda x: x["filed_date"], reverse=True)
    unique_candidates = unique_candidates[:MAX_CANDIDATES]
    print(f"\nTotal candidates to process: {len(unique_candidates)} (cap: {MAX_CANDIDATES})")

    # ── Step 2: Fetch and parse XML for each candidate ────────────────────────
    leads = []
    for i, candidate in enumerate(unique_candidates, 1):
        cik = candidate["cik"]
        acc = candidate["accession"]
        xml_url = build_xml_url(cik, acc)

        print(f"  [{i}/{len(unique_candidates)}] {candidate['company']} — {xml_url}")
        xml_text = fetch_xml(xml_url)
        time.sleep(RATE_LIMIT_SLEEP)

        if not xml_text:
            print(f"    Skipped: no XML")
            continue

        lead = parse_xml(xml_text, cik, acc, candidate["filed_date"])
        if lead is None:
            print(f"    Skipped: filtered out")
            continue

        # Use name from index if XML didn't provide one
        if not lead["company"]:
            lead["company"] = candidate["company"]

        leads.append(lead)
        print(f"    Added: {lead['company']} | ${lead['amount']:,.0f} | {lead['industry']}")

    # ── Step 3: Sort and write output ─────────────────────────────────────────
    leads.sort(key=lambda x: x["filed_date"], reverse=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(
            {
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "cutoff_date": cutoff.isoformat(),
                "count": len(leads),
                "leads": leads,
            },
            f,
            indent=2,
        )

    print(f"\nDone. {len(leads)} leads written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
