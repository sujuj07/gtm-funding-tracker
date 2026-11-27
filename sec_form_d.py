"""SEC Form D filings as a funding source.

Why this is different from every other source in the pipeline
-------------------------------------------------------------
Every other source is a news article: an LLM reads prose and guesses at
structured fields, and `validate.py` exists to catch it guessing wrong.

Form D is a **filing**, not a story. Companies raising under Regulation D -- which
covers most US seed and Series A rounds -- must file one with the SEC, under
penalty of perjury, and the SEC publishes it as structured XML. So this path
needs no model call at all:

    company name      <- entityName
    raise amount      <- offeringData/totalAmountSold
    HQ                <- issuer address
    founding year     <- yearOfInc          (the "new company" signal, stated)
    founder names     <- relatedPersonsList (officers and directors)
    industry          <- industryGroup

That makes it both the most accurate source available and the cheapest: no
tokens, no extraction failures, no validation rejections.

It also fills the two fields the news sources almost never carry. Founding year
was empty on essentially every row, and founders on ~88%.

Scope and limits
----------------
* **US only.** Form D is a US filing. It adds nothing for India or Europe.
* **Amount filed is amount *sold at filing time*.** A company may file early
  having sold little of its offering, so `totalAmountSold` can understate or be
  zero even on a real round. Zero is reported as unknown rather than as $0.
* **No narrative.** There is no one-liner or sector description, because a filing
  does not contain one. Those fields stay empty rather than being invented.
* The SEC requires a declared User-Agent with contact details and rate-limits to
  10 requests/second. Both are respected below.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests

# The SEC requires a descriptive User-Agent naming the requester. Override via
# env so a fork does not impersonate this one.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "gtm-funding-tracker/1.0 (github.com/sujuj07/gtm-funding-tracker)",
)
SEC_BASE = "https://www.sec.gov"
TIMEOUT = (5, 25)
# SEC's published ceiling is 10 req/s; stay an order of magnitude under it.
REQUEST_DELAY = float(os.getenv("SEC_REQUEST_DELAY", "0.25"))

MAX_FILINGS_PER_RUN = int(os.getenv("SEC_MAX_FILINGS_PER_RUN", "40"))
LOOKBACK_DAYS = int(os.getenv("SEC_LOOKBACK_DAYS", "3"))

# Only these Form D industry groups are relevant to the target sectors. Form D's
# taxonomy is coarse, so this is a filter against obvious noise (real estate,
# oil and gas, agriculture) rather than a precise sector match.
RELEVANT_INDUSTRIES = {
    "Computers", "Other Technology", "Telecommunications", "Biotechnology",
    "Electronics", "Health Care", "Other Health Care", "Pharmaceuticals",
    "Commercial Banking", "Insurance", "Investing", "Other Banking and Financial Services",
    "Manufacturing", "Airlines and Airports", "Business Services",
    "Industrial", "Technology", "Energy Conservation", "Other Energy",
}
# Groups never worth surfacing for this purpose.
EXCLUDED_INDUSTRIES = {
    "Pooled Investment Fund", "Other Real Estate", "Residential",
    "Commercial", "REITS and Finance", "Construction", "Oil and Gas",
    "Agriculture", "Restaurants", "Retailing", "Travel",
}

_NS = re.compile(r"\{[^}]*\}")


def _text(node, path: str, default: str = "") -> str:
    """Namespace-agnostic single-value lookup."""
    if node is None:
        return default
    found = node.find(path)
    return (found.text or default).strip() if found is not None and found.text else default


def _strip_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        element.tag = _NS.sub("", element.tag)
    return root


def _get(url: str) -> str | None:
    """Single GET against the SEC with the required headers. None on any failure."""
    try:
        response = requests.get(
            url, timeout=TIMEOUT,
            headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )
        if response.status_code != 200:
            return None
        return response.text
    except Exception:
        return None
    finally:
        time.sleep(REQUEST_DELAY)


def daily_index_urls(lookback_days: int = LOOKBACK_DAYS) -> list[str]:
    """Daily EDGAR form indexes for the last N days.

    Weekends and holidays simply 404 and are skipped; the SEC does not publish an
    index on non-business days.
    """
    urls = []
    today = datetime.now(timezone.utc).date()
    for offset in range(lookback_days + 1):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:          # Saturday or Sunday
            continue
        quarter = (day.month - 1) // 3 + 1
        urls.append(
            f"{SEC_BASE}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/"
            f"form.{day.strftime('%Y%m%d')}.idx"
        )
    return urls


def parse_form_d_entries(index_text: str) -> list[dict]:
    """Pull Form D rows out of a daily form index.

    The .idx is fixed-width-ish and pipe-free, so fields are split on runs of
    whitespace from the right: the last two columns (date, filename) are stable
    while the company name may contain spaces.
    """
    entries = []
    for line in (index_text or "").splitlines():
        if not line.startswith("D "):
            continue
        # "D   COMPANY NAME   CIK   DATE   edgar/data/.../xxx.txt"
        parts = line.split()
        if len(parts) < 5:
            continue
        filename = parts[-1]
        filed_date = parts[-2]
        cik = parts[-3]
        company = " ".join(parts[1:-3]).strip()
        if not company or not filename.endswith(".txt"):
            continue
        entries.append({
            "company": company,
            "cik": cik,
            "filed_date": filed_date,
            "txt_path": filename,
        })
    return entries


def primary_doc_url(cik: str, txt_path: str) -> str:
    """Derive the structured XML URL from the submission text path.

    edgar/data/1234567/000123456726000123.txt
      -> /Archives/edgar/data/1234567/000123456726000123/primary_doc.xml
    """
    accession = txt_path.rsplit("/", 1)[-1].removesuffix(".txt")
    return f"{SEC_BASE}/Archives/edgar/data/{cik}/{accession}/primary_doc.xml"


def _parse_amount(raw: str) -> float | None:
    """Form D amounts are plain integers in dollars."""
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    # 0 means "nothing sold yet at filing time", which is not a $0 round.
    return value if value > 0 else None


def parse_primary_doc(xml_text: str) -> dict | None:
    """Parse a Form D primary_doc.xml into the pipeline's `clean` row shape.

    Returns None when the document is not a parseable Form D. Every field comes
    straight from the filing: nothing here is inferred.
    """
    try:
        root = _strip_namespaces(ET.fromstring(xml_text))
    except ET.ParseError:
        return None

    entity = root.find(".//primaryIssuer")
    if entity is None:
        return None

    company = _text(entity, "entityName")
    if not company:
        return None

    year_raw = _text(entity, "yearOfInc/value") or _text(entity, "yearOfInc/yearOfIncorporation")
    over_five = _text(entity, "yearOfInc/overFiveYears").lower() == "true"
    founded_year = None
    if year_raw.isdigit() and 1800 <= int(year_raw) <= datetime.now(timezone.utc).year + 1:
        founded_year = int(year_raw)

    city = _text(entity, "issuerAddress/city")
    state = _text(entity, "issuerAddress/stateOrCountry")
    # stateOrCountry carries a US state code for domestic issuers ("CA") and a
    # country code for foreign ones. Keep the state with the city and report the
    # country separately, rather than writing "CA" into an HQ country column.
    hq_city = ", ".join(part for part in (city.title() if city else "", state) if part)

    offering = root.find(".//offeringData")
    industry = _text(offering, "industryGroup/industryGroupType") if offering is not None else ""
    amount = _parse_amount(_text(offering, "offeringSalesAmounts/totalAmountSold")) if offering is not None else None
    total_offering = _parse_amount(_text(offering, "offeringSalesAmounts/totalOfferingAmount")) if offering is not None else None

    # Officers and directors named on the filing. The closest thing to a verified
    # founder list available anywhere in this pipeline.
    people: list[str] = []
    for person in root.findall(".//relatedPersonInfo"):
        name_node = person.find("relatedPersonName")
        first = _text(name_node, "firstName")
        last = _text(name_node, "lastName")
        full = " ".join(part for part in (first, last) if part).strip()
        if full and full not in people:
            people.append(full)

    is_new = bool(founded_year and (datetime.now(timezone.utc).year - founded_year) <= 3) or (
        not over_five and not founded_year
    )

    return {
        "company": company,
        "event_type": "Funding round",
        "stage": "Unknown",            # Form D does not name a round stage.
        "amount_usd": amount,
        "valuation_usd": None,
        "sector": industry,
        "one_liner": "",               # A filing contains no description.
        "is_b2b": False,               # Not stated; left false rather than guessed.
        "hiring_flag": "Unknown",
        "hiring_text": "",
        "website": "",
        "founders": people[:6],
        "lead_investor": "",
        "other_investors": [],
        "broad_offerings": "",
        "niche_presence": "",
        "product_type": "",
        "target_customers": "",
        "hq_city": hq_city,
        "hq_country": "United States" if len(state) == 2 and state.isalpha() else (state or "United States"),
        "confidence": "High",          # A filing under penalty of perjury.
        "team_size": None,             # Not in Form D.
        "founded_year": founded_year,
        "event_date": "",              # Filled from the filing date by the caller.
        "_total_offering": total_offering,
        "_is_new_company": is_new,
    }


def is_relevant(clean: dict) -> bool:
    """Filter out filings that are not operating-company raises.

    Pooled investment funds are the big one: a VC fund raising its own vehicle
    files a Form D, and those were already polluting the sheet from news sources.
    """
    industry = (clean.get("sector") or "").strip()
    if industry in EXCLUDED_INDUSTRIES:
        return False
    if RELEVANT_INDUSTRIES and industry and industry not in RELEVANT_INDUSTRIES:
        return False
    name = (clean.get("company") or "").lower()
    if re.search(r"\b(fund|fund\s+[ivxl]+|lp|l\.p\.|partners\s+fund|capital\s+partners)\b", name):
        return False
    return True


def fetch_form_d_rows(max_filings: int = MAX_FILINGS_PER_RUN,
                      lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch recent Form D filings as ready-to-write `clean` dicts.

    Each returned dict carries `_source_url` and `_published` so the caller can
    build a sheet row without a model call.
    """
    rows: list[dict] = []
    seen_companies: set[str] = set()

    for index_url in daily_index_urls(lookback_days):
        if len(rows) >= max_filings:
            break
        index_text = _get(index_url)
        if not index_text:
            continue                      # weekend, holiday, or not yet published
        entries = parse_form_d_entries(index_text)
        print(f"  [sec] {index_url.rsplit('/', 1)[-1]}: {len(entries)} Form D filing(s)")

        for entry in entries:
            if len(rows) >= max_filings:
                break
            key = entry["company"].lower().strip()
            if key in seen_companies:
                continue
            seen_companies.add(key)

            xml_url = primary_doc_url(entry["cik"], entry["txt_path"])
            xml_text = _get(xml_url)
            if not xml_text:
                continue
            clean = parse_primary_doc(xml_text)
            if not clean or not is_relevant(clean):
                continue

            filed = entry["filed_date"]
            try:
                clean["event_date"] = datetime.strptime(filed, "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                clean["event_date"] = filed[:10]

            clean["_source_url"] = f"{SEC_BASE}/Archives/{entry['txt_path']}"
            clean["_published"] = clean["event_date"]
            clean["_publisher"] = "SEC EDGAR (Form D)"
            rows.append(clean)

    return rows


if __name__ == "__main__":
    import json
    found = fetch_form_d_rows()
    print(f"\n{len(found)} relevant Form D filing(s)")
    for row in found[:10]:
        print(json.dumps({k: v for k, v in row.items() if not k.startswith("_")}, default=str))
