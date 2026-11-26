"""Resolve each company's real job board and count the roles that matter.

Why this exists
---------------
The tracker inferred "is this company hiring?" from news prose, and wrote a
Careers Page column built by string concatenation (``website + "/careers"``) that
was never fetched. The result: the column was ~100% populated and ~0% verified,
and `Hiring Signal` said "Unknown" on 345 rows.

Greenhouse, Lever, Ashby and Workable each publish an official, keyless, public
JSON endpoint for a company's job board -- the same one the employer's own
careers page calls to embed its openings. That makes three things first-party
fact rather than inference:

  * how many roles the company has open   -> a real headcount/urgency proxy
  * whether a growth/BD/RevOps role is open -> a stated need, not a guess
  * when it was last confirmed             -> freshness

A company with an open Growth role on its Ashby board is a stronger lead than
one that raised $12M six weeks ago: the first is a stated need, the second is a
guess. Funding is the discovery layer; this is the qualifying layer.

Only public endpoints are used. No login, no cookies, no scraping of
aggregators, and nothing that is not already published for syndication.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

USER_AGENT = os.getenv(
    "ATS_USER_AGENT",
    "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0; +https://github.com/sujuj07/gtm-funding-tracker)",
)
TIMEOUT = (5, 15)
# Be a polite client: this walks a few hundred third-party sites per run.
REQUEST_DELAY = float(os.getenv("ATS_REQUEST_DELAY", "0.6"))
MAX_COMPANIES_PER_RUN = int(os.getenv("ATS_MAX_COMPANIES_PER_RUN", "60"))

# Careers paths worth trying when the homepage does not link a board directly.
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/jobs/", "/about/careers", "/company/careers")

# ---------------------------------------------------------------- board detection

# Each entry: platform -> (regex over page HTML, public JSON endpoint template).
# Slug charset is kept tight so a stray URL fragment cannot be mistaken for a board.
_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.-]{1,60})"
BOARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?" + _SLUG, re.I)),
    ("greenhouse", re.compile(r"job-boards\.greenhouse\.io/" + _SLUG, re.I)),
    ("greenhouse", re.compile(_SLUG + r"\.greenhouse\.io", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/" + _SLUG, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/" + _SLUG + r"/embed", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/" + _SLUG, re.I)),
    ("recruitee", re.compile(_SLUG + r"\.recruitee\.com", re.I)),
)

# Endpoints are the vendors' own documented public job-board APIs.
BOARD_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
}

BOARD_HUMAN_URL = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com/",
}

# Slugs that show up in boilerplate ("powered by greenhouse") and are never a board.
SLUG_BLOCKLIST = {
    "embed", "job_board", "www", "api", "boards", "jobs", "static", "assets",
    "support", "help", "docs", "blog", "greenhouse", "lever", "ashbyhq", "workable",
}

# ---------------------------------------------------------------- role matching

# The roles worth reaching out about: growth, BD, revenue ops and adjacent GTM.
TARGET_ROLE = re.compile(
    r"\b("
    r"growth|business\s+development|\bbd\b|bizdev|"
    r"revenue\s+(operations|ops)|\brevops\b|sales\s+(operations|ops)|"
    r"\bgtm\b|go[-\s]to[-\s]market|"
    r"partnerships?|alliances|"
    r"demand\s+gen(eration)?|marketing\s+(operations|ops)|lifecycle\s+marketing|"
    r"account\s+executive|\bae\b|sales\s+development|\bsdr\b|\bbdr\b|"
    r"commercial\s+(lead|manager|director)|"
    r"community\s+(lead|manager)|"
    r"product\s+marketing|\bpmm\b"
    r")\b",
    re.I,
)

# Engineering/other roles are counted too (as total headcount signal) but these
# patterns let the caller see whether a board is GTM-shaped or purely technical.
SENIOR_HINT = re.compile(r"\b(head|lead|director|vp|chief|principal|manager|senior|staff)\b", re.I)


def _get(url: str, *, as_json: bool = False) -> Any:
    """Single GET with a shared UA and timeout. Returns None on any failure.

    Enrichment is best-effort: one unreachable site must never fail the run.
    """
    try:
        response = requests.get(
            url, timeout=TIMEOUT, allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        if response.status_code != 200:
            return None
        return response.json() if as_json else response.text
    except Exception:
        return None
    finally:
        time.sleep(REQUEST_DELAY)


def detect_board(html: str) -> tuple[str, str] | None:
    """Find an ATS board reference in page HTML.

    Returns ``(platform, slug)`` or None. First match wins, and patterns are
    ordered most-specific first so a vendor's own marketing link cannot shadow a
    real board URL.
    """
    if not html:
        return None
    for platform, pattern in BOARD_PATTERNS:
        for match in pattern.finditer(html):
            slug = (match.group(1) or "").strip(" /.-")
            if not slug or slug.lower() in SLUG_BLOCKLIST:
                continue
            if len(slug) < 2:
                continue
            return platform, slug
    return None


def find_board_for_site(website: str) -> tuple[str, str, str] | None:
    """Locate a company's job board starting from its website.

    Tries the homepage first (most sites link careers in the footer), then a
    short list of conventional careers paths. Returns
    ``(platform, slug, page_url_where_found)`` or None.

    Returning None is a legitimate, useful answer: plenty of seed-stage companies
    have no ATS board at all. Recording that honestly is the whole point --
    the previous code could only ever claim a careers page existed.
    """
    if not website:
        return None
    base = website.rstrip("/")
    candidates = [base] + [base + path for path in CAREERS_PATHS]
    for url in candidates:
        html = _get(url)
        found = detect_board(html or "")
        if found:
            return found[0], found[1], url
    return None


# ---------------------------------------------------------------- board parsing


def _normalise_jobs(platform: str, payload: Any) -> list[dict]:
    """Flatten each vendor's response into ``[{title, location, url}]``."""
    jobs: list[dict] = []
    if payload is None:
        return jobs

    if platform == "greenhouse":
        for job in (payload or {}).get("jobs", []) or []:
            jobs.append({
                "title": str(job.get("title") or ""),
                "location": str(((job.get("location") or {}) or {}).get("name") or ""),
                "url": str(job.get("absolute_url") or ""),
            })
    elif platform == "lever":
        for job in payload or []:
            categories = job.get("categories") or {}
            jobs.append({
                "title": str(job.get("text") or ""),
                "location": str(categories.get("location") or ""),
                "url": str(job.get("hostedUrl") or ""),
            })
    elif platform == "ashby":
        for job in (payload or {}).get("jobs", []) or []:
            jobs.append({
                "title": str(job.get("title") or ""),
                "location": str(job.get("location") or ""),
                "url": str(job.get("jobUrl") or ""),
            })
    elif platform == "workable":
        for job in (payload or {}).get("jobs", []) or []:
            jobs.append({
                "title": str(job.get("title") or ""),
                "location": str(job.get("location") or job.get("city") or ""),
                "url": str(job.get("url") or job.get("application_url") or ""),
            })
    elif platform == "recruitee":
        for job in (payload or {}).get("offers", []) or []:
            jobs.append({
                "title": str(job.get("title") or ""),
                "location": str(job.get("location") or ""),
                "url": str(job.get("careers_url") or ""),
            })

    return [job for job in jobs if job["title"]]


def fetch_board_jobs(platform: str, slug: str) -> list[dict] | None:
    """Fetch and normalise the public board. None means the board was unreachable.

    None and [] mean different things and must not be conflated: None is "could
    not check", [] is "checked, and there are genuinely no open roles". Writing
    0 for an unreachable board would invent a fact.
    """
    template = BOARD_API.get(platform)
    if not template:
        return None
    payload = _get(template.format(slug=slug), as_json=True)
    if payload is None:
        return None
    return _normalise_jobs(platform, payload)


def summarise_jobs(jobs: list[dict]) -> dict:
    """Count total roles and the growth/BD/RevOps subset."""
    target = [job for job in jobs if TARGET_ROLE.search(job["title"])]
    senior_target = [job for job in target if SENIOR_HINT.search(job["title"])]
    return {
        "open_roles": len(jobs),
        "target_roles": len(target),
        "target_titles": "; ".join(job["title"] for job in target[:6]),
        "senior_target_roles": len(senior_target),
        "sample_target_url": target[0]["url"] if target else "",
    }


def enrich_company(website: str) -> dict:
    """Full pass for one company: site -> board -> role counts.

    Always returns a dict. ``status`` distinguishes the outcomes so the sheet can
    show the difference between "no board found" and "board found, zero roles".
    """
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blank = {
        "platform": "", "slug": "", "board_url": "", "open_roles": "",
        "target_roles": "", "target_titles": "", "senior_target_roles": "",
        "sample_target_url": "", "checked_at": checked, "status": "no website",
    }
    if not website:
        return blank

    found = find_board_for_site(website)
    if not found:
        return {**blank, "status": "no ATS board found"}

    platform, slug, _page = found
    jobs = fetch_board_jobs(platform, slug)
    result = {
        **blank,
        "platform": platform,
        "slug": slug,
        "board_url": BOARD_HUMAN_URL[platform].format(slug=slug),
        "status": "board found",
    }
    if jobs is None:
        result["status"] = "board found, API unreachable"
        return result

    result.update(summarise_jobs(jobs))
    result["status"] = "ok" if jobs else "board found, no open roles"
    return result


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python ats_enrich.py <website> [<website> ...]")
        raise SystemExit(2)
    for site in sys.argv[1:MAX_COMPANIES_PER_RUN + 1]:
        print(json.dumps({"website": site, **enrich_company(site)}, indent=2))
