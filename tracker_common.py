"""Shared helpers for the GTM / startup-intelligence trackers."""
from __future__ import annotations

import hashlib
import json
from email.utils import parsedate_to_datetime
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from groq import Groq

MODEL = (os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b")
GROQ_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
DEFILLAMA_API_KEY = os.getenv("DEFILLAMA_API_KEY", "").strip()


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def fetch_official_social_links(website: str) -> list[str]:
    """Extract actual social/community/docs links linked from the supplied project homepage.

    This intentionally does not guess handles or construct URLs. Only links found in the
    homepage HTML are returned; failures leave the spreadsheet cell blank.
    """
    value = str(website or "").strip()
    if not value:
        return []
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    try:
        parsed_input = urllib.parse.urlparse(value)
        host = (parsed_input.hostname or "").lower().strip(".").removeprefix("www.")
        if parsed_input.scheme not in {"http", "https"} or not host or host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local"):
            return []
        try:
            import ipaddress
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return []
        except ValueError:
            pass
        response = requests.get(
            value, timeout=(3, 6), allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0)"},
        )
        response.raise_for_status()
        # Keep parsing bounded if a site returns an unusually large response.
        page = response.text[:900_000]
        hrefs = re.findall(r"href\s*=\s*[\"']([^\"'<>]+)[\"']", page, flags=re.I)
    except Exception as exc:
        print(f"  [links] official site links unavailable: {type(exc).__name__}")
        return []

    found: dict[str, str] = {}
    special_hosts = {
        "x.com": "X", "twitter.com": "X", "github.com": "GitHub",
        "discord.gg": "Discord", "discord.com": "Discord",
        "t.me": "Telegram", "telegram.me": "Telegram",
        "linkedin.com": "LinkedIn", "youtube.com": "YouTube", "youtu.be": "YouTube",
        "warpcast.com": "Farcaster", "farcaster.xyz": "Farcaster",
    }
    for raw in hrefs:
        href = urllib.parse.unquote(raw.replace("&amp;", "&").strip())
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urllib.parse.urljoin(response.url, href)
        parsed = urllib.parse.urlparse(full)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        link_host = parsed.hostname.lower().removeprefix("www.")
        label = next((name for domain, name in special_hosts.items() if link_host == domain or link_host.endswith("." + domain)), "")
        path = parsed.path.lower()
        if label == "X" and (path.startswith(("/intent", "/share", "/search", "/i/")) or path in {"/", "/home"}):
            continue
        if label == "GitHub" and path.lower() in {"/", "/features", "/pricing", "/login", "/signup", "/marketplace", "/topics", "/search"}:
            continue
        if not label:
            same_site = link_host == host or link_host.endswith("." + host)
            if same_site and (link_host.startswith("docs.") or re.search(r"/(docs|documentation|developers|developer)(/|$)", path)):
                label = "Docs"
        if label and label not in found:
            found[label] = full.split("#", 1)[0]
        if len(found) >= 8:
            break
    preferred = ["X", "GitHub", "Discord", "Telegram", "LinkedIn", "Farcaster", "YouTube", "Docs"]
    return [f"{label}: {found[label]}" for label in preferred if label in found]


def norm_company(value: Any) -> str:
    value = str(value or "").lower()
    value = re.sub(r"\b(incorporated|inc|llc|ltd|limited|corp|corporation|co|company|labs|technologies|technology)\b", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def parse_event_date(item: dict, fallback: str | None = None) -> str:
    pp = item.get("published_parsed")
    if pp:
        try:
            return time.strftime("%Y-%m-%d", pp)
        except Exception:
            pass
    published = item.get("published", "")
    if published:
        try:
            raw_date = str(published).strip()
            if raw_date.isdigit() and len(raw_date) in (10, 13):
                epoch = int(raw_date) / (1000 if len(raw_date) == 13 else 1)
                return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
            iso = str(published).replace("Z", "+00:00")
            parsed_iso = datetime.fromisoformat(iso)
            return parsed_iso.strftime("%Y-%m-%d")
        except Exception:
            pass
        try:
            parsed = feedparser._parse_date(published)
            if parsed:
                return time.strftime("%Y-%m-%d", parsed)
        except Exception:
            pass
    return fallback or "Unknown"


def published_timestamp(item: dict) -> float:
    value = str(item.get("published", "") or "").strip()
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def google_news_rss_url(query: str, after: str | None = None, before: str | None = None) -> str:
    parts = [query]
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    q = urllib.parse.quote(" ".join(parts))
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_rss_feed(url: str, limit: int = 12, label: str = "") -> list[dict]:
    results: list[dict] = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit]:
            link = entry.get("link", "")
            if not link:
                continue
            results.append({
                "title": strip_html(entry.get("title", "")),
                "body": strip_html(entry.get("summary", entry.get("description", "")))[:2400],
                "link": link,
                "published": entry.get("published", entry.get("updated", "")),
                "published_parsed": entry.get("published_parsed", entry.get("updated_parsed")),
                "publisher": label or feed.feed.get("title", "RSS"),
                "source_type": "rss",
            })
    except Exception as exc:
        print(f"[warn] RSS fetch failed for {url}: {exc}")
    return results


def fetch_google_news(query: str, after: str | None = None, before: str | None = None, limit: int = 12) -> list[dict]:
    url = google_news_rss_url(query, after, before)
    return fetch_rss_feed(url, limit=limit, label="Google News: " + query)

def _as_name_list(value: Any) -> list[str]:
    if isinstance(value, list):
        candidates = [str(x).strip() for x in value]
    elif isinstance(value, str):
        candidates = re.split(r"[;,\n|]+", value)
    else:
        candidates = []
    blocked = {"unknown", "n/a", "none", "not reported", "anonymous", "undisclosed"}
    result = []
    for item in candidates:
        item = re.sub(r"\s+", " ", item).strip(" .:-")
        if item and item.lower() not in blocked and item not in result:
            result.append(item)
    return result[:3]


def excluded_nft_or_meme(data: dict[str, Any]) -> bool:
    """Fail closed when a project is classified as NFT/meme-first."""
    if data.get("is_nft_or_meme_project") is True:
        return True
    labels = " ".join(str(data.get(k, "") or "") for k in (
        "project_category", "crypto_project_category", "project_type", "sector"
    )).lower()
    labels = re.sub(r"[_/]+", " ", labels)
    return bool(re.search(
        r"\b(nft[- ]only|nft project|nft collection|nft marketplace|pfp collection|collectibles project|"
        r"meme[- ]?coin|memecoin|meme token|meme project|nft collectibles|meme memecoin)\b", labels
    ))


def verify_crypto_launch_quality(client: Groq, item: dict[str, Any], candidate: dict[str, Any], event_date: str) -> tuple[bool, dict[str, Any], list[str]]:
    """Require sourced launch, public founder identity and prior shipped Web3 work."""
    project = str(candidate.get("project") or "").strip()
    category = str(candidate.get("project_category") or "").strip()
    if not project or excluded_nft_or_meme(candidate):
        return False, {"quality_gate_pass": False, "project_category": category or "NFT/meme/unknown",
                       "quality_gate_reason": "Rejected by NFT/meme or missing-project hard filter."}, []

    raw_names = _as_name_list(candidate.get("founders"))
    queries = [f'"{project}" founders team crypto web3', f'"{project}" founder co-founder blockchain protocol']
    for founder in raw_names[:2]:
        queries.extend([f'"{founder}" built web3 protocol founder', f'"{founder}" crypto founder previous project'])

    evidence: list[dict[str, str]] = [{
        "title": str(item.get("title", ""))[:350], "publisher": str(item.get("publisher", "Unknown")),
        "url": str(item.get("link", "")), "body": str(item.get("body", ""))[:1000],
    }]
    seen_urls = {e["url"] for e in evidence if e.get("url")}
    for query in queries:
        for result in fetch_google_news(query, limit=5):
            url = str(result.get("link", "")).strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            evidence.append({"title": str(result.get("title", ""))[:350],
                             "publisher": str(result.get("publisher", "Unknown")), "url": url,
                             "body": str(result.get("body", ""))[:1000]})
        time.sleep(0.15)

    prompt = r"""You are a conservative due-diligence reviewer for a crypto/Web3 launch radar. Return JSON only:
{
 "quality_gate_pass": false,
 "launch_verified": false,
 "project_category": "DeFi protocol|blockchain infrastructure|developer tools|wallet/security/custody|stablecoin/payments|gaming infrastructure|NFT/collectibles|meme/memecoin|other",
 "is_nft_or_meme_project": false,
 "founders_publicly_identified": false,
 "founders": ["publicly named founders only"],
 "prior_web3_builder_track_record": false,
 "previous_web3_projects": ["project — role / shipped product"],
 "founder_evidence_url": "exact URL from evidence or empty",
 "track_record_evidence_url": "exact URL from evidence or empty",
 "launch_evidence_url": "exact URL from evidence or empty",
 "confidence": "High|Medium|Low",
 "quality_gate_reason": "brief factual reason"
}
PASS ONLY if all are evidenced: (1) the original source explicitly announces a substantive new product/protocol/mainnet/testnet/developer infrastructure/wallet/security/DeFi/payments launch; (2) the project is NOT primarily NFT/PFP/collectibles or a meme coin/token and is not a token-only launch without substantive product utility; (3) at least one founder is publicly identified by name in a supplied source; (4) at least one named founder previously built/shipped a real Web3 product/protocol or founded a Web3 company with a product. Merely working in crypto, advising, investing, marketing, influencing, running a community or making an unshipped announcement does not qualify as builder history. Do not infer identity from a pseudonymous X handle or use general model memory as evidence. Do not invent names, roles, projects or URLs. Evidence URLs must exactly match URLs supplied below. If evidence is insufficient or ambiguous, fail closed. Prefer credible publications, official team/about pages or prior-project sources.

INITIAL CANDIDATE:
""" + json.dumps(candidate, ensure_ascii=False) + r"""

LAUNCH SOURCE:
""" + json.dumps({"date": event_date, "title": item.get("title", ""), "publisher": item.get("publisher", ""), "url": item.get("link", ""), "body": item.get("body", "")[:1200]}, ensure_ascii=False) + r"""

PROJECT / FOUNDER SEARCH RESULTS:
""" + json.dumps(evidence, ensure_ascii=False) + "\n"

    verified = extract_json(client, prompt)
    if not isinstance(verified, dict):
        return False, {"quality_gate_pass": False, "quality_gate_reason": "No successful verification response."}, [e["url"] for e in evidence if e.get("url")]
    allowed_urls = {e["url"] for e in evidence if e.get("url")}
    founders = _as_name_list(verified.get("founders"))
    projects = _as_name_list(verified.get("previous_web3_projects"))
    founder_url = str(verified.get("founder_evidence_url") or "").strip()
    track_url = str(verified.get("track_record_evidence_url") or "").strip()
    launch_url = str(verified.get("launch_evidence_url") or "").strip()
    category_out = str(verified.get("project_category") or category).strip()
    exclusion_check = {"is_nft_or_meme_project": verified.get("is_nft_or_meme_project") is True,
                       "project_category": category_out}
    passed = all((verified.get("quality_gate_pass") is True, verified.get("launch_verified") is True,
                  verified.get("founders_publicly_identified") is True,
                  verified.get("prior_web3_builder_track_record") is True,
                  bool(founders), bool(projects), founder_url in allowed_urls,
                  track_url in allowed_urls, launch_url in allowed_urls,
                  category_out.lower() not in {"", "other", "unknown", "nft/collectibles", "meme/memecoin"},
                  not excluded_nft_or_meme(exclusion_check),
                  str(verified.get("confidence", "")).lower() in {"high", "medium"}))
    verified["founders"] = founders
    verified["previous_web3_projects"] = projects
    verified["project_category"] = category_out
    verified["founder_evidence_url"] = founder_url if founder_url in allowed_urls else ""
    verified["track_record_evidence_url"] = track_url if track_url in allowed_urls else ""
    verified["launch_evidence_url"] = launch_url if launch_url in allowed_urls else ""
    verified["quality_gate_pass"] = bool(passed)
    if not passed and not verified.get("quality_gate_reason"):
        verified["quality_gate_reason"] = "Failed a required evidence check: launch, non-NFT/non-meme category, public founder, previous shipped Web3 work, evidence URLs or confidence."
    urls = []
    for url in (founder_url, track_url, launch_url):
        if url and url in allowed_urls and url not in urls:
            urls.append(url)
    return bool(passed), verified, urls


def groq_client() -> Groq:
    return Groq(api_key=GROQ_KEY)


def extract_json(client: Groq, prompt: str, retries: int = 2) -> dict | None:
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_completion_tokens=1000,
                reasoning_effort="low",
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
        except Exception as exc:
            message = str(exc)
            if any(token in message.lower() for token in ("429", "rate limit", "ratelimit")):
                match = re.search(r"try again in ([\d.]+)s", message, re.I)
                wait = min(float(match.group(1)) + 1, 90) if match else min(20 * (attempt + 1), 90)
                print(f"  [retry] rate limited; waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if any(code in message for code in ("500", "502", "503", "504")):
                time.sleep(10 * (attempt + 1))
                continue
            print(f"[warn] model extraction failed: {message[:250]}")
            return None
    return None


def webhook_get(stream: str, action: str) -> set[str]:
    try:
        response = requests.get(
            WEBHOOK_URL,
            params={"token": WEBHOOK_TOKEN, "stream": stream, "action": action},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        key = {"existing_sources": "sources", "existing_keys": "keys", "existing_companies": "companies"}[action]
        return set(str(x) for x in data.get(key, []) if x)
    except Exception as exc:
        print(f"[warn] failed to fetch {action} for {stream}: {exc}")
        return set()


def post_rows(stream: str, rows: list[list[Any]], processed_sources: list[str] | None = None) -> dict | None:
    if not rows and not processed_sources:
        return {"ok": True, "appended": 0, "skipped": 0}
    try:
        response = requests.post(
            WEBHOOK_URL,
            json={"token": WEBHOOK_TOKEN, "stream": stream, "rows": rows,
                  "processedSources": processed_sources or []},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[error] webhook POST failed for {stream}: {exc}")
        return None


def event_key(company: str, date: str, event_type: str, stage: str = "", amount: Any = "") -> str:
    base = "|".join((norm_company(company), date or "", (event_type or "").lower(), (stage or "").lower(), str(amount or "")))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]



def account_freshness(created_at: str | None, now: datetime | None = None) -> str:
    """Account creation-age bucket; this is not a claim about first/last activity."""
    if not created_at:
        return "Unknown"
    try:
        value = str(created_at).replace("Z", "+00:00")
        created = datetime.fromisoformat(value)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        age_days = (current - created).days
        if age_days < 0:
            return "Unknown"
        if age_days <= 90:
            return "Created within 90 days"
        if age_days <= 180:
            return "Created 91-180 days ago"
        return "Older than 180 days"
    except Exception:
        return "Unknown"

def x_recent_search(query: str, max_results: int = 100) -> list[dict]:
    """Optional X API v2 recent search. Recent Search only covers the last 7 days."""
    if not X_BEARER_TOKEN:
        print("[info] X_BEARER_TOKEN not set; skipping direct X search")
        return []
    endpoint = "https://api.x.com/2/tweets/search/recent"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    params = {
        "query": query,
        "max_results": min(max(10, max_results), 100),
        "sort_order": "recency",
        "tweet.fields": "created_at,author_id,public_metrics,entities",
        "expansions": "author_id",
        "user.fields": "created_at,description,public_metrics,url,verified",
    }
    output: list[dict] = []
    next_token = None
    try:
        while len(output) < max_results:
            if next_token:
                params["next_token"] = next_token
            response = requests.get(endpoint, headers=headers, params=params, timeout=30)
            if response.status_code == 429:
                print("[warn] X recent-search rate limit reached")
                break
            response.raise_for_status()
            payload = response.json()
            users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
            for tweet in payload.get("data", []):
                user = users.get(tweet.get("author_id"), {})
                username = user.get("username", "")
                output.append({
                    "title": f"X launch signal from @{username}",
                    "body": tweet.get("text", ""),
                    "link": f"https://x.com/{username}/status/{tweet.get('id')}" if username else f"https://x.com/i/web/status/{tweet.get('id')}",
                    "published": tweet.get("created_at", ""),
                    "published_parsed": None,
                    "publisher": "X API v2",
                    "source_type": "x",
                    "x_user": user,
                    "x_tweet": tweet,
                })
            next_token = payload.get("meta", {}).get("next_token")
            if not next_token or not payload.get("data"):
                break
            time.sleep(0.25)
    except Exception as exc:
        print(f"[warn] X search failed: {exc}")
    return output[:max_results]


def defillama_raises() -> list[dict]:
    """Optional DeFiLlama Raises API integration; the endpoint requires a Pro API key."""
    if not DEFILLAMA_API_KEY:
        print("[info] DEFILLAMA_API_KEY not set; skipping DeFiLlama API (Pro endpoint)")
        return []
    url = f"https://pro-api.llama.fi/{DEFILLAMA_API_KEY}/api/raises"
    try:
        response = requests.get(url, timeout=40)
        response.raise_for_status()
        payload = response.json()
        raises = payload.get("raises", payload if isinstance(payload, list) else [])
        rows = []
        for item in raises:
            name = item.get("name") or item.get("project") or item.get("protocol") or ""
            if not name:
                continue
            project_slug = urllib.parse.quote(str(name), safe="")
            row_link = item.get("source") or f"https://defillama.com/raises?project={project_slug}"
            rows.append({
                "title": f"{name} funding round: {item.get('round', '')}",
                "body": json.dumps(item, ensure_ascii=False)[:2400],
                "link": row_link,
                "published": str(item.get("date", "")),
                "published_parsed": None,
                "publisher": "DeFiLlama Raises API",
                "source_type": "defillama",
                "defillama_data": item,
            })
        return rows
    except Exception as exc:
        print(f"[warn] DeFiLlama API fetch failed: {exc}")
        return []
