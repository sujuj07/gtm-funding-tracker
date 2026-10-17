"""Shared helpers for the GTM / startup-intelligence trackers."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import difflib
from email.utils import parsedate_to_datetime
import os
import re
import time
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from groq import Groq

# Quota-aware model routing. Use the smaller GPT-OSS model by default, then
# fall back to independent model quota pools when a model is rate-limited or unavailable.
MODEL = (os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b")
_DEFAULT_FALLBACKS = "qwen/qwen3.8-27b,openai/gpt-oss-120b"
MODEL_FALLBACKS = [
    value.strip() for value in os.getenv("GROQ_MODEL_FALLBACKS", _DEFAULT_FALLBACKS).split(",")
    if value.strip()
]
MODEL_CHAIN = tuple(dict.fromkeys([MODEL, *MODEL_FALLBACKS]))
MODEL_CHAIN_LABEL = " -> ".join(MODEL_CHAIN)
_MODEL_BLOCKED_UNTIL: dict[str, float] = {}
GROQ_KEY = os.environ["GROQ_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "").strip()
DEFILLAMA_API_KEY = os.getenv("DEFILLAMA_API_KEY", "").strip()
ROOTDATA_API_KEY = os.getenv("ROOTDATA_API_KEY", "").strip()
# RootData Basic plan: /open/ser_inv is documented as unlimited search; /open/get_item costs 2 credits.
# Keep a 200-credit account reserve, cap our own use at 800 credits/month and 24 credits/day.
ROOTDATA_MONTHLY_CREDIT_CAP = int(os.getenv("ROOTDATA_MONTHLY_CREDIT_CAP", "800"))
ROOTDATA_DAILY_CREDIT_CAP = int(os.getenv("ROOTDATA_DAILY_CREDIT_CAP", "24"))
ROOTDATA_RESERVE_CREDITS = int(os.getenv("ROOTDATA_RESERVE_CREDITS", "200"))
ROOTDATA_STATE_PATH = Path(os.getenv("ROOTDATA_STATE_PATH", ".backfill-state/rootdata-state.json"))
ROOTDATA_DETAIL_CREDIT_COST = 2


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



# High-signal launch detection used before spending model tokens. This is a
# conservative recall/precision balance: obvious generic crypto-news headlines
# do not reach LLM triage unless their headline/body contains a substantive
# product/network launch cue.
_LAUNCH_CUE_RE = re.compile(
    r"\b(?:launch(?:es|ed|ing)?|mainnet|testnet|goes live|went live|now live|is live|we are live|we\'re live|live now|"
    r"introduc(?:es|ed|ing|tion)|unveil(?:s|ed|ing)?|debut(?:s|ed)?|rolls? out|"
    r"release[sd]?|ships?|shipping|public beta|open beta|early access|available now|"
    r"goes public|opens to the public|network launch|protocol launch|product launch)\b",
    re.IGNORECASE,
)
_TECH_CONTEXT_RE = re.compile(
    r"\b(?:crypto|web3|web 3|blockchain|protocol|mainnet|testnet|defi|onchain|"
    r"layer[- ]?[123]|\bL[123]\b|wallet|smart contract|stablecoin|decentralized|"
    r"decentralised|rollup|validator|dapp|infrastructure|tokenization|tokenisation)\b",
    re.IGNORECASE,
)
_NOISE_ONLY_RE = re.compile(
    r"\b(?:funding round|raises? \$|raised \$|series [a-f]|seed round|token price|"
    r"price prediction|price surges?|ETF|moves? \$|transfers? \$|business officer|"
    r"appoints?|joins? as|acquires?|acquisition|hiring|job opening|validator group)\b",
    re.IGNORECASE,
)


def launch_signal_score(item: dict[str, Any]) -> int:
    """Return 0 for non-launch-like news, otherwise rank stronger launch evidence higher."""
    if str(item.get("source_type") or "") == "defillama_protocol":
        # A recent structured registry listing is a discovery lead; second-stage evidence
        # verification must still prove an actual launch before anything is written.
        return 2 if item.get("official_url") else 1
    title = strip_html(str(item.get("title") or ""))
    body = strip_html(str(item.get("body") or ""))[:1800]
    title_has_cue = bool(_LAUNCH_CUE_RE.search(title))
    context = f"{title} {body}"
    has_tech_context = bool(_TECH_CONTEXT_RE.search(context))

    # Stop obvious fundraising, hiring, price/transfer and executive news from
    # consuming triage calls unless its headline explicitly signals a launch.
    if _NOISE_ONLY_RE.search(title) and not title_has_cue:
        return 0
    if title_has_cue and has_tech_context:
        return 3
    if _LAUNCH_CUE_RE.search(body) and has_tech_context:
        return 2
    # A new protocol/network/chain/product announcement can be phrased without
    # the literal word "launch"; accept it at lower priority for model review.
    if re.search(r"\b(?:new|introducing|announcing)\s+(?:a\s+)?(?:protocol|network|blockchain|layer[- ]?[123]|L1|L2|wallet|DeFi platform|Web3 platform)\b", title, re.I) and has_tech_context:
        return 1
    return 0

def google_news_rss_url(query: str, after: str | None = None, before: str | None = None) -> str:
    parts = [query]
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    q = urllib.parse.quote(" ".join(parts))
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_rss_feed(
    url: str, limit: int = 12, label: str = "", *, strict: bool = False
) -> list[dict]:
    """Fetch and parse an RSS feed.

    In normal live monitoring, return [] on a feed error so one source doesn't stop
    the whole job. Historical backfills pass strict=True so transport/HTTP failures
    are not mistaken for an empty historical date range.
    """
    results: list[dict] = []
    try:
        response = requests.get(
            url, timeout=(5, 20), allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0)"},
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not getattr(feed, "entries", []):
            raise RuntimeError(f"RSS parse failed: {getattr(feed, 'bozo_exception', 'invalid feed')}")
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
        if strict:
            raise RuntimeError(f"RSS fetch failed for {label or url}: {type(exc).__name__}: {exc}") from exc
        print(f"[warn] RSS fetch failed for {url}: {exc}")
    return results


def fetch_google_news(
    query: str, after: str | None = None, before: str | None = None,
    limit: int = 12, *, strict: bool = False,
) -> list[dict]:
    url = google_news_rss_url(query, after, before)
    return fetch_rss_feed(url, limit=limit, label="Google News: " + query, strict=strict)

def _listing_date(value: Any) -> str:
    """Parse DeFiLlama listedAt timestamps to an ISO UTC date."""
    if value in (None, "", 0, "0"):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
            number = float(value)
            if number > 10_000_000_000:  # tolerate milliseconds
                number /= 1000.0
            return datetime.fromtimestamp(number, timezone.utc).strftime("%Y-%m-%d")
        normalized = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d") if parsed.tzinfo else parsed.strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch_defillama_protocol_leads(after: str, before: str) -> list[dict]:
    """Free, no-key DeFiLlama /protocols feed, filtered by its listedAt field.

    `listedAt` is a DeFiLlama listing timestamp, NOT a launch date. These records are
    discovery leads only; downstream verification must find an independent launch source.
    The paid /api/raises endpoint is deliberately not used here.
    """
    endpoint = "https://api.llama.fi/protocols"
    response = requests.get(
        endpoint, timeout=(5, 30),
        headers={"User-Agent": "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0)"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("DeFiLlama /protocols returned an unexpected schema")
    records: list[dict] = []
    seen: set[str] = set()
    for protocol in payload:
        if not isinstance(protocol, dict):
            continue
        listed_date = _listing_date(protocol.get("listedAt"))
        if not listed_date or not (after <= listed_date < before):
            continue
        name = str(protocol.get("name") or "").strip()
        if not name:
            continue
        raw_module = str(protocol.get("module") or protocol.get("slug") or name).strip()
        # DeFiLlama's public page slug commonly follows the module's first path segment.
        slug = raw_module.split("/", 1)[0].removesuffix(".js")
        slug = re.sub(r"[^A-Za-z0-9._-]", "", slug) or re.sub(r"[^A-Za-z0-9._-]", "-", name.lower())
        lead_url = f"https://defillama.com/protocol/{urllib.parse.quote(slug, safe='._-')}"
        if lead_url in seen:
            continue
        seen.add(lead_url)
        chains = protocol.get("chains") or []
        if not isinstance(chains, list):
            chains = [str(chains)] if chains else []
        official_url = str(protocol.get("url") or "").strip()
        twitter = str(protocol.get("twitter") or "").strip().lstrip("@")
        category = str(protocol.get("category") or "").strip()
        description = str(protocol.get("description") or "").strip()
        tvl = protocol.get("tvl")
        body = (
            "DISCOVERY-ONLY REGISTRY RECORD. The listedAt value means DeFiLlama listing date, "
            "not project launch date. Independent launch evidence is required. "
            f"Name: {name}. Category: {category}. Chains: {', '.join(map(str, chains))}. "
            f"Description: {description[:900]}. Official website: {official_url}. "
            f"Public X handle listed by DeFiLlama: @{twitter if twitter else 'not provided'}. "
            f"Current TVL field: {tvl}. DeFiLlama listedAt: {listed_date}."
        )
        records.append({
            "title": f"DeFiLlama protocol listing lead: {name} ({category or 'category not supplied'})",
            "body": body,
            "link": lead_url,
            "published": f"{listed_date}T00:00:00Z",
            "published_parsed": None,
            "publisher": "DeFiLlama public /protocols API",
            "source_type": "defillama_protocol",
            "official_url": official_url,
            "x_handle": twitter,
            "chains": list(map(str, chains)),
            "listed_at": listed_date,
        })
    records.sort(key=lambda item: str(item.get("listed_at") or ""), reverse=True)
    return records



# RootData Basic integration. Project/VC/person search is documented as unlimited
# (subject to 60 req/min); project details cost 2 credits/call. We only request
# project details with include_team/include_investors omitted, and we intentionally
# ignore funding and investor fields because the user's Basic plan does not include them.
def _rootdata_load_state() -> dict[str, Any]:
    try:
        raw = ROOTDATA_STATE_PATH.read_text(encoding="utf-8")
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _rootdata_save_state(state: dict[str, Any]) -> None:
    try:
        ROOTDATA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ROOTDATA_STATE_PATH.with_suffix(ROOTDATA_STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(ROOTDATA_STATE_PATH)
    except OSError as exc:
        # The API quota endpoint remains a hard safety gate even if the local ledger can't persist.
        print(f"  [rootdata] local cache/usage ledger could not be saved: {type(exc).__name__}")


def _rootdata_current_period(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    day = now.strftime("%Y-%m-%d")
    usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
    changed = False
    if usage.get("month") != month:
        usage = {"month": month, "month_credits_used": 0, "day": day, "day_credits_used": 0}
        changed = True
    elif usage.get("day") != day:
        usage["day"] = day
        usage["day_credits_used"] = 0
        changed = True
    usage.setdefault("month_credits_used", 0)
    usage.setdefault("day_credits_used", 0)
    state["usage"] = usage
    return usage, changed


def _rootdata_post(endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not ROOTDATA_API_KEY:
        raise RuntimeError("ROOTDATA_API_KEY is not configured")
    url = f"https://api.rootdata.com/open/{endpoint}"
    response = requests.post(
        url,
        headers={"apikey": ROOTDATA_API_KEY, "language": "en", "Content-Type": "application/json"},
        json=body or {},
        timeout=(4, 18),
    )
    if response.status_code == 429:
        raise RuntimeError(f"RootData {endpoint} rate limit (HTTP 429)")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"RootData {endpoint} returned an unexpected response")
    result_code = payload.get("result")
    if result_code not in (None, 200, "200"):
        raise RuntimeError(f"RootData {endpoint} API result={result_code}: {str(payload.get('message') or '')[:180]}")
    data = payload.get("data")
    return {"data": data, "message": payload.get("message", ""), "result": result_code}


def rootdata_remaining_credits() -> int | None:
    """Query RootData's documented free quota endpoint without consuming API credits."""
    if not ROOTDATA_API_KEY:
        return None
    try:
        result = _rootdata_post("quotacredits", {})
        data = result.get("data")
        if isinstance(data, dict) and data.get("credits") is not None:
            return max(0, int(data.get("credits")))
        print("  [rootdata] quota endpoint did not report a remaining-credit count; detail calls disabled")
    except Exception as exc:
        print(f"  [rootdata] quota check unavailable; paid detail calls disabled: {type(exc).__name__}: {exc}")
    return None


def _rootdata_name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _rootdata_match_project(query: str, results: Any) -> dict[str, Any] | None:
    if isinstance(results, dict):
        results = results.get("items") or results.get("list") or results.get("results") or [results]
    if not isinstance(results, list):
        return None
    qkey = _rootdata_name_key(query)
    if not qkey:
        return None
    candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        # RootData type: 1 Project, 2 VC, 3 Person. Never match an investor/person.
        try:
            if int(item.get("type")) != 1:
                continue
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or item.get("project_name") or "").strip()
        if not name:
            continue
        key = _rootdata_name_key(name)
        exact = key == qkey
        similarity = difflib.SequenceMatcher(a=qkey, b=key).ratio() if min(len(qkey), len(key)) >= 6 else 0.0
        # Fuzzy matching is only allowed for longer names; ambiguous names like Arc/OP are exact-only.
        if exact:
            score = 1.0
        elif min(len(qkey), len(key)) >= 8 and similarity >= 0.94:
            score = similarity
        else:
            continue
        candidates.append((score, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.035:
        return None
    return candidates[0][1]


def _rootdata_sanitized_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only Basic-friendly project metadata. Deliberately drop team, funding, and investor data."""
    social = raw.get("social_media")
    social_out: dict[str, str] = {}
    if isinstance(social, dict):
        for key in ("website", "twitter", "x", "discord", "linkedin", "medium", "github", "telegram"):
            value = social.get(key)
            if isinstance(value, str) and value.strip():
                social_out[key] = value.strip()[:500]
    elif isinstance(social, list):
        for item in social:
            if not isinstance(item, dict):
                continue
            key = str(item.get("type") or item.get("name") or item.get("platform") or "").lower()
            value = item.get("url") or item.get("link") or item.get("value")
            if value and key and any(term in key for term in ("website", "twitter", "x", "discord", "linkedin", "medium", "github", "telegram")):
                social_out[key] = str(value).strip()[:500]
    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    return {
        "project_id": raw.get("project_id"),
        "project_name": str(raw.get("project_name") or "")[:180],
        "one_liner": str(raw.get("one_liner") or "")[:500],
        "description": str(raw.get("description") or "")[:1800],
        "establishment_date": str(raw.get("establishment_date") or "")[:40],
        "project_type": str(raw.get("project_type") or "")[:80],
        "tags": [str(tag)[:80] for tag in tags[:20]],
        "rootdataurl": str(raw.get("rootdataurl") or "")[:500],
        "social_media": social_out,
    }


def rootdata_project_lookup(project_name: str) -> dict[str, Any] | None:
    """Find an exact RootData project; enrich from its Basic profile within strict credit caps.

    Search is documented as unlimited times. /get_item costs 2 credits. This function caps
    this install at 24 credits/day and 800 credits/month, keeps at least 200 credits on the
    actual account balance, and caches project details for reuse between workflow runs.
    The returned profile is metadata only: never launch/founder/prior-builder proof.
    """
    name = str(project_name or "").strip()
    if not ROOTDATA_API_KEY or not name:
        return None
    state = _rootdata_load_state()
    usage, period_changed = _rootdata_current_period(state)
    search_cache = state.get("search_cache") if isinstance(state.get("search_cache"), dict) else {}
    detail_cache = state.get("project_cache") if isinstance(state.get("project_cache"), dict) else {}
    cache_key = _rootdata_name_key(name)
    now_ts = time.time()
    cached_search = search_cache.get(cache_key)
    search_match = None
    if isinstance(cached_search, dict) and float(cached_search.get("expires_at", 0) or 0) > now_ts:
        search_match = cached_search.get("match") if isinstance(cached_search.get("match"), dict) else None
    else:
        try:
            response = _rootdata_post("ser_inv", {"query": name})
            search_match = _rootdata_match_project(name, response.get("data"))
            # Positive search cache: 30 days; negative: 24 hours so new listings can appear.
            search_cache[cache_key] = {"expires_at": now_ts + (30 * 86400 if search_match else 86400), "match": search_match}
            state["search_cache"] = search_cache
            if period_changed:
                state["usage"] = usage
            _rootdata_save_state(state)
        except Exception as exc:
            print(f"  [rootdata] project search unavailable for {name!r}: {type(exc).__name__}: {exc}")
            return None
    if not search_match:
        print(f"  [rootdata] no unambiguous RootData project match for {name!r}; no credits spent")
        return None
    try:
        project_id = int(search_match.get("id") or search_match.get("project_id"))
    except (TypeError, ValueError):
        return {"search": search_match, "detail": None, "metadata_only": True}

    cache_entry = detail_cache.get(str(project_id))
    detail = None
    if isinstance(cache_entry, dict) and isinstance(cache_entry.get("data"), dict):
        detail = cache_entry["data"]
        print(f"  [rootdata] cached Basic project details reused for {name!r}; 0 credits spent")
    else:
        local_month_used = int(usage.get("month_credits_used", 0) or 0)
        local_day_used = int(usage.get("day_credits_used", 0) or 0)
        if local_month_used + ROOTDATA_DETAIL_CREDIT_COST > ROOTDATA_MONTHLY_CREDIT_CAP:
            print(f"  [rootdata] local monthly cap reached ({local_month_used}/{ROOTDATA_MONTHLY_CREDIT_CAP} credits); using search summary only")
            return {"search": search_match, "detail": None, "metadata_only": True}
        if local_day_used + ROOTDATA_DETAIL_CREDIT_COST > ROOTDATA_DAILY_CREDIT_CAP:
            print(f"  [rootdata] local daily cap reached ({local_day_used}/{ROOTDATA_DAILY_CREDIT_CAP} credits); using search summary only")
            return {"search": search_match, "detail": None, "metadata_only": True}
        remaining = rootdata_remaining_credits()
        if remaining is None or remaining - ROOTDATA_DETAIL_CREDIT_COST < ROOTDATA_RESERVE_CREDITS:
            print(f"  [rootdata] actual balance unknown/near reserve ({remaining}); using search summary only")
            return {"search": search_match, "detail": None, "metadata_only": True}
        try:
            # Basic plan only: omit include_team/include_investors and strip out any restricted fields.
            response = _rootdata_post("get_item", {"project_id": project_id})
            raw_detail = response.get("data")
            if not isinstance(raw_detail, dict) or not raw_detail:
                print(f"  [rootdata] no detail profile returned for {name!r}; no confirmed credit debit recorded")
                return {"search": search_match, "detail": None, "metadata_only": True}
            detail = _rootdata_sanitized_detail(raw_detail)
            usage["month_credits_used"] = local_month_used + ROOTDATA_DETAIL_CREDIT_COST
            usage["day_credits_used"] = local_day_used + ROOTDATA_DETAIL_CREDIT_COST
            state["usage"] = usage
            detail_cache[str(project_id)] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": detail}
            # Keep state bounded for fast workflow commits.
            if len(detail_cache) > 450:
                oldest = sorted(detail_cache.items(), key=lambda pair: str(pair[1].get("fetched_at", "")) if isinstance(pair[1], dict) else "")
                for old_id, _ in oldest[:len(detail_cache) - 450]:
                    detail_cache.pop(old_id, None)
            state["project_cache"] = detail_cache
            # Bound query cache while preferring to retain positive names.
            if len(search_cache) > 900:
                ordered = sorted(search_cache.items(), key=lambda pair: (isinstance(pair[1], dict) and bool(pair[1].get("match")), float(pair[1].get("expires_at", 0) or 0)))
                for old_key, _ in ordered[:len(search_cache) - 900]:
                    search_cache.pop(old_key, None)
                state["search_cache"] = search_cache
            _rootdata_save_state(state)
            print(f"  [rootdata] details fetched for {name!r}; +{ROOTDATA_DETAIL_CREDIT_COST} credits; local month={usage['month_credits_used']}/{ROOTDATA_MONTHLY_CREDIT_CAP}, day={usage['day_credits_used']}/{ROOTDATA_DAILY_CREDIT_CAP}, remaining account credits={remaining - ROOTDATA_DETAIL_CREDIT_COST}")
        except Exception as exc:
            print(f"  [rootdata] detail fetch unavailable for {name!r}: {type(exc).__name__}: {exc}")
            return {"search": search_match, "detail": None, "metadata_only": True}
    return {"search": search_match, "detail": detail, "metadata_only": not bool(detail)}


def rootdata_metadata_for_candidate(project_name: str) -> dict[str, Any] | None:
    """Return concise RootData Basic metadata for verification context, if available."""
    record = rootdata_project_lookup(project_name)
    if not record:
        return None
    search = record.get("search") if isinstance(record.get("search"), dict) else {}
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    social = detail.get("social_media") if isinstance(detail.get("social_media"), dict) else {}
    return {
        "name": str(detail.get("project_name") or search.get("name") or project_name)[:160],
        "introduction": str(detail.get("one_liner") or search.get("introduce") or "")[:500],
        "description": str(detail.get("description") or "")[:900],
        "establishment_date": str(detail.get("establishment_date") or "")[:40],
        "project_type": str(detail.get("project_type") or "")[:50],
        "tags": detail.get("tags") if isinstance(detail.get("tags"), list) else [],
        "rootdata_url": str(detail.get("rootdataurl") or search.get("rootdataurl") or "")[:500],
        "social_media": social,
        "metadata_only": True,
    }

def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def fetch_x_public_timeline(username: str, max_posts: int = 20) -> list[dict]:
    """Fetch recent posts from a known public X profile through the public syndication page.

    This is an unofficial, best-effort profile timeline endpoint: no API key, no keyword
    search, and no full archive. It is used only to enrich a known project profile found
    by a separate source, never as the sole proof of a project or founder.
    """
    handle = str(username or "").strip().lstrip("@").strip("/")
    if "x.com/" in handle or "twitter.com/" in handle:
        parsed = urllib.parse.urlparse(handle if handle.startswith("http") else "https://" + handle)
        parts = [part for part in parsed.path.split("/") if part]
        handle = parts[0] if parts else ""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return []
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{urllib.parse.quote(handle)}"
    try:
        response = requests.get(
            url, timeout=(4, 12), allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0)"},
        )
        if response.status_code in {403, 404, 429}:
            print(f"  [x-public] timeline unavailable for @{handle}: HTTP {response.status_code}")
            return []
        response.raise_for_status()
        match = re.search(r"""<script[^>]+id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""", response.text, re.I | re.S)
        if not match:
            print(f"  [x-public] no timeline JSON returned for @{handle}")
            return []
        data = json.loads(match.group(1))
    except Exception as exc:
        print(f"  [x-public] timeline unavailable for @{handle}: {type(exc).__name__}")
        return []
    output: list[dict] = []
    seen_ids: set[str] = set()
    for node in _walk_dicts(data):
        legacy = node.get("legacy")
        if not isinstance(legacy, dict):
            continue
        text = str(legacy.get("full_text") or legacy.get("text") or "").strip()
        post_id = str(legacy.get("id_str") or node.get("rest_id") or "").strip()
        if not text or not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        core = node.get("core") if isinstance(node.get("core"), dict) else {}
        user_result = core.get("user_results", {}).get("result", {}) if isinstance(core.get("user_results"), dict) else {}
        user_legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}
        screen_name = str(user_legacy.get("screen_name") or handle).strip()
        created_at = str(legacy.get("created_at") or "")
        output.append({
            "title": f"Public X post from @{screen_name}",
            "body": text[:2400],
            "link": f"https://x.com/{screen_name}/status/{post_id}",
            "published": created_at,
            "published_parsed": None,
            "publisher": "X public profile timeline (unofficial syndication)",
            "source_type": "x_syndication",
            "x_user": {"username": screen_name, "name": str(user_legacy.get("name") or ""),
                       "created_at": str(user_legacy.get("created_at") or ""),
                       "description": str(user_legacy.get("description") or ""),
                       "url": f"https://x.com/{screen_name}"},
        })
        if len(output) >= max_posts:
            break
    output.sort(key=published_timestamp, reverse=True)
    return output[:max_posts]

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


class _ResearchPageParser(HTMLParser):
    """Small stdlib-only parser for evidence snippets and navigational links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._skip_depth = 0
        self._anchor_href = ""
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg", "iframe"}:
            self._skip_depth += 1
            return
        if low == "title":
            self._in_title = True
        if low == "a":
            self._anchor_href = str(attrs.get("href") or "")
            self._anchor_parts = []
        if low in {"br", "p", "div", "li", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag):
        low = tag.lower()
        if low in {"script", "style", "noscript", "svg", "iframe"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if low == "title":
            self._in_title = False
        if low == "a" and self._anchor_href:
            label = " ".join(self._anchor_parts).strip()
            self.links.append((self._anchor_href, label))
            self._anchor_href = ""
            self._anchor_parts = []
        if low in {"br", "p", "div", "li", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_data(self, data):
        if self._skip_depth:
            return
        value = re.sub(r"\s+", " ", data or " ").strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        self.text_parts.append(value)
        if self._anchor_href:
            self._anchor_parts.append(value)


def _safe_public_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().strip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def fetch_research_page(url: str, *, source_label: str = "Web page", max_chars: int = 1800) -> dict[str, Any] | None:
    """Fetch a public HTML page for evidence; never trusts model text as a fact without a URL."""
    if not _safe_public_http_url(url):
        return None
    try:
        response = requests.get(
            url, timeout=(4, 8), allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StartupIntelligenceTracker/1.0)"},
        )
        response.raise_for_status()
        if not _safe_public_http_url(response.url):
            return None
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if content_type and not any(x in content_type for x in ("text/html", "application/xhtml+xml", "text/plain")):
            return None
        parser = _ResearchPageParser()
        # Keep extraction bounded to avoid large downloads/token explosions.
        parser.feed(response.text[:900_000])
        title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:300]
        body = re.sub(r"\s+", " ", html.unescape(" ".join(parser.text_parts))).strip()[:max_chars]
        links = [(urllib.parse.urljoin(response.url, href), label[:160]) for href, label in parser.links if href]
        if not title and not body:
            return None
        return {"title": title or source_label, "publisher": source_label, "url": response.url,
                "body": body, "links": links}
    except Exception as exc:
        print(f"  [verify] page fetch unavailable ({source_label}): {type(exc).__name__}")
        return None


def fetch_github_link_evidence(url: str) -> dict[str, Any] | None:
    """Fetch public metadata for a GitHub profile/repository already linked by the project site.

    This is an evidence-enrichment helper, not independent proof that a person founded a
    project. Association must come from an official project/team page; repository metadata
    can only support prior product/build history when that association is also evidenced.
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        if (parsed.hostname or "").lower().removeprefix("www.") != "github.com":
            return None
        parts = [p for p in parsed.path.split("/") if p]
        reserved = {"features", "topics", "collections", "marketplace", "login", "signup", "settings", "orgs", "sponsors", "enterprise"}
        if not parts or parts[0].lower() in reserved or parts[0].startswith("?"):
            return None
        if len(parts) >= 2 and parts[1].lower() not in {"followers", "following", "repositories", "projects", "packages", "stars"}:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            api_url = f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"
            response = requests.get(api_url, timeout=(4, 10), headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "StartupIntelligenceTracker/1.0",
            })
            if response.status_code in {403, 404, 429}:
                return None
            response.raise_for_status()
            data = response.json()
            body = (
                f"Public GitHub repository. Name: {data.get('full_name', '')}. "
                f"Description: {data.get('description') or ''}. Owner: {((data.get('owner') or {}).get('login') or '')}. "
                f"Owner type: {((data.get('owner') or {}).get('type') or '')}. "
                f"Created: {data.get('created_at') or ''}. Last pushed: {data.get('pushed_at') or ''}. "
                f"Primary language: {data.get('language') or ''}. Topics: {', '.join(data.get('topics') or [])}. "
                f"Homepage: {data.get('homepage') or ''}. Stars: {data.get('stargazers_count') or 0}."
            )
            return {"title": f"GitHub repository: {data.get('full_name') or owner + '/' + repo}",
                    "publisher": "GitHub public repository metadata", "url": data.get("html_url") or url,
                    "body": body, "links": []}
        login = parts[0]
        api_url = f"https://api.github.com/users/{urllib.parse.quote(login, safe='')}"
        response = requests.get(api_url, timeout=(4, 10), headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "StartupIntelligenceTracker/1.0",
        })
        if response.status_code in {403, 404, 429}:
            return None
        response.raise_for_status()
        data = response.json()
        body = (
            f"Public GitHub user profile. Username: {data.get('login') or login}. "
            f"Public name: {data.get('name') or ''}. Bio: {data.get('bio') or ''}. "
            f"Company: {data.get('company') or ''}. Blog/site: {data.get('blog') or ''}. "
            f"Created: {data.get('created_at') or ''}. Public repositories: {data.get('public_repos') or 0}. "
            f"Public profile URL: {data.get('html_url') or url}."
        )
        return {"title": f"GitHub profile: {data.get('name') or login}",
                "publisher": "GitHub public profile metadata", "url": data.get("html_url") or url,
                "body": body, "links": []}
    except Exception as exc:
        print(f"  [verify] GitHub linked-page enrichment unavailable: {type(exc).__name__}")
        return None


def _verification_queries(project: str, candidate: dict[str, Any], item: dict[str, Any], website_host: str = "") -> list[str]:
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:140]
    queries = [
        f'"{project}" founder team Web3',
        f'"{project}" founder previous project crypto protocol',
        f'"{project}" crypto protocol launch announcement',
        f'"{project}" mainnet OR testnet launch',
    ]
    # Context helps with short/ambiguous names like Arc, while the official-domain
    # query helps recover founders from the project's own About/Team pages.
    if title:
        queries.append(f'"{project}" {title} launch founder team blockchain')
    if website_host:
        queries.append(f'site:{website_host} founder OR team OR about OR leadership')
    for founder in _as_name_list(candidate.get("founders"))[:2]:
        queries.extend([f'"{founder}" built Web3 protocol', f'"{founder}" previous crypto project founder'])
    unique = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in unique:
            unique.append(query)
    return unique[:6]


def verify_crypto_launch_quality(client: Groq, item: dict[str, Any], candidate: dict[str, Any], event_date: str) -> tuple[bool, dict[str, Any], list[str]]:
    """Require evidence of a real launch, publicly named founder, and prior shipped Web3 work."""
    candidate = dict(candidate)
    # Use only metadata supplied by the structured lead itself as fallback; never guess handles.
    if not candidate.get("website") and item.get("official_url"):
        candidate["website"] = item.get("official_url")
    if not candidate.get("x_handle") and item.get("x_handle"):
        candidate["x_handle"] = item.get("x_handle")
    if not candidate.get("chain_ecosystem") and item.get("chains"):
        candidate["chain_ecosystem"] = ", ".join(map(str, item.get("chains", [])))
    project = str(candidate.get("project") or "").strip()
    category = str(candidate.get("project_category") or "").strip()
    if not project or excluded_nft_or_meme(candidate):
        # Apply the cheap hard exclusion before any RootData detail lookup so excluded categories
        # never consume project-detail credits.
        return False, {"quality_gate_pass": False, "project_category": category or "NFT/meme/unknown",
                       "quality_gate_reason": "Rejected by NFT/meme or missing-project hard filter."}, []
    # RootData Basic is used for metadata/official-link enrichment only. Its registry record
    # never counts as proof of a dated launch, public founder identity, or prior shipped work.
    if not ROOTDATA_API_KEY:
        print("  [rootdata] ROOTDATA_API_KEY not configured; skipping Basic metadata enrichment")
    rootdata_meta = rootdata_metadata_for_candidate(project) if ROOTDATA_API_KEY else None
    if rootdata_meta:
        rd_social = rootdata_meta.get("social_media") if isinstance(rootdata_meta.get("social_media"), dict) else {}
        if not candidate.get("website") and rd_social.get("website"):
            candidate["website"] = rd_social.get("website")
        if not candidate.get("twitter") and not candidate.get("x_handle"):
            x_link = rd_social.get("twitter") or rd_social.get("x")
            if x_link:
                candidate["twitter"] = x_link
        rd_tags = rootdata_meta.get("tags") if isinstance(rootdata_meta.get("tags"), list) else []
        if not candidate.get("niche_presence") and rd_tags:
            candidate["niche_presence"] = ", ".join(str(tag) for tag in rd_tags[:8])
        print(f"  [rootdata] matched Basic metadata for {project!r}; rootdata profile is context-only, not qualification evidence")

    raw_names = _as_name_list(candidate.get("founders"))
    evidence: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    def add_evidence(record: dict[str, Any] | None, fallback_url: str = "", fallback_title: str = ""):
        if record is None:
            return
        url = str(record.get("url") or fallback_url or "").strip()
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        evidence.append({
            "title": str(record.get("title") or fallback_title or "Web source")[:300],
            "publisher": str(record.get("publisher") or "Unknown")[:100],
            "url": url,
            "published_date": parse_event_date(record) if parse_event_date(record) != "Unknown" else "",
            "body": str(record.get("body") or "")[:1400],
        })

    # Fetch the article behind the RSS result itself: feeds often truncate the
    # founder and prior-product context needed for a strict verification pass.
    add_evidence({"title": item.get("title", ""), "publisher": item.get("publisher", "Unknown"),
                  "url": item.get("link", ""), "body": item.get("body", "")[:1200]})
    article_label = "DeFiLlama listing page" if item.get("source_type") == "defillama_protocol" else str(item.get("publisher") or "Launch source")
    article_page = fetch_research_page(str(item.get("link") or ""), source_label=article_label, max_chars=2200)
    add_evidence(article_page)

    website = str(candidate.get("website") or "").strip()
    if website and not re.match(r"^https?://", website, re.I):
        website = "https://" + website
    website_page = fetch_research_page(website, source_label="Official project website", max_chars=2100) if website else None
    add_evidence(website_page)

    # Follow public, same-site founder/team and launch/news/blog links from the real homepage.
    # Also probe a few conventional paths because many early-stage project sites have sparse nav
    # markup or JS-rendered menus that the lightweight HTML parser cannot see.
    website_host = ""
    website_pages: list[dict[str, Any]] = []
    if website_page:
        website_pages.append(website_page)
        parsed_website = urllib.parse.urlparse(str(website_page.get("url") or website))
        website_host = (parsed_website.hostname or "").lower().removeprefix("www.")
        team_links: list[tuple[str, str]] = []
        launch_links: list[tuple[str, str]] = []
        for link_url, label in website_page.get("links", []):
            full = urllib.parse.urlparse(link_url)
            host = (full.hostname or "").lower().removeprefix("www.")
            if host != website_host:
                continue
            searchable = f"{full.path} {label}".lower()
            pair = (link_url, label or full.path)
            if re.search(r"\b(about|team|founder|leadership|company|people|our-story|who-we-are|contributors|core-team)\b", searchable):
                if pair not in team_links:
                    team_links.append(pair)
            if re.search(r"\b(launch|blog|news|announc|mainnet|testnet|release|changelog|product)\b", searchable):
                if pair not in launch_links:
                    launch_links.append(pair)

        selected_team = team_links[:3]
        selected_launch = launch_links[:2]
        seen_page_urls = {str(website_page.get("url") or website)}
        # If no people links are exposed in navigation, try two conventional pages.
        if len(selected_team) < 2:
            for path in ("/about", "/team", "/about-us", "/founders"):
                url = urllib.parse.urljoin(website.rstrip("/") + "/", path.lstrip("/"))
                if url not in seen_page_urls and len(selected_team) < 2:
                    selected_team.append((url, path.strip("/")))
                    seen_page_urls.add(url)
        # Registry leads need a separate launch announcement. Look at a limited set of project
        # news/blog pages if homepage navigation doesn't expose one.
        if item.get("source_type") == "defillama_protocol" and len(selected_launch) < 2:
            for path in ("/blog", "/news", "/announcements"):
                url = urllib.parse.urljoin(website.rstrip("/") + "/", path.lstrip("/"))
                if url not in seen_page_urls and len(selected_launch) < 2:
                    selected_launch.append((url, path.strip("/")))
                    seen_page_urls.add(url)

        for url, label in (selected_team + selected_launch):
            seen_page_urls.add(url)
            page = fetch_research_page(url, source_label=f"Official website: {label}", max_chars=1800)
            if page:
                website_pages.append(page)
                add_evidence(page)

        # If a blog/news index was found, inspect at most two same-site launch/article links.
        followed_launches = 0
        for page in website_pages[1:]:
            for link_url, label in page.get("links", []):
                full = urllib.parse.urlparse(link_url)
                host = (full.hostname or "").lower().removeprefix("www.")
                searchable = f"{full.path} {label}".lower()
                if host != website_host or link_url in seen_page_urls:
                    continue
                if re.search(r"\b(launch|mainnet|testnet|announcement|release|introducing|we-launched|our-launch)\b", searchable):
                    page2 = fetch_research_page(link_url, source_label=f"Official launch page: {label or full.path}", max_chars=1900)
                    seen_page_urls.add(link_url)
                    if page2:
                        website_pages.append(page2)
                        add_evidence(page2)
                        followed_launches += 1
                    if followed_launches >= 2:
                        break
            if followed_launches >= 2:
                break

        # Public GitHub profiles/repositories linked by the project's own pages can support
        # builder-history verification, but are never treated as founder proof by themselves.
        github_links = []
        for page in website_pages:
            for link_url, _label in page.get("links", []):
                parsed = urllib.parse.urlparse(link_url)
                if (parsed.hostname or "").lower().removeprefix("www.") == "github.com" and link_url not in github_links:
                    github_links.append(link_url)
        for github_url in github_links[:4]:
            add_evidence(fetch_github_link_evidence(github_url))

    # Free, unofficial profile timeline is an enrichment source only. Obtain handles from
    # the actual protocol record or links on the official website; never invent account names.
    handles: list[str] = []
    item_x_user = item.get("x_user") if isinstance(item.get("x_user"), dict) else {}
    for raw_handle in (item.get("x_handle"), item_x_user.get("username"), candidate.get("x_handle"), candidate.get("twitter")):
        raw = str(raw_handle or "").strip()
        if raw:
            if "x.com/" in raw or "twitter.com/" in raw:
                parsed = urllib.parse.urlparse(raw if raw.startswith("http") else "https://" + raw)
                parts = [part for part in parsed.path.split("/") if part]
                raw = parts[0] if parts else ""
            raw = raw.lstrip("@").strip()
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", raw) and raw not in handles:
                handles.append(raw)
    if website_page:
        for raw_url, _label in website_page.get("links", []):
            parsed = urllib.parse.urlparse(raw_url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if host not in {"x.com", "twitter.com"}:
                continue
            parts = [part for part in parsed.path.split("/") if part]
            if parts and parts[0].lower() not in {"home", "search", "intent", "share", "i", "explore"}:
                handle = parts[0].lstrip("@").strip()
                if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) and handle not in handles:
                    handles.append(handle)
    for handle in handles[:2]:
        timeline = fetch_x_public_timeline(handle, max_posts=20)
        print(f"  [x-public] @{handle}: {len(timeline)} recent public posts returned (unofficial endpoint)")
        if timeline:
            profile = timeline[0].get("x_user") or {}
            item["_verified_x_user"] = {
                "username": profile.get("username") or handle,
                "name": profile.get("name") or "",
                "created_at": profile.get("created_at") or "",
                "description": profile.get("description") or "",
                "url": profile.get("url") or f"https://x.com/{handle}",
            }
        relevant = [post for post in timeline if launch_signal_score(post) > 0 or re.search(r"\b(founder|co-founder|built|building|previously|formerly|mainnet|testnet|protocol launch|we launched|our launch)\b", post.get("body", ""), re.I)]
        for post in relevant[:6]:
            add_evidence({"title": post.get("title", ""), "publisher": post.get("publisher", "X public timeline"),
                          "url": post.get("link", ""), "body": post.get("body", "")[:1400]})
    queries = _verification_queries(project, candidate, item, website_host)
    query_result_count = 0
    for query in queries:
        try:
            results = fetch_google_news(query, limit=4)
        except Exception as exc:
            print(f"  [verify] search unavailable for {query[:85]!r}: {type(exc).__name__}")
            results = []
        query_result_count += len(results)
        for result in results:
            add_evidence(result)
        print(f"  [verify] query returned {len(results)} result(s): {query[:100]}")
        time.sleep(0.12)

    evidence = evidence[:14]
    discovered_founders = raw_names[:]
    if not discovered_founders and evidence:
        founder_discovery_prompt = r"""Extract only explicitly named founders or co-founders of the specific project from the supplied source materials. Do not use prior model knowledge. Do not treat a CEO, investor, advisor, executive or parent-company founder as the new project's founder unless a source explicitly says they founded this specific project. Return JSON only:
{"founders": [{"name": "Full Name", "role": "Founder|Co-founder", "evidence_url": "exact URL from supplied sources"}]}
If no source explicitly connects a named person to founding this project, return {"founders": []}. Evidence URLs must match one of the supplied URLs exactly.

PROJECT: """ + json.dumps(project, ensure_ascii=False) + r"""
LAUNCH TITLE: """ + json.dumps(str(item.get("title") or ""), ensure_ascii=False) + r"""
SOURCES: """ + json.dumps(evidence[:10], ensure_ascii=False) + "\n"
        discovery = extract_json(client, founder_discovery_prompt)
        allowed_before_followup = {e["url"] for e in evidence if e.get("url")}
        discovered = []
        if isinstance(discovery, dict) and isinstance(discovery.get("founders"), list):
            for person in discovery["founders"][:3]:
                if not isinstance(person, dict):
                    continue
                name = str(person.get("name") or "").strip()
                role = str(person.get("role") or "").lower()
                ev_url = str(person.get("evidence_url") or "").strip()
                if name and re.search(r"founder|co.?founder", role) and ev_url in allowed_before_followup and name not in discovered:
                    discovered.append(name)
        if discovered:
            discovered_founders = discovered
            print(f"  [verify] extracted publicly named founder(s) from sources: {', '.join(discovered_founders)}; checking prior build history")
            followup_queries = []
            for founder in discovered_founders[:2]:
                followup_queries.extend([
                    f'"{founder}" built Web3 protocol product',
                    f'"{founder}" previous crypto project founder blockchain',
                ])
            for query in followup_queries:
                try:
                    results = fetch_google_news(query, limit=4)
                except Exception as exc:
                    print(f"  [verify] founder-history search unavailable: {type(exc).__name__}")
                    results = []
                query_result_count += len(results)
                for result in results:
                    add_evidence(result)
                print(f"  [verify] founder-history query returned {len(results)} result(s): {query[:100]}")
                time.sleep(0.12)
    evidence = evidence[:20]
    verification_candidate = dict(candidate)
    if rootdata_meta:
        verification_candidate["rootdata_metadata_context_only"] = rootdata_meta
    if discovered_founders:
        verification_candidate["founders"] = discovered_founders
    print(f"  [verify] evidence pool={len(evidence)} unique sources; web-search items={query_result_count}; official website={'yes' if website_page else 'no'}")
    for src in evidence[:10]:
        print(f"  [verify-source] {src['publisher']}: {src['title'][:95]} | {src['url'][:150]}")

    prompt = r"""You are a conservative due-diligence reviewer for a selective crypto/Web3 launch radar. Return JSON only:
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
 "verified_launch_date": "YYYY-MM-DD only if directly stated by launch evidence; otherwise empty",
 "confidence": "High|Medium|Low",
 "quality_gate_reason": "brief factual reason"
}
RootData metadata in the initial candidate is only a directory/metadata clue. It is NOT independent launch evidence, NOT proof of public founder identity, and NOT proof of prior shipped Web3 work. Do not count it as one of the evidence URLs.
PASS ONLY if all are evidenced: (1) a supplied source independent of any DeFiLlama or RootData registry explicitly announces a substantive new product/protocol/mainnet/testnet/developer infrastructure/wallet/security/DeFi/payments launch; (2) the project is NOT primarily NFT/PFP/collectibles or a meme coin/token and is not a token-only launch without substantive product utility; (3) at least one project founder or explicitly identified founding team member is publicly named in supplied source material; (4) at least one named founder previously built/shipped a real Web3 product/protocol or founded a Web3 company with a product. Merely working in crypto, advising, investing, marketing, influencing, running a community or making an unshipped announcement does not qualify. Do not infer identity from a pseudonymous X handle. Do not use model memory as evidence. Do not invent names, roles, projects or URLs. Every evidence URL must exactly match a URL in the supplied source list. `verified_launch_date` may only be returned when an independent launch source explicitly supports it; never use DeFiLlama listedAt as launch date. If any criterion lacks evidence, fail closed and explain which evidence is missing. Prefer credible media, official team/about pages, project documentation, GitHub organizations or verifiable prior-project sources.

INITIAL CANDIDATE:
""" + json.dumps(verification_candidate, ensure_ascii=False) + r"""

LAUNCH SOURCE:
""" + json.dumps({"date": event_date, "title": item.get("title", ""), "publisher": item.get("publisher", ""), "url": item.get("link", ""), "body": item.get("body", "")[:1200]}, ensure_ascii=False) + r"""

PROJECT / FOUNDER SEARCH RESULTS AND OFFICIAL WEB PAGES:
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
    verified_launch_date = str(verified.get("verified_launch_date") or "").strip()
    try:
        datetime.strptime(verified_launch_date, "%Y-%m-%d")
    except Exception:
        verified_launch_date = ""
    launch_evidence_record = next((entry for entry in evidence if entry.get("url") == launch_url), None)
    # Code-level evidence guard: the selected launch source must itself contain a launch cue
    # plus crypto/Web3 context. A homepage/team page or DeFiLlama registry listing is not launch
    # proof, even if the model incorrectly labels it as such.
    launch_source_has_cue = bool(launch_evidence_record and launch_signal_score(launch_evidence_record) > 0)
    if not launch_evidence_record or str(launch_evidence_record.get("publisher") or "").lower().startswith("defillama"):
        verified_launch_date = ""
    if item.get("source_type") == "defillama_protocol" and verified_launch_date == str(item.get("listed_at") or ""):
        verified_launch_date = ""
    exclusion_check = {"is_nft_or_meme_project": verified.get("is_nft_or_meme_project") is True,
                       "project_category": category_out}
    passed = all((verified.get("quality_gate_pass") is True, verified.get("launch_verified") is True,
                  verified.get("founders_publicly_identified") is True,
                  verified.get("prior_web3_builder_track_record") is True,
                  bool(founders), bool(projects), founder_url in allowed_urls,
                  track_url in allowed_urls, launch_url in allowed_urls, launch_source_has_cue,
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
    verified["verified_launch_date"] = verified_launch_date
    if not passed and not verified.get("quality_gate_reason"):
        if not launch_source_has_cue:
            verified["quality_gate_reason"] = "No independent launch source with an explicit launch cue and Web3 context; registry listings and generic homepages do not count."
        else:
            verified["quality_gate_reason"] = "Failed a required evidence check: launch, non-NFT/non-meme category, public founder, previous shipped Web3 work, evidence URLs or confidence."
    urls = []
    for url in (founder_url, track_url, launch_url):
        if url and url in allowed_urls and url not in urls:
            urls.append(url)
    return bool(passed), verified, urls


def groq_client() -> Groq:
    return Groq(api_key=GROQ_KEY)


def _duration_seconds(value: str) -> float | None:
    """Parse common Retry-After/reset values such as 12, 1m20s, or 0.8s."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    matches = re.findall(r"([\d.]+)\s*(h|m|s)", raw)
    if not matches:
        return None
    scales = {"h": 3600, "m": 60, "s": 1}
    return sum(float(number) * scales[unit] for number, unit in matches)


def _exception_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _block_model_for_exception(model: str, exc: Exception) -> float:
    """Mark a model unavailable for the remainder of this Python process."""
    now = time.monotonic()
    headers = {}
    response = getattr(exc, "response", None)
    try:
        headers = dict(getattr(response, "headers", {}) or {})
    except Exception:
        headers = {}
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    wait = _duration_seconds(normalized.get("retry-after", ""))
    reset = _duration_seconds(normalized.get("x-ratelimit-reset-requests", ""))
    token_reset = _duration_seconds(normalized.get("x-ratelimit-reset-tokens", ""))
    message = str(exc).lower()
    if wait is None:
        if "day" in message or "daily" in message or "requests per day" in message:
            wait = 3600  # This process skips the depleted model; subsequent runs will retry it.
        else:
            wait = max(reset or 0, token_reset or 0, 600)
    # Avoid accidentally blocking a model for a week if an unfamiliar reset format is returned.
    wait = min(max(wait, 30), 24 * 3600)
    _MODEL_BLOCKED_UNTIL[model] = now + wait
    return wait


def _is_rate_limited(exc: Exception) -> bool:
    return _exception_status(exc) == 429 or any(
        marker in str(exc).lower() for marker in ("rate limit", "ratelimit", "too many requests")
    )


def _is_transient(exc: Exception) -> bool:
    status = _exception_status(exc)
    return status in {500, 502, 503, 504, 408} or any(
        code in str(exc) for code in ("500", "502", "503", "504", "timeout", "temporarily unavailable")
    )


def _is_model_configuration_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "model not found", "model_not_found", "decommissioned", "not available",
        "unsupported model", "model is not supported", "unknown model",
        "does not support response_format", "unsupported parameter", "invalid model",
    ))


def extract_json(client: Groq, prompt: str, retries: int = 2) -> dict | None:
    """Request JSON with automatic model failover when quotas or model availability fail.

    The selected primary model is tried first. On 429/quota errors the router skips it
    for the rest of this process and tries the next model in GROQ_MODEL_FALLBACKS. This
    prevents a run with several candidates from repeatedly hammering an exhausted model.
    """
    now = time.monotonic()
    candidates = [model for model in MODEL_CHAIN if _MODEL_BLOCKED_UNTIL.get(model, 0) <= now]
    if not candidates:
        # Every configured pool was rate-limited earlier in this job. Let the next
        # scheduled run retry rather than spend time or requests retrying known failures.
        print("  [model-router] all configured models are cooling down; will retry on the next run")
        return None

    for model_index, model in enumerate(candidates):
        for attempt in range(retries + 1):
            try:
                request = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "max_completion_tokens": 1000,
                }
                # `reasoning_effort` is supported by GPT-OSS. Keep the Qwen request
                # portable; if Groq adds model-specific parameters, the fallback remains usable.
                if model.startswith("openai/gpt-oss-"):
                    request["reasoning_effort"] = "low"
                response = client.chat.completions.create(**request)
                content = response.choices[0].message.content or "{}"
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("Model returned JSON but it was not an object")
                route = "primary" if model == MODEL else "fallback"
                print(f"  [model-router] success model={model} route={route}")
                return result
            except Exception as exc:
                message = str(exc)
                if _is_rate_limited(exc):
                    wait = _block_model_for_exception(model, exc)
                    print(f"  [model-router] {model} rate-limited; switching model (cooldown ~{wait:.0f}s)")
                    break  # Try the next model immediately; do not sleep on a depleted pool.
                if _is_model_configuration_error(exc):
                    _MODEL_BLOCKED_UNTIL[model] = time.monotonic() + 3600
                    print(f"  [model-router] {model} unavailable/configuration-incompatible; trying the next model")
                    break
                if _is_transient(exc) and attempt < retries:
                    wait = min(5 * (attempt + 1), 15)
                    print(f"  [model-router] transient error for {model}; retry {attempt + 1}/{retries} in {wait}s")
                    time.sleep(wait)
                    continue
                # A malformed/non-object JSON response gets one retry on the same model;
                # other non-transient errors fall through to the next configured model.
                if attempt < retries and isinstance(exc, (json.JSONDecodeError, ValueError)):
                    print(f"  [model-router] invalid JSON from {model}; retrying")
                    continue
                print(f"  [model-router] {model} failed: {message[:220]}; trying next model if available")
                break
    print("[warn] all configured Groq models failed for this extraction")
    return None


def webhook_get(stream: str, action: str) -> set[str]:
    """Read dedupe state from Apps Script, failing closed on errors."""
    response = requests.get(
        WEBHOOK_URL,
        params={"token": WEBHOOK_TOKEN, "stream": stream, "action": action},
        timeout=30,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Webhook returned non-JSON for {action}/{stream}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Webhook GET {action}/{stream} failed: {data}")
    key = {"existing_sources": "sources", "existing_keys": "keys", "existing_companies": "companies"}.get(action)
    if not key:
        raise ValueError(f"Unsupported set-valued webhook action: {action}")
    values = data.get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"Webhook response missing '{key}' for {action}/{stream}")
    return set(str(x) for x in values if x)


def webhook_get_review_queue() -> list[list[Any]]:
    """Read the visible Crypto Review Queue; errors must not look like an empty queue."""
    response = requests.get(
        WEBHOOK_URL,
        params={"token": WEBHOOK_TOKEN, "stream": "crypto", "action": "review_queue"},
        timeout=45,
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Webhook returned non-JSON for review_queue/crypto") from exc
    if not data.get("ok") or not isinstance(data.get("rows"), list):
        raise RuntimeError(f"Webhook GET review_queue/crypto failed: {data}")
    return data["rows"]


def post_rows(
    stream: str, rows: list[list[Any]], processed_sources: list[str] | None = None, *,
    allow_reprocessed_sources: bool = False, review_rows: list[list[Any]] | None = None,
) -> dict | None:
    review_rows = review_rows or []
    if not rows and not processed_sources and not review_rows:
        return {"ok": True, "appended": 0, "updated": 0, "reviewAppended": 0, "reviewUpdated": 0, "skipped": 0}
    try:
        response = requests.post(
            WEBHOOK_URL,
            json={"token": WEBHOOK_TOKEN, "stream": stream, "rows": rows,
                  "reviewRows": review_rows,
                  "processedSources": processed_sources or [],
                  "allowReprocessedSources": bool(allow_reprocessed_sources)},
            timeout=60,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            print(f"[error] webhook POST returned non-JSON (HTTP {response.status_code}): {response.text[:500]!r}")
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[error] webhook POST rejected payload (HTTP {response.status_code}): {str(data)[:700]}")
            return None
        return data
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", "")
        print(f"[error] webhook POST failed for {stream} (HTTP {status or 'n/a'}): {exc}; response={str(body)[:500]!r}")
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
