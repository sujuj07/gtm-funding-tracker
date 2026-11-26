"""Tests that the ranking puts the right companies at the top.

Target profile: raised recently, still small, plausibly needs someone to build
revenue operations. These assert the *ordering* that profile implies, because a
scoring function that compiles but ranks badly is worse than useless -- it looks
authoritative while pointing at the wrong companies.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring import score_row, tier_for

FRESH = "2026-11-20"
BASE = dict(stage="Seed", amount_usd=3_000_000, event_date=FRESH, is_b2b=True,
            event_type="Funding round")


def _score(**kw):
    return score_row(**{**BASE, **kw})[0]


def test_stated_small_team_outranks_unknown_headcount():
    # The signal the user actually asked for: under ~10 people.
    assert _score(team_size=6) > _score(team_size=None)


def test_small_team_tag_names_the_number():
    _, _, why = score_row(**BASE, team_size=8)
    assert "SMALL-TEAM(8)" in why


def test_unknown_headcount_is_marked_as_inferred_not_asserted():
    # Stage is only a proxy for smallness; the sheet must say so.
    _, _, why = score_row(**BASE, team_size=None)
    assert "team-size-unknown" in why


def test_large_org_is_penalised():
    assert _score(team_size=300) < _score(team_size=None)
    _, _, why = score_row(**BASE, team_size=300)
    assert "LARGE-ORG(300)" in why


def test_recently_founded_company_scores_higher():
    assert _score(founded_year=2026) > _score(founded_year=2015)


def test_newly_incorporated_is_kept_and_tagged_not_discarded():
    # Scores low (no raise behind it) but must remain visible.
    score, _, why = score_row(stage="Unknown", amount_usd=None, event_date=FRESH,
                              event_type="Company formation/incorporation")
    assert "JUST-INCORPORATED" in why
    assert score > 0


def test_acquisitions_and_exec_changes_score_zero():
    score, tier, why = score_row(stage="Unknown", event_date=FRESH, event_type="Other")
    assert (score, tier, why) == (0, "D", "NOT-A-FUNDING-EVENT")


def test_raise_band_rewarded_and_oversized_raise_penalised():
    assert _score(amount_usd=15_000_000) > _score(amount_usd=800_000_000)


def test_stage_amount_mismatch_is_flagged_and_demoted():
    # The real "Uforce, Series A, $4B" row.
    score, tier, why = score_row(stage="Series A", amount_usd=4e9, event_date=FRESH, is_b2b=True,
                                 event_type="Funding round")
    assert "MISMATCH!" in why
    assert tier not in ("A", "B")


def test_missing_amount_is_tagged_not_guessed():
    _, _, why = score_row(**{**BASE, "amount_usd": None})
    assert "AMT?" in why


def test_layoff_report_outweighs_a_hiring_guess():
    assert _score(hiring_flag="No") < _score(hiring_flag="Yes")
    _, _, why = score_row(**BASE, hiring_flag="No")
    assert "LAYOFF-REPORTED" in why


def test_stale_rows_are_demoted():
    _, _, why = score_row(stage="Series A", amount_usd=10e6, event_date="2025-01-01")
    assert "STALE" in why


def test_best_case_outranks_everything_tested():
    best = _score(team_size=7, founded_year=2026, hiring_flag="Yes", amount_usd=5_000_000)
    for other in (
        _score(team_size=None),
        _score(team_size=300),
        _score(amount_usd=900_000_000),
        _score(hiring_flag="No"),
        _score(event_date="2024-01-01"),
    ):
        assert best > other


def test_tier_boundaries():
    assert tier_for(16) == "A"
    assert tier_for(15) == "B"
    assert tier_for(12) == "B"
    assert tier_for(11) == "C"
    assert tier_for(8) == "C"
    assert tier_for(7) == "D"


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
