"""
Funding -> GTM job tracker (Groq version).

Pulls funding announcements from RSS feeds, extracts structured data with
Groq (Llama 3.3 70B, free tier - 1,000 requests/day), filters for
GTM-relevant rounds, posts new rows to a Google Apps Script webhook.

Required env vars:
  GROQ_API_KEY    - from console.groq.com/keys (free, no card)
  WEBHOOK_URL     - your Apps Script deployment URL
  WEBHOOK_TOKEN   - the SECRET_TOKEN you set inside Code.gs
"""
import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone

import feedparser
import requests
from groq import Groq


# ----- Config -----

GROQ_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]

# Llama 3.3 70B: high quality, 1,000 requests/day free tier.
# Alternative: "llama-3.1-8b-instant" (14,400/day, faster, slightly lower quality)
MODEL = "llama-3.3-70b-versatile"

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

# Groq free tier for llama-3.3-70b: 6,000 tokens/min. Each call ~1,350 tokens,
# so ~4.4 calls/min max. 15s delay = ~4/min, safely under the TPM cap.
RATE_DELAY_SEC = 15
MAX_RETRIES_PER_ITEM = 2

# Per-run cap to fit inside the 20-min GitHub Actions timeout.
# 60 items x 15s = 15 min, leaving margin. Daily RPD (1,000) is not the
# constraint — the per-run timeout is. Leftover items get picked up next run.
MAX_ITEMS_PER_FEED = 12
MAX_CALLS_PER_RUN = 60


# ----- Prompt -----

EXTRACTION_PROMPT = """You are evaluating a startup news item for someone hunting GTM (sales, growth, marketing, partnerships, BD, revops) roles at funded startups.

Return ONLY a JSON object, no markdown, no commentary. Schema:
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

Scoring guide for gtm_fit_score (how good a GTM-role target is this company right now?):
- 9-10: Series A/B/C, B2B with clear ICP and product-market fit, scaling phase, recognizable lead VC, founder mentions hiring/growth
- 7-8: Solid B2B at Series A+ but smaller raise, or niche sector with strong fundamentals
- 5-6: Seed/Seed extension B2B with a real product, OR Series A+ in adjacent areas
- 3-4: Pre-seed B2B (founder still doing GTM themselves), OR B2C with strong growth team need
- 1-2: B2C without clear GTM need, very early-stage exploration, niche deeptech
- 0: Acquihires, market commentary, layoff news, IPO filings, fund announcements, leadership changes, pure research

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


def extract(client, item):
    prompt = EXTRACTION_PROMPT.replace("%TITLE%", item["title"]).replace("%BODY%", item["body"])
    for attempt in range(MAX_RETRIES_PER_ITEM + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            text = response.choices[0].message.content or ""
            return json.loads(text)
        except Exception as e:
            err_str = str(e)
            # Rate limit — back off and retry
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in err_str:
                m = re.search(r"try again in ([\d.]+)s", err_str)
                wait = (float(m.group(1)) + 1) if m else (20 * (attempt + 1))
                wait = min(wait, 60)
                print(f"  [retry] rate limited, waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue
            # Service issue — brief wait
            if "503" in err_str or "502" in err_str or "500" in err_str:
                print("  [retry] Groq temporarily unavailable, waiting 15s")
                time.sleep(15)
                continue
            print(f"[warn] extract failed for '{item['title'][:60]}': {e}")
            return None
    print(f"[warn] gave up on '{item['title'][:60]}' after {MAX_RETRIES_PER_ITEM} retries")
    return None


def should_include(data):
    if not data:
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
        )
    except Exception as e:
        print(f"\n[FATAL] Could not reach webhook: {e}")
        raise SystemExit(1)

    if r.status_code in (401, 403):
        print("\n[FATAL] Webhook 401/403. In Apps Script > Manage deployments,")
        print("set 'Who has access' to 'Anyone' (not 'Anyone with Google account').")
        raise SystemExit(1)
    if not r.ok:
        print(f"\n[FATAL] Webhook HTTP {r.status_code}: {r.text[:200]}")
        raise SystemExit(1)
    try:
        data = r.json()
    except Exception:
        print("\n[FATAL] Webhook didn't return JSON — URL likely wrong or not deployed.")
        print(f"Response preview: {r.text[:200]}")
        raise SystemExit(1)
    if not data.get("ok"):
        print(f"[warn] webhook GET returned: {data}")
        return set()
    return set(data.get("sources", []))


def append_rows(rows):
    try:
        r = requests.post(WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "rows": rows}, timeout=60)
        if not r.ok:
            print(f"[error] webhook POST HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[error] webhook POST failed: {e}")
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
    client = Groq(api_key=GROQ_KEY)
    already_seen = fetch_existing_sources()
    print(f"Already in sheet: {len(already_seen)} source URLs")

    items = fetch_items()
    print(f"Fetched {len(items)} items from {len(RSS_SOURCES)} feeds")

    fresh = [i for i in items if i["link"] and i["link"] not in already_seen]
    print(f"After URL dedupe: {len(fresh)} new items to send to Groq")

    if len(fresh) > MAX_CALLS_PER_RUN:
        print(f"Capping to {MAX_CALLS_PER_RUN} items this run (rest picked up next run)")
        fresh = fresh[:MAX_CALLS_PER_RUN]

    new_rows = []
    for i, item in enumerate(fresh, 1):
        print(f"[{i}/{len(fresh)}] {item['title'][:70]}")
        data = extract(client, item)
        if should_include(data):
            new_rows.append(build_row(data, item))
            print(f"  + KEPT: {data.get('company')} ({data.get('stage')}) — score {data.get('gtm_fit_score')}")
        time.sleep(RATE_DELAY_SEC)

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
