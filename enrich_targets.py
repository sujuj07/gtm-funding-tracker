"""Verify each company's job board and rank it as an outreach target.

Runs separately from main.py and on a slower cadence, because it does something
different: main.py discovers companies from news, this qualifies them against
first-party data.

    sheet (enrichment_queue)  ->  company website
                              ->  find the real ATS board
                              ->  count open roles + growth/BD roles
                              ->  score and rank
                              ->  sheet (intelUpdates)

Only writes the appended intelligence columns (AF onward). Columns A:AE belong to
main.py and are never touched, and the human outreach columns are never
overwritten by this pass.

Budgeted deliberately: each company costs up to a handful of HTTP requests
against third-party sites, so ENRICH_MAX_COMPANIES caps a run and the queue is
served highest-score-first, so a capped run spends its budget on the best
targets rather than on alphabetical luck.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from ats_enrich import enrich_company
from scoring import score_queue_row
from tracker_common import post_rows, webhook_get_enrichment_queue

STREAM = "funding"
MAX_COMPANIES = int(os.getenv("ENRICH_MAX_COMPANIES", "60"))
# Re-check a board this many days after the last successful check.
RECHECK_AFTER_DAYS = int(os.getenv("ENRICH_RECHECK_DAYS", "14"))
SAVE_BATCH_SIZE = int(os.getenv("ENRICH_SAVE_BATCH_SIZE", "25"))


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"ATS enrichment run | UTC={started.isoformat()} | cap={MAX_COMPANIES}")

    queue = webhook_get_enrichment_queue(limit=MAX_COMPANIES, max_age_days=RECHECK_AFTER_DAYS)
    if not queue:
        print("Nothing to enrich: every row with a website has been checked recently.")
        return 0
    print(f"{len(queue)} company(ies) queued, highest-scoring first")

    updates: list[dict] = []
    stats = {"boards": 0, "with_growth_role": 0, "no_board": 0, "unreachable": 0}

    def flush() -> None:
        nonlocal updates
        if not updates:
            return
        result = post_rows(STREAM, [], intel_updates=updates)
        if result and result.get("ok"):
            print(f"  [save] intelApplied={result.get('intelApplied', 0)}")
            updates = []
        else:
            # Keep them pending; the next flush retries. Never drop results silently.
            print("  [warn] batch not confirmed; keeping updates pending for final retry")

    for index, row in enumerate(queue, 1):
        company = row.get("company", "?")
        website = row.get("website", "")
        enrichment = enrich_company(website)
        status = enrichment.get("status", "")

        if status == "no ATS board found":
            stats["no_board"] += 1
        elif status == "board found, API unreachable":
            stats["unreachable"] += 1
        elif enrichment.get("platform"):
            stats["boards"] += 1
            if enrichment.get("target_roles"):
                stats["with_growth_role"] += 1

        update = score_queue_row(row, enrichment)
        updates.append(update)

        detail = ""
        if enrichment.get("platform"):
            detail = (f" | {enrichment['platform']}/{enrichment['slug']}"
                      f" | {enrichment.get('open_roles', '?')} roles"
                      f" | {enrichment.get('target_roles', 0)} growth/BD")
        print(f"[{index}/{len(queue)}] {company[:38]:38s} score={update['targetScore']:>3} "
              f"tier={update['tier']}{detail}  ({status})")

        if len(updates) >= SAVE_BATCH_SIZE:
            flush()

    flush()
    if updates:
        print(f"[error] {len(updates)} update(s) could not be saved; they stay queued for the next run")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"Completed in {elapsed:.0f}s | boards found: {stats['boards']} | "
        f"with an open growth/BD role: {stats['with_growth_role']} | "
        f"no board: {stats['no_board']} | board unreachable: {stats['unreachable']}"
    )
    return 1 if updates else 0


if __name__ == "__main__":
    sys.exit(main())
