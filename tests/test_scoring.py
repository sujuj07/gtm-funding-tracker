"""Tests that the ranking puts the right companies at the top.

The target profile: recently funded, still small, and visibly hiring a
growth/BD/RevOps person. These tests assert the *ordering* that profile implies,
because a scoring function that compiles but ranks badly is worse than useless --
it looks authoritative while sending you at the wrong companies.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import score_queue_row, score_row, tier_for

FRESH = "2026-11-20"


def _score(**kw):
    return score_row(**kw)[0]


def test_open_growth_role_outranks_a_board_with_none():
    base = dict(stage="Series A", amount_usd=15e6, event_date=FRESH,
                customer_type="B2B", ats_platform="lever", open_roles=9)
    assert _score(**base, target_roles=2) > _score(**base, target_roles=0)


def test_verified_board_outranks_a_news_only_hiring_claim():
    verified = _score(stage="Series A", amount_usd=15e6, event_date=FRESH, customer_type="B2B",
                      ats_platform="lever", open_roles=9, target_roles=2)
    news_only = _score(stage="Series A", amount_usd=15e6, event_date=FRESH, customer_type="B2B",
                       hiring_flag="Yes")
    assert verified > news_only


def test_small_recent_raise_outranks_a_large_late_one():
    target = _score(stage="Series A", amount_usd=15e6, event_date=FRESH, customer_type="B2B",
                    ats_platform="lever", open_roles=9, target_roles=2)
    too_big = _score(stage="Series C", amount_usd=750e6, event_date=FRESH, customer_type="B2B",
                     ats_platform="greenhouse", open_roles=400, target_roles=20)
    assert target > too_big


def test_large_org_is_penalised_despite_many_growth_roles():
    # 20 open growth roles at a 400-role company is not the target profile.
    score, tier, signals = score_row(
        stage="Series C", amount_usd=750e6, event_date=FRESH, customer_type="B2B",
        ats_platform="greenhouse", open_roles=400, target_roles=20)
    assert tier in ("C", "D")
    assert "LARGE-ORG" in signals


def test_stage_amount_mismatch_is_flagged_and_demoted():
    # The real "Uforce, Series A, $4B" row.
    score, tier, signals = score_row(
        stage="Series A", amount_usd=4e9, event_date=FRESH, customer_type="B2B",
        ats_platform="greenhouse", open_roles=250, target_roles=5)
    assert "MISMATCH!" in signals
    assert tier not in ("A", "B")


def test_non_funding_events_score_zero():
    score, tier, signals = score_row(stage="Unknown", event_type="Other", event_date=FRESH)
    assert score == 0
    assert signals == "NOT-A-FUNDING-EVENT"


def test_unchecked_board_is_not_treated_as_zero_roles():
    # None means "not checked"; 0 means "checked, none open". They must differ.
    unchecked = _score(stage="Seed", amount_usd=5e6, event_date=FRESH, customer_type="B2B")
    checked_empty = _score(stage="Seed", amount_usd=5e6, event_date=FRESH, customer_type="B2B",
                           ats_platform="ashby", open_roles=0, target_roles=0)
    assert unchecked != checked_empty


def test_missing_amount_is_tagged_not_guessed():
    _, _, signals = score_row(stage="Seed", amount_usd=None, event_date=FRESH)
    assert "AMT?" in signals


def test_stale_rows_are_demoted():
    _, _, signals = score_row(stage="Series A", amount_usd=10e6, event_date="2025-01-01")
    assert "STALE" in signals


def test_layoff_report_is_surfaced():
    _, _, signals = score_row(stage="Seed", amount_usd=3e6, event_date=FRESH, customer_type="B2B",
                              hiring_flag="No", ats_platform="ashby", open_roles=4, target_roles=0)
    assert "LAYOFF-REPORTED" in signals


def test_tier_boundaries():
    assert tier_for(18) == "A"
    assert tier_for(17) == "B"
    assert tier_for(13) == "B"
    assert tier_for(12) == "C"
    assert tier_for(8) == "C"
    assert tier_for(7) == "D"


def test_payload_never_includes_human_edited_columns():
    # The enrichment pass must not be able to overwrite outreach notes.
    payload = score_queue_row(
        {"key": "k", "stage": "Seed", "amount": 5e6, "date": FRESH,
         "customerType": "B2B", "eventType": "Funding round"},
        {"platform": "ashby", "slug": "x", "open_roles": 5, "target_roles": 1,
         "checked_at": "2026-11-26 12:00 UTC"},
    )
    for field in ("pocName", "pocTitle", "pocContact", "outreachStatus", "outreachDate", "outreachNotes"):
        assert field not in payload


def test_unreachable_board_reports_blank_not_zero():
    # enrich_company returned no counts; the sheet must show blank, not 0.
    payload = score_queue_row(
        {"key": "k", "stage": "Seed", "amount": 5e6, "date": FRESH, "eventType": "Funding round"},
        {"platform": "greenhouse", "slug": "x", "checked_at": "2026-11-26 12:00 UTC",
         "status": "board found, API unreachable"},
    )
    assert payload["openRoles"] == ""
    assert payload["targetRoles"] == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    print(f"\n{'all tests passed' if not failures else f'{failures} test(s) failed'}")
    sys.exit(1 if failures else 0)
