"""
Funding -> GTM job tracker (webhook version).

Pulls funding announcements from RSS feeds, extracts structured data with
Gemini (free tier), filters for GTM-relevant rounds, posts new rows to a
Google Apps Script webhook attached to your sheet.

Required env vars:
  GEMINI_API_KEY    - from aistudio.google.com/apikey (free, no card)
  WEBHOOK_URL       - your Apps Script deployment URL
  WEBHOOK_TOKEN     - the SECRET_TOKEN you set inside Code.gs
"""
import os
import re
import sys
import json
import time
import hashlib
from datetime import datetime, timezone

import feedparser
import requests
from google import genai


# ----- Config -----

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]

# Flash-Lite has more generous free-tier daily limits than Flash.
# If this also rate-limits, fall back to "gemini-2.5-flash-lite" anyway —
# it's the most generous free model.
MODEL = "gemini-2.5-flash-lite"

RSS_SOURCES = [
    "https://techcrunch.com/category/venture/feed/",
    "https://news.crunchbase.com/feed/",
    "https://inc42.com/buzz/feed/",
    "https://entrackr.com/feed/",
    "https://sifted.eu/feed",
]

MIN_GTM_SCORE = 5
QUALIFYING_STAGES = {
    "Pre-seed", "Seed", "Seed Extension",
    "Series A", "Series B", "Series C",
    "Series D+", "Growth",
}

# Stay well under free-tier rate limits.
# 13s between calls = ~4.6 requests/minute (under 5 RPM limit on free tier).
GEMINI_RATE_DELAY_SEC = 13

# Keep per-run volume modest so daily quota lasts. With 5 feeds x 5 items = 25
# Gemini calls/run max, scheduled every 6h = 100/day worst case. Most days
# dedupe will kill most of these before they reach Gemini.
MAX_ITEMS_PER_FEED = 4

# Stop processing further items once we've spent this much daily budget on
# rate-limit retries — better to leave headroom for the next scheduled run.
MAX_RATE_LIMIT_RETRIES_PER_ITEM = 1

# Hard ceiling on total Gemini calls in one script invocation.
# Free tier daily quota is ~20/day per model — keep this low to leave room
# for subsequent runs in the day.
MAX_GEMINI_CALLS_PER_RUN = 15


# ----- Prompt -----

EXTRACTION_PROMPT = """You are evaluating a startup news item for someone hunting GTM (sales, growth, marketing, partnerships, BD, revops) roles at funded startups.

Return ONLY raw JSON, no markdown fences, no commentary. Schema:
{
  "is_funding_round": true|false,
  "company": "name",
  "amount_usd": number or null,
  "stage": "Pre-seed"|"Seed"|"Seed Extension"|"Series A"|"Series B"|"Series C"|"Series D+"|"Growth"|null,
  "sector": "specific (e.g. 'B2B SaaS - HR tech', 'Fintech - lending', 'Vertical AI - legal')",
  "is_b2b": true|false,
  "is_customer_facing": true|false,
  "hq_country": "country" or null,
  "hq_city": "city" or null,
  "lead_investor": "name" or null,
  "founders": ["name1", "name2"],
  "one_liner": "<15 words: what they do",
  "website_guess": "https://...",
  "gtm_fit_score": 0-10 integer (10 = ideal GTM job target: B2B, Series A-C, customer-facing, scaling),
  "gtm_fit_reason": "<15 words why this score",
  "open_gtm_roles_likely": true|false
}

Scoring guide for gtm_fit_score (think: how good a GTM-role target is this company right now?):
- 9-10: Series A/B/C, B2B with clear ICP and product-market fit, scaling phase, recognizable lead VC, founder explicitly mentions hiring/growth/expansion
- 7-8: Solid B2B at Series A+ but smaller raise, or niche sector with strong fundamentals
- 5-6: Seed/Seed extension B2B with a real product, OR Series A+ in adjacent areas (verticalized SaaS, B2B marketplaces, fintech infra)
- 3-4: Pre-seed B2B (founder still doing GTM themselves), OR B2C with strong growth team need
- 1-2: B2C without clear GTM need, very early-stage exploration, niche deeptech
- 0: Acquihires, market commentary, layoff news, IPO filings, fund announcements, leadership changes, pure research with no commercial team

If this isn't a funding round (acquisition, product news, opinion piece, market commentary, layoff/leadership news, IPO filing), return: {"is_funding_round": false}

Title: %TITLE%
Body: %BODY%"""


# ----- Helpers -----

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_items():
    items = []
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
                items.append({
                    "title": strip_html(entry.get("title", "")),
                    "body": strip_html(entry.get("summary", ""))[:2000],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source_feed": url,
                })
        except Exception as e:
            print(f"[warn] failed to fetch {url}: {e}")
    return items


def parse_retry_delay(error_str):
    """Pull retryDelay seconds out of a Gemini error message, e.g. 'Please retry in 19.4s'."""
    m = re.search(r"retry in ([\d.]+)s", str(error_str))
    if m:
        return min(float(m.group(1)) + 1, 60)
    return None


def extract(client, item):
    """Returns parsed dict on success, 'DAILY_QUOTA' if daily limit hit, None on other failure."""
    prompt = EXTRACTION_PROMPT.replace("%TITLE%", item["title"]).replace("%BODY%", item["body"])
    
    for attempt in range(MAX_RATE_LIMIT_RETRIES_PER_ITEM + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
            )
            text = (response.text or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except Exception as e:
            err_str = str(e)
            # Daily quota = stop, save budget for next scheduled run
            if "PerDay" in err_str:
                print(f"[stop] hit daily Gemini quota — stopping run early to preserve next-run capacity")
                return "DAILY_QUOTA"
            # Per-minute quota = wait and retry
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                wait = parse_retry_delay(err_str) or (15 * (attempt + 1))
                print(f"  [retry] minute-quota hit, waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue
            # Temporary outage = brief wait then retry
            if "503" in err_str or "UNAVAILABLE" in err_str:
                print(f"  [retry] Gemini temporarily unavailable, waiting 20s")
                time.sleep(20)
                continue
            print(f"[warn] extract failed for '{item['title'][:60]}': {e}")
            return None
    
    print(f"[warn] extract gave up on '{item['title'][:60]}' after {MAX_RATE_LIMIT_RETRIES_PER_ITEM} retries")
    return None


def should_include(data):
    if not data or data == "DAILY_QUOTA":
        return False
    if not data.get("is_funding_round"):
        return False
    if data.get("gtm_fit_score", 0) < MIN_GTM_SCORE:
        return False
    if not data.get("is_b2b"):
        return False
    if data.get("stage") not in QUALIFYING_STAGES:
        return False
    return True


def fetch_existing_sources():
    try:
        r = requests.get(
            WEBHOOK_URL,
            params={"token": WEBHOOK_TOKEN, "action": "existing_sources"},
            timeout=30,
            allow_redirects=True,
        )
    except Exception as e:
        print(f"\n[FATAL] Could not reach webhook: {e}")
        sys.exit(1)
    
    if r.status_code == 401 or r.status_code == 403:
        print("\n[FATAL] Webhook returned 401/403 Unauthorized.")
        print("Most likely cause: in Apps Script > Deploy > Manage deployments,")
        print("'Who has access' was set to 'Anyone with Google account' instead of 'Anyone'.")
        print("Edit the deployment, change to 'Anyone', create a new version, redeploy.")
        sys.exit(1)
    
    if not r.ok:
        print(f"\n[FATAL] Webhook returned HTTP {r.status_code}")
        print(f"Response preview: {r.text[:300]}")
        sys.exit(1)
    
    try:
        data = r.json()
    except Exception:
        print("\n[FATAL] Webhook didn't return JSON. This usually means the deployment URL is wrong")
        print("or the deployment doesn't exist yet. Re-deploy and update the WEBHOOK_URL secret.")
        print(f"Response preview: {r.text[:300]}")
        sys.exit(1)
    
    if not data.get("ok"):
        print(f"[warn] webhook GET returned: {data}")
        return set()
    
    return set(data.get("sources", []))


def append_rows(rows):
    try:
        r = requests.post(
            WEBHOOK_URL,
            json={"token": WEBHOOK_TOKEN, "rows": rows},
            timeout=60,
        )
    except Exception as e:
        print(f"[error] webhook POST failed: {e}")
        return None
    
    if not r.ok:
        print(f"[error] webhook POST returned HTTP {r.status_code}: {r.text[:300]}")
        return None
    
    try:
        return r.json()
    except Exception:
        print(f"[error] webhook POST didn't return JSON: {r.text[:300]}")
        return None


def dedupe_key(company, link):
    raw = f"{(company or '').lower().strip()}|{link}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def build_row(data, item):
    hq = ", ".join(filter(None, [data.get("hq_city"), data.get("hq_country")]))
    website = (data.get("website_guess") or "").rstrip("/")
    return [
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        data.get("company", ""),
        data.get("stage", ""),
        data.get("amount_usd", ""),
        data.get("sector", ""),
        hq,
        data.get("one_liner", ""),
        "B2B" if data.get("is_b2b") else "B2C",
        data.get("gtm_fit_score", ""),
        data.get("gtm_fit_reason", ""),
        "Yes" if data.get("open_gtm_roles_likely") else "Maybe",
        website,
        f"{website}/careers" if website else "",
        ", ".join(data.get("founders", []) or []),
        data.get("lead_investor", ""),
        item["link"],
        "New",
        "",
        dedupe_key(data.get("company", ""), item["link"]),
    ]


def main():
    client = genai.Client(api_key=GEMINI_KEY)
    already_seen = fetch_existing_sources()
    print(f"Already in sheet: {len(already_seen)} source URLs")

    items = fetch_items()
    print(f"Fetched {len(items)} items from {len(RSS_SOURCES)} feeds")

    fresh = [i for i in items if i["link"] and i["link"] not in already_seen]
    print(f"After URL dedupe: {len(fresh)} new items to send to Gemini")

    if len(fresh) > MAX_GEMINI_CALLS_PER_RUN:
        print(f"Capping to {MAX_GEMINI_CALLS_PER_RUN} items this run (the rest will be picked up next run)")
        fresh = fresh[:MAX_GEMINI_CALLS_PER_RUN]

    new_rows = []
    for i, item in enumerate(fresh, 1):
        print(f"[{i}/{len(fresh)}] {item['title'][:70]}")
        data = extract(client, item)
        
        if data == "DAILY_QUOTA":
            break
        
        if should_include(data):
            new_rows.append(build_row(data, item))
            print(f"  + KEPT: {data.get('company')} ({data.get('stage')}) — score {data.get('gtm_fit_score')}")
        
        time.sleep(GEMINI_RATE_DELAY_SEC)

    if new_rows:
        result = append_rows(new_rows)
        if result and result.get("ok"):
            print(f"\nAppended {result.get('appended')} new rows to sheet")
        else:
            print(f"\n[warn] failed to append rows: {result}")
    else:
        print("\nNo new qualifying rows this run")


if __name__ == "__main__":
    main()
