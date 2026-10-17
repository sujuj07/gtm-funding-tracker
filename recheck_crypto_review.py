"""Revisit unresolved Crypto Review Queue entries weekly without using RootData detail credits.

Candidates remain in the review queue until the strict evidence gate passes or a human
sets Manual Decision to Reject/Ignore. Successful candidates are promoted to the main
Crypto Launch Radar, and the review entry is marked promoted for auditability.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from backfill_crypto import _hard_excluded, build_review_row
from tracker_common import (
    account_freshness,
    event_key,
    fetch_official_social_links,
    groq_client,
    post_rows,
    verify_crypto_launch_quality,
    webhook_get_review_queue,
)

MAX_ITEMS = max(1, int(os.getenv("REVIEW_RECHECK_MAX_ITEMS", "5")))
MIN_AGE_DAYS = max(1, int(os.getenv("REVIEW_RECHECK_MIN_AGE_DAYS", "7")))
DELAY_SECONDS = max(0.5, float(os.getenv("MODEL_DELAY_SECONDS", "2.5")))


def _str(value) -> str:
    return "" if value is None else str(value).strip()


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in _str(value).split(";") if v.strip()]


def _parse_last_checked(value) -> datetime | None:
    raw = _str(value)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(datetime.now(timezone.utc).strftime(fmt))], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _item_and_candidate(row: list) -> tuple[dict, dict, str]:
    # Review Queue columns: A First Seen, B Project, C Signal, D verified launch date,
    # E Website, F offering, G niche, H chain, I names mentioned, J source URL,
    # K/L evidence URLs, M other evidence, N status, O reason, P category, Q confidence,
    # R last checked, S source type, T source title, U publisher, V key, W/X manual fields,
    # Y original source published date, Z source summary snippet.
    source_type = _str(row[18]) or "rss"
    source_date = _str(row[24])[:10]
    if source_type == "defillama_protocol":
        source_date = ""
    item = {
        "title": _str(row[19]) or f"Crypto launch evidence review: {_str(row[1])}",
        "publisher": _str(row[20]) or "Crypto Review Queue",
        "link": _str(row[9]),
        "published": source_date,
        "source_type": source_type,
        "body": _str(row[25])[:1800],
        "official_url": _str(row[4]),
        "chains": [_str(row[7])] if _str(row[7]) else [],
    }
    candidate = {
        "qualifies": True,
        "project": _str(row[1]),
        "signal_type": _str(row[2]) or "Protocol launch",
        "launch_date": _str(row[3]) or None,
        "website": _str(row[4]),
        "broad_offerings": _str(row[5]),
        "niche_presence": _str(row[6]),
        "chain_ecosystem": _str(row[7]),
        "founders": _as_list(row[8]),
        "project_category": _str(row[15]) or "other",
        "confidence": _str(row[16]) or "Low",
    }
    return item, candidate, source_date or "Unknown"


def _qualified_main_row(item: dict, candidate: dict, vet: dict, evidence_urls: list[str], source_date: str) -> list:
    project = _str(candidate.get("project"))
    signal = _str(candidate.get("signal_type")) or "Protocol launch"
    verified_launch_date = _str(vet.get("verified_launch_date"))
    if item.get("source_type") == "defillama_protocol" and verified_launch_date == _str(item.get("listed_at")):
        verified_launch_date = ""
    key_date = verified_launch_date or (source_date if item.get("source_type") != "defillama_protocol" else "Unknown")
    key = event_key(project, key_date, signal)
    website = _str(candidate.get("website") or item.get("official_url"))
    official_links = fetch_official_social_links(website)
    founders = _as_list(vet.get("founders"))
    previous = _as_list(vet.get("previous_web3_projects"))
    launch_source = _str(vet.get("launch_evidence_url") or item.get("link"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        "" if item.get("source_type") == "defillama_protocol" or source_date == "Unknown" else source_date,
        project, signal, verified_launch_date, "", "", "", website,
        _str(candidate.get("broad_offerings")), _str(candidate.get("niche_presence")),
        _str(candidate.get("chain_ecosystem")), "; ".join(founders), launch_source, "",
        "Qualified: public founder + prior shipped Web3 work; NFT/meme excluded",
        _str(vet.get("confidence") or "Medium"), now, key, "Unknown",
        "\n".join(dict.fromkeys([u for u in evidence_urls if u])), True, True, "; ".join(previous),
        _str(vet.get("founder_evidence_url")), _str(vet.get("track_record_evidence_url")),
        _str(vet.get("project_category") or candidate.get("project_category") or "Other"),
        _str(vet.get("quality_gate_reason") or "Passed all quality checks"), "; ".join(official_links),
    ]


def main() -> None:
    print(f"[review-recheck] starting; eligible if last checked >= {MIN_AGE_DAYS} days ago; max={MAX_ITEMS}; RootData detail API disabled")
    rows = webhook_get_review_queue()
    print(f"[review-recheck] unresolved queue rows returned={len(rows)}")
    now = datetime.now(timezone.utc)
    eligible = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 24:
            continue
        manual = _str(row[22]).lower()
        if manual in {"reject", "rejected", "ignore", "ignored"}:
            continue
        checked = _parse_last_checked(row[17])
        if checked and now - checked < timedelta(days=MIN_AGE_DAYS):
            continue
        if not _str(row[1]) or not _str(row[9]):
            continue
        eligible.append(row)
    eligible.sort(key=lambda r: _parse_last_checked(r[17]) or datetime(1970, 1, 1, tzinfo=timezone.utc))
    chosen = eligible[:MAX_ITEMS]
    print(f"[review-recheck] due={len(eligible)}; processing={len(chosen)}")
    if not chosen:
        print("[review-recheck] no due candidates; nothing to write")
        return

    client = groq_client()
    main_rows: list[list] = []
    updated_review_rows: list[list] = []
    for i, row in enumerate(chosen, 1):
        item, candidate, source_date = _item_and_candidate(row)
        print(f"[{i}/{len(chosen)}] Rechecking {candidate['project']} | status={_str(row[13])}")
        passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, candidate, source_date)
        if vet.get("quality_gate_reason") == "No successful verification response.":
            vet = dict(vet)
            vet["quality_gate_reason"] = "Retry required — verification service unavailable; the next scheduled review will retry."
            updated_review_rows.append(build_review_row(item, candidate, vet, evidence_urls, source_date, passed=False))
            print("  [review-recheck] verification unavailable; retaining unresolved review item")
        elif _hard_excluded(candidate, vet):
            update = build_review_row(item, candidate, vet, evidence_urls, source_date, passed=False)
            update[13] = "Excluded — NFT/meme-first"
            update[14] = _str(vet.get("quality_gate_reason") or "Explicit NFT/meme-first category exclusion.")
            updated_review_rows.append(update)
            print("  [review-recheck] explicit NFT/meme-first project; excluded from both main sheet and unresolved queue")
        elif passed:
            main_rows.append(_qualified_main_row(item, candidate, vet, evidence_urls, source_date))
            updated_review_rows.append(build_review_row(item, candidate, vet, evidence_urls, source_date, passed=True))
            print(f"  [review-recheck] QUALIFIED {candidate['project']}; promoting to main sheet")
        else:
            updated_review_rows.append(build_review_row(item, candidate, vet, evidence_urls, source_date, passed=False))
            print(f"  [review-recheck] still pending: {_str(vet.get('quality_gate_reason'))[:240]}")
        time.sleep(DELAY_SECONDS)

    result = None
    for attempt in range(1, 4):
        result = post_rows(
            "crypto", main_rows, processed_sources=[], allow_reprocessed_sources=True,
            review_rows=updated_review_rows,
        )
        if result and result.get("ok"):
            break
        print(f"[review-recheck] Apps Script write not confirmed (attempt {attempt}/3)")
        if attempt < 3:
            time.sleep(2 ** attempt)
    if not result or not result.get("ok"):
        raise RuntimeError("Review-queue updates were not confirmed by Apps Script; items remain eligible for a later retry.")
    print(
        f"[review-recheck] write confirmed: main_appended={result.get('appended', 0)} "
        f"main_updated={result.get('updated', 0)} review_added={result.get('reviewAppended', 0)} "
        f"review_updated={result.get('reviewUpdated', 0)}"
    )


if __name__ == "__main__":
    main()
