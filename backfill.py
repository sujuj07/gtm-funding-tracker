"""
Backfill historical funding rounds via Google News RSS date-range search.
Groq version (Llama 3.3 70B, 1,000 requests/day free tier).

With Groq's 1,000/day quota you can process a full multi-month backfill in
one or two runs instead of weeks of grinding. Edit DATE_FROM / DATE_TO,
then run the backfill workflow from the Actions tab.

Required env vars: GROQ_API_KEY, WEBHOOK_URL, WEBHOOK_TOKEN
"""
import os
import re
import json
import time
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from groq import Groq


# ----- Config -----

GROQ_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]

MODEL = "llama-3.1-8b-instant"

# ====== EDIT THESE TWO LINES TO SET YOUR BACKFILL WINDOW ======
DATE_FROM = "2026-04-01"   # earliest date to pull from
DATE_TO = "2026-05-29"     # latest date (usually today)
# ==============================================================

SEARCH_QUERIES = [
    "startup raises funding round",
    "startup Series A funding",
    "startup Series B funding",
    "startup seed funding round",
    # Crypto / Web3 / DePIN
    "crypto startup raises funding",
    "web3 startup funding round",
    "blockchain startup funding",
    "DePIN startup funding",
]

WINDOW_DAYS = 14

MIN_GTM_SCORE = 5
QUALIFYING_STAGES = {
    "Pre-seed", "Seed", "Seed Extension",
    "Series A", "Series B", "Series C",
    "Series D+", "Growth",
}

# Save progress to the sheet every N items so timeouts don't lose work.
# Combined with company-level dedupe, subsequent runs cleanly resume.
SAVE_BATCH_SIZE = 20

# 5s delay = 12/min, comfortably under the 8B free-tier TPM/RPM limits with
# headroom for retries on busy moments.
RATE_DELAY_SEC = 5
MAX_RETRIES_PER_ITEM = 2

# 150 items x 5s = ~12 min processing + ~5 min for feed fetching = ~17 min.
# Well under any reasonable timeout. Re-run 2-3x to clear a 3-month backlog.
MAX_CALLS_PER_RUN = 150
MAX_ITEMS_PER_QUERY = 20


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

For crypto / web3 / blockchain / DePIN companies: treat ecosystem BD, developer relations, growth, and partnerships as valid GTM functions. Score them on the same scale even if they're protocol- or token-based rather than classic B2B SaaS, and set is_b2b true if they sell to or partner with other businesses/developers.

If this isn't a funding round, return: {"is_funding_round": false}

Title: %TITLE%
Body: %BODY%"""


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def date_windows(date_from, date_to, window_days):
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    cur = start
    while cur < end:
        win_end = min(cur + timedelta(days=window_days), end)
        yield cur.strftime("%Y-%m-%d"), win_end.strftime("%Y-%m-%d")
        cur = win_end


def google_news_rss_url(query, after, before):
    full_query = f"{query} after:{after} before:{before}"
    encoded = urllib.parse.quote(full_query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def fetch_historical_items():
    items = []
    seen_links = set()
    for after, before in date_windows(DATE_FROM, DATE_TO, WINDOW_DAYS):
        for query in SEARCH_QUERIES:
            url = google_news_rss_url(query, after, before)
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:MAX_ITEMS_PER_QUERY]:
                    link = entry.get("link", "")
                    if link and link not in seen_links:
                        seen_links.add(link)
                        items.append({
                            "title": strip_html(entry.get("title", "")),
                            "body": strip_html(entry.get("summary", ""))[:2000],
                            "link": link,
                            "published": entry.get("published", ""),
                            "published_parsed": entry.get("published_parsed"),
                        })
            except Exception as e:
                print(f"[warn] query failed ({query}, {after}-{before}): {e}")
            time.sleep(1)
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
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in err_str:
                m = re.search(r"try again in ([\d.]+)s", err_str)
                wait = (float(m.group(1)) + 1) if m else (20 * (attempt + 1))
                wait = min(wait, 60)
                print(f"  [retry] rate limited, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if "503" in err_str or "502" in err_str or "500" in err_str:
                print("  [retry] Groq temporarily unavailable, waiting 15s")
                time.sleep(15)
                continue
            print(f"[warn] extract failed for '{item['title'][:60]}': {e}")
            return None
    return None


CRYPTO_KEYWORDS = {
    "crypto", "web3", "blockchain", "defi", "depin",
    "token", "onchain", "on-chain", "decentralized", "stablecoin",
}


def is_crypto(data):
    haystack = f"{(data.get('sector') or '')} {(data.get('one_liner') or '')}".lower()
    return any(kw in haystack for kw in CRYPTO_KEYWORDS)


def should_include(data):
    if not data:
        return False
    if not data.get("is_funding_round"):
        return False
    if data.get("gtm_fit_score", 0) < MIN_GTM_SCORE:
        return False
    if not data.get("is_b2b") and not is_crypto(data):
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
        print(f"[warn] webhook GET returned: {data}")
    except Exception as e:
        print(f"[warn] couldn't fetch existing sources: {e}")
    return set()


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
    # Use the article's actual publish date, not today
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        pp = item.get("published_parsed")
        if pp:
            date_str = time.strftime("%Y-%m-%d", pp)
    except Exception:
        pass
    return [
        date_str,
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
    print(f"Backfilling funding rounds from {DATE_FROM} to {DATE_TO}")
    client = Groq(api_key=GROQ_KEY)
    already_seen = fetch_existing_sources()
    print(f"Already in sheet: {len(already_seen)} source URLs")

    items = fetch_historical_items()
    print(f"Found {len(items)} unique historical items from Google News")

    fresh = [i for i in items if i["link"] and i["link"] not in already_seen]
    print(f"After dedupe: {len(fresh)} new items")

    if len(fresh) > MAX_CALLS_PER_RUN:
        print(f"Capping to {MAX_CALLS_PER_RUN} this run — re-run to continue the backlog")
        fresh = fresh[:MAX_CALLS_PER_RUN]

    pending = []          # rows accumulated since last save
    total_saved = 0       # cumulative saved this run

    def flush(reason):
        nonlocal pending, total_saved
        if not pending:
            return
        result = append_rows(pending)
        if result and result.get("ok"):
            n = result.get("appended", 0)
            skipped = result.get("skipped", 0)
            total_saved += n
            print(f"  [save:{reason}] flushed {n} rows (webhook dedupe skipped {skipped}). Total this run: {total_saved}")
        else:
            print(f"  [save:{reason}] FAILED to flush — keeping rows in memory: {result}")
            return  # don't clear pending so we retry next flush
        pending = []

    try:
        for i, item in enumerate(fresh, 1):
            print(f"[{i}/{len(fresh)}] {item['title'][:70]}")
            data = extract(client, item)
            if should_include(data):
                pending.append(build_row(data, item))
                print(f"  + KEPT: {data.get('company')} ({data.get('stage')}) — score {data.get('gtm_fit_score')}")

            # Save in batches so a timeout doesn't lose work
            if len(pending) >= SAVE_BATCH_SIZE:
                flush("batch")

            time.sleep(RATE_DELAY_SEC)
    finally:
        # Always flush remaining pending rows, even on error/interrupt
        flush("final")
        print(f"\nDone. Saved {total_saved} rows this run. Re-run to process more of the backlog.")


if __name__ == "__main__":
    main()
