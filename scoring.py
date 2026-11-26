"""Score how worth reaching out to a company is, for a growth/BD/RevOps job hunt.

Scored from the row itself at write time, so every row gets a score as it is
created. There is no second pass, no queue and no extra workflow: the inputs are
all fields the extraction already produces.

Target profile: raised recently, still small enough to have no commercial team,
and therefore likely to need someone to build revenue operations.

Honest limits on the inputs
---------------------------
`team_size` and `founded_year` are only ever populated when the *source article
states them*. That is uncommon -- most funding stories never mention headcount --
so expect these to be empty on the majority of rows. They are scored generously
when present and simply absent otherwise; a missing value is never guessed, and
never treated as "small".

Where a signal is absent, stage and raise size act as the fallback proxy. They
are weaker: a Seed round usually means a small team, but "usually" is doing real
work in that sentence. The `Why Fit` string names which signals actually fired,
so a score built on a stated headcount is distinguishable from one built on
stage alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Stage -> points. The "prime window" is after a company has money and customers
# but before it has a commercial team.
STAGE_POINTS = {
    "Seed": 5,
    "Seed Extension": 5,
    "Series A": 5,
    "Bridge": 4,
    "Pre-seed": 4,
    "Series B": 2,
    "Grant": 1,
    "Debt": 1,
    "Unknown": 1,
    "Series C": 0,
    "Series D+": 0,
    "Growth": 0,
    "Strategic": 0,
}

AMOUNT_FLOOR = 1_000_000
AMOUNT_CEILING = 40_000_000

SMALL_TEAM = 10          # the threshold worth flagging explicitly
MEDIUM_TEAM = 50
LARGE_TEAM = 200
NEW_COMPANY_YEARS = 3

TIER_BOUNDS = ((16, "A"), (12, "B"), (8, "C"))


def _days_since(value: str) -> int | None:
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
    is_b2b: bool = False,
    hiring_flag: str = "",
    event_type: str = "",
    team_size: int | None = None,
    founded_year: int | None = None,
) -> tuple[int, str, str]:
    """Score one row.

    Returns ``(score, tier, why)``. ``why`` is a compact tag string written to the
    GTM Fit Reason column, e.g.
    ``SMALL-TEAM(8) · NEW-CO(2025) · PRIME-STAGE · $1-40M · HIRING · B2B · FRESH``
    so the sheet shows which signals fired rather than just a number.
    """
    score = 0
    tags: list[str] = []

    # Acquisitions, exec changes and the like are not outreach targets. A newly
    # incorporated company IS kept, because "is this a new company" is a signal
    # worth seeing -- it just scores low on its own, having no raise behind it.
    SCORABLE = ("Funding round", "Debt", "Company formation/incorporation", "")
    if event_type and event_type not in SCORABLE:
        return 0, "D", "NOT-A-FUNDING-EVENT"

    # --- team size: the signal that matters most, when the source states it ---
    if team_size is not None:
        if team_size <= SMALL_TEAM:
            score += 5
            tags.append(f"SMALL-TEAM({team_size})")
        elif team_size <= MEDIUM_TEAM:
            score += 2
            tags.append(f"team {team_size}")
        elif team_size >= LARGE_TEAM:
            score -= 3
            tags.append(f"LARGE-ORG({team_size})")
        else:
            tags.append(f"team {team_size}")

    # --- new company ---
    this_year = datetime.now(timezone.utc).year
    if founded_year and (this_year - founded_year) <= NEW_COMPANY_YEARS:
        score += 3
        tags.append(f"NEW-CO({founded_year})")
    if event_type == "Company formation/incorporation":
        score += 2
        tags.append("JUST-INCORPORATED")

    # --- stage: the fallback proxy for team size when headcount is unknown ---
    stage_points = STAGE_POINTS.get(stage, 1)
    score += stage_points
    if stage_points >= 5:
        tags.append("PRIME-STAGE")
    elif stage and stage != "Unknown":
        tags.append(stage)
    if team_size is None and stage_points >= 4:
        # Be explicit that smallness is inferred here, not established.
        tags.append("team-size-unknown")

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

    # --- hiring mention in the source ---
    if hiring_flag == "Yes":
        score += 3
        tags.append("HIRING")
    elif hiring_flag == "Maybe":
        score += 1
    elif hiring_flag == "No":
        score -= 3
        tags.append("LAYOFF-REPORTED")

    # --- fit and freshness ---
    if is_b2b:
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


def score_clean_row(clean: dict, fallback_date: str = "") -> tuple[int, str]:
    """Score a validated extraction, returning ``(score, why)`` for columns I and J.

    Tier is folded into ``why`` rather than taking a third column, so this fits the
    two columns the sheet already has instead of adding more.
    """
    score, tier, why = score_row(
        stage=clean.get("stage") or "",
        amount_usd=clean.get("amount_usd") or None,
        event_date=clean.get("event_date") or fallback_date,
        is_b2b=bool(clean.get("is_b2b")),
        hiring_flag=clean.get("hiring_flag") or "",
        event_type=clean.get("event_type") or "",
        team_size=clean.get("team_size"),
        founded_year=clean.get("founded_year"),
    )
    return score, f"[{tier}] {why}" if why else f"[{tier}]"
