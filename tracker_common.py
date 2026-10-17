"""Shared helpers for the GTM / startup-intelligence trackers."""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
from email.utils import parsedate_to_datetime
import os
import re
import time
import urllib.parse
from html.parser import HTMLParser
from datetime import datetime, timezone
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


def _verification_queries(project: str, candidate: dict[str, Any], item: dict[str, Any], website_host: str = "") -> list[str]:
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:140]
    queries = [
        f'"{project}" founders team Web3',
        f'"{project}" founder previous project crypto protocol',
        f'"{project}" founder mainnet launch team',
    ]
    # Short or ambiguous names such as "Arc" need the launch headline context to
    # avoid searching an unrelated product with the same name.
    if title:
        queries.append(f'"{project}" {title} founder team blockchain')
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
    project = str(candidate.get("project") or "").strip()
    category = str(candidate.get("project_category") or "").strip()
    if not project or excluded_nft_or_meme(candidate):
        return False, {"quality_gate_pass": False, "project_category": category or "NFT/meme/unknown",
                       "quality_gate_reason": "Rejected by NFT/meme or missing-project hard filter."}, []

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
            "body": str(record.get("body") or "")[:1400],
        })

    # Fetch the article behind the RSS result itself: feeds often truncate the
    # founder and prior-product context needed for a strict verification pass.
    add_evidence({"title": item.get("title", ""), "publisher": item.get("publisher", "Unknown"),
                  "url": item.get("link", ""), "body": item.get("body", "")[:1200]})
    article_page = fetch_research_page(str(item.get("link") or ""), source_label=str(item.get("publisher") or "Launch source"), max_chars=2200)
    add_evidence(article_page)

    website = str(candidate.get("website") or "").strip()
    if website and not re.match(r"^https?://", website, re.I):
        website = "https://" + website
    website_page = fetch_research_page(website, source_label="Official project website", max_chars=2100) if website else None
    add_evidence(website_page)

    # Follow only public, same-site people/about/founder links found on the real homepage.
    website_host = ""
    if website_page:
        parsed_website = urllib.parse.urlparse(str(website_page.get("url") or website))
        website_host = (parsed_website.hostname or "").lower().removeprefix("www.")
        selected_links = []
        for url, label in website_page.get("links", []):
            full = urllib.parse.urlparse(url)
            host = (full.hostname or "").lower().removeprefix("www.")
            searchable = f"{full.path} {label}".lower()
            if host != website_host:
                continue
            if re.search(r"\b(about|team|founder|leadership|company|people|our-story|who-we-are|contributors|core-team)\b", searchable):
                if url not in [x[0] for x in selected_links]:
                    selected_links.append((url, label or full.path))
            if len(selected_links) >= 3:
                break
        for url, label in selected_links:
            add_evidence(fetch_research_page(url, source_label=f"Official website: {label}", max_chars=1800))

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
 "confidence": "High|Medium|Low",
 "quality_gate_reason": "brief factual reason"
}
PASS ONLY if all are evidenced: (1) the source explicitly announces a substantive new product/protocol/mainnet/testnet/developer infrastructure/wallet/security/DeFi/payments launch; (2) the project is NOT primarily NFT/PFP/collectibles or a meme coin/token and is not a token-only launch without substantive product utility; (3) at least one project founder or explicitly identified founding team member is publicly named in supplied source material; (4) at least one named founder previously built/shipped a real Web3 product/protocol or founded a Web3 company with a product. Merely working in crypto, advising, investing, marketing, influencing, running a community or making an unshipped announcement does not qualify. Do not infer identity from a pseudonymous X handle. Do not use model memory as evidence. Do not invent names, roles, projects or URLs. Every evidence URL must exactly match a URL in the supplied source list. If any criterion lacks evidence, fail closed and explain which evidence is missing. Prefer credible media, official team/about pages, project documentation, GitHub organizations or verifiable prior-project sources.

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
    """Read dedupe state from Apps Script, failing closed on errors.

    Returning an empty set on an auth/timeout error made a historical scan look like
    a fresh empty period and could advance the cursor incorrectly. Backfills must
    not advance unless the remote processed-source registry was read successfully.
    """
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
    key = {"existing_sources": "sources", "existing_keys": "keys", "existing_companies": "companies"}[action]
    values = data.get(key)
    if not isinstance(values, list):
        raise RuntimeError(f"Webhook response missing '{key}' for {action}/{stream}")
    return set(str(x) for x in values if x)


def post_rows(
    stream: str, rows: list[list[Any]], processed_sources: list[str] | None = None, *,
    allow_reprocessed_sources: bool = False,
) -> dict | None:
    if not rows and not processed_sources:
        return {"ok": True, "appended": 0, "skipped": 0}
    try:
        response = requests.post(
            WEBHOOK_URL,
            json={"token": WEBHOOK_TOKEN, "stream": stream, "rows": rows,
                  "processedSources": processed_sources or [],
                  "allowReprocessedSources": bool(allow_reprocessed_sources)},
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
