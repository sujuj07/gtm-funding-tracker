"""Score how worth reaching out to a company is, for a growth/BD/RevOps job hunt.

The target profile is specific: a company that has raised recently, is still
small enough not to have a commercial team, and therefore needs someone to build
revenue operations. Funding alone is a weak proxy for that. The number of open
roles on the company's own job board is a much better one, and an *open
growth/BD role* is not a proxy at all -- it is the company stating the need.

So the weights deliberately put verified board data above news-derived fields:

  open growth/BD role   +6   they are hiring this job, right now
  small board (1-30)    +3   thin team, so the role has real scope
  prime stage           +5   seed / Series A, the window before a GTM team exists
  raise $1-40M          +4   enough money to hire, too little to already have staff
  large raise           -4   probably already has a commercial org
  stage/amount mismatch -3   the data contradicts itself; do not trust the row

Scores are only as good as their inputs, so every component records a tag. A row
scoring 18 with `AMT?` in its signals is a different proposition from one scoring
18 on verified numbers, and the tags make that visible instead of burying it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# Stage -> points. The "prime hiring window" is the stretch where a company has
# money and customers but no commercial team yet.
STAGE_POINTS = {
    "Seed": 5,
    "Seed Extension": 5,
    "Series A": 5,
    "Bridge": 4,
    "Pre-seed": 3,
    "Series B": 2,
    "Debt": 1,
    "Unknown": 1,
    "Series C": 0,
    "Series D+": 0,
    "Growth": 0,
    "Strategic": 0,
    "Grant": 1,
}

AMOUNT_FLOOR = 1_000_000
AMOUNT_CEILING = 40_000_000

# A board this large means an established company with an existing GTM function.
BOARD_LARGE = 100
BOARD_SMALL = 30

TIER_BOUNDS = ((18, "A"), (13, "B"), (8, "C"))


def _days_since(value: str) -> int | None:
    """Days between an ISO date string and today, or None if unparseable."""
    text = (value or "").strip()[:10]
    if not text:
        return None
    try:
        when = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - when).days


def tier_for(score: int) -> str:
    for threshold, label in TIER_BOUNDS:
        if score >= threshold:
            return label
    return "D"


def score_row(
    *,
    stage: str = "",
    amount_usd: float | None = None,
    event_date: str = "",
    customer_type: str = "",
    hiring_flag: str = "",
    event_type: str = "",
    ats_platform: str = "",
    open_roles: int | None = None,
    target_roles: int | None = None,
) -> tuple[int, str, str]:
    """Score one company.

    Returns ``(score, tier, signals)``. ``signals`` is a compact tag string so
    the sheet shows *why* a row ranks where it does, e.g.
    ``GROWTH-ROLE-OPEN · SMALL-TEAM · PRIME-STAGE · $1-40M · B2B · FRESH``.

    ``open_roles``/``target_roles`` of None mean "not checked yet", which is
    scored differently from 0 ("checked, none open"). Treating those the same
    would quietly reward companies that have never been looked at.
    """
    score = 0
    tags: list[str] = []

    # An entry that is not a funding event is not a target for this purpose.
    if event_type and event_type not in ("Funding round", "Debt", ""):
        return 0, "D", "NOT-A-FUNDING-EVENT"

    # --- the strongest signal: a growth-shaped role is actually open ---
    if target_roles:
        score += 6
        tags.append(f"GROWTH-ROLE-OPEN x{target_roles}" if target_roles > 1 else "GROWTH-ROLE-OPEN")
        if target_roles > 1:
            score += 1
    elif target_roles == 0 and ats_platform:
        tags.append("no-growth-role-open")

    # --- team size, from the board rather than from prose ---
    if open_roles is not None:
        if 1 <= open_roles <= BOARD_SMALL:
            score += 3
            tags.append(f"SMALL-TEAM ({open_roles} roles)")
        elif open_roles > BOARD_LARGE:
            score -= 3
            tags.append(f"LARGE-ORG ({open_roles} roles)")
        elif open_roles > BOARD_SMALL:
            tags.append(f"{open_roles} roles open")
    if ats_platform:
        score += 1
        tags.append(ats_platform)

    # --- stage ---
    stage_points = STAGE_POINTS.get(stage, 1)
    score += stage_points
    if stage_points >= 5:
        tags.append("PRIME-STAGE")
    elif stage and stage != "Unknown":
        tags.append(stage)

    # --- raise size ---
    if amount_usd:
        if AMOUNT_FLOOR <= amount_usd <= AMOUNT_CEILING:
            score += 4
            tags.append("$1-40M")
        elif amount_usd < AMOUNT_FLOOR:
            score += 1
            tags.append("MICRO-RAISE")
        else:
            score -= 4
            tags.append("TOO-BIG")
            if stage in ("Pre-seed", "Seed", "Seed Extension", "Series A") and amount_usd > 100_000_000:
                score -= 3
                tags.append("MISMATCH!")
    else:
        tags.append("AMT?")

    # --- news-derived hiring hint: kept, but worth far less than the board ---
    if not ats_platform:
        if hiring_flag == "Yes":
            score += 2
            tags.append("hiring (news)")
        elif hiring_flag == "Maybe":
            score += 1
    elif hiring_flag == "No":
        # A layoff report outweighs an open board.
        score -= 3
        tags.append("LAYOFF-REPORTED")

    # --- fit and freshness ---
    if customer_type == "B2B":
        score += 2
        tags.append("B2B")

    age = _days_since(event_date)
    if age is not None:
        if age <= 60:
            score += 3
            tags.append("FRESH")
        elif age <= 120:
            score += 1
        elif age > 365:
            score -= 2
            tags.append("STALE")

    score = max(score, 0)
    return score, tier_for(score), " · ".join(tags)


def score_queue_row(row: dict, enrichment: dict | None = None) -> dict:
    """Score one enrichment-queue row and shape it for the webhook.

    Returns a dict ready to be posted in ``intelUpdates``. Human-entered outreach
    columns are intentionally absent so the webhook cannot overwrite them.
    """
    enrichment = enrichment or {}

    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    open_roles = as_int(enrichment.get("open_roles"))
    target_roles = as_int(enrichment.get("target_roles"))

    score, tier, signals = score_row(
        stage=row.get("stage") or "",
        amount_usd=row.get("amount") or None,
        event_date=row.get("date") or "",
        customer_type=row.get("customerType") or "",
        hiring_flag=row.get("hiringFlag") or "",
        event_type=row.get("eventType") or "",
        ats_platform=enrichment.get("platform") or "",
        open_roles=open_roles,
        target_roles=target_roles,
    )

    return {
        "key": row.get("key"),
        "targetScore": score,
        "tier": tier,
        "signals": signals,
        "atsPlatform": enrichment.get("platform", ""),
        "atsSlug": enrichment.get("slug", ""),
        "careersUrl": enrichment.get("board_url", ""),
        "openRoles": "" if open_roles is None else open_roles,
        "targetRoles": "" if target_roles is None else target_roles,
        "targetTitles": enrichment.get("target_titles", ""),
        "rolesChecked": enrichment.get("checked_at", ""),
    }
