"""
Funding -> GTM job tracker (webhook version).

Pulls funding announcements from RSS feeds, extracts structured data with
Gemini 2.5 Flash (free tier), filters for GTM-relevant rounds, posts new
rows to a Google Apps Script webhook attached to your sheet.

Required env vars:
  GEMINI_API_KEY    - from aistudio.google.com/apikey (free, no card)
  WEBHOOK_URL       - your Apps Script deployment URL
  WEBHOOK_TOKEN     - the SECRET_TOKEN you set inside Code.gs
"""
import os
import re
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

RSS_SOURCES = [
    "https://techcrunch.com/category/venture/feed/",
    "https://news.crunchbase.com/feed/",
    "https://inc42.com/buzz/feed/",
    "https://entrackr.com/feed/",
    "https://sifted.eu/feed",
]

MIN_GTM_SCORE = 6
QUALIFYING_STAGES = {
    "Seed Extension", "Series A", "Series B",
    "Series C", "Series D+", "Growth",
}

GEMINI_RATE_DELAY_SEC = 4
MAX_ITEMS_PER_FEED = 25


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

Scoring guide for gtm_fit_score:
- 9-10: Series A/B/C, B2B SaaS, clear ICP, scaling phase, recognizable lead VC
- 6-8: B2B with some flag (early-stage, niche sector, smaller raise)
- 3-5: B2C, very early seed, or tangential GTM need
- 0-2: Acquihires, biotech with no commercial team, infra-only research, deeptech with no customers

If this isn't a funding round (acquisition, product news, opinion piece), return: {"is_funding_round": false}

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


def extract(client, item):
    prompt = EXTRACTION_PROMPT.replace("%TITLE%", item["title"]).replace("%BODY%", item["body"])
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
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
        print(f"[warn] extract failed for '{item['title'][:60]}': {e}")
        return None


def should_include(data):
    if not data or not data.get("is_funding_round"):
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
        )
        data = r.json()
        if data.get("ok"):
            return set(data.get("sources", []))
        print(f"[warn] couldn't fetch existing sources: {data}")
    except Exception as e:
        print(f"[warn] couldn't fetch existing sources: {e}")
    return set()


def append_rows(rows):
    r = requests.post(
        WEBHOOK_URL,
        json={"token": WEBHOOK_TOKEN, "rows": rows},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


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

    new_rows = []
    for item in fresh:
        data = extract(client, item)
        if should_include(data):
            new_rows.append(build_row(data, item))
            print(f"  + {data.get('company')} ({data.get('stage')}) - score {data.get('gtm_fit_score')}")
        time.sleep(GEMINI_RATE_DELAY_SEC)

    if new_rows:
        result = append_rows(new_rows)
        print(f"\nWebhook response: {result}")
    else:
        print("\nNo new qualifying rows this run")


if __name__ == "__main__":
    main()
