"""Regression tests built from values that actually reached the sheet.

Every case here is a real failure observed in "Funding Intelligence", not a
hypothetical. Run with:  python -m pytest tests/ -q      (or: python tests/test_validate.py)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import validate as v


def test_stage_enum_echo_is_rejected():
    # The model returned the whole enum list instead of picking one.
    stage, note = v.coerce_stage("Series A|Series B|Series C|Series D+|Growth|Unknown")
    assert stage == "Unknown"
    assert "echoed" in note


def test_stage_valid_value_passes_through():
    assert v.coerce_stage("Series A")[0] == "Series A"


def test_stage_inferred_from_source_text():
    assert v.coerce_stage("", "ZuriQ raises $25.5m seed")[0] == "Seed"
    assert v.coerce_stage("Unknown", "secures a $6M bridge round")[0] == "Bridge"
    assert v.coerce_stage("", "a pre-seed round")[0] == "Pre-seed"


def test_explicit_series_beats_a_prior_seed_mention():
    assert v.coerce_stage("", "raises Series A after its 2024 seed round")[0] == "Series A"


def test_amount_parses_common_formats():
    assert v.coerce_amount_usd("$10M")[0] == 10_000_000
    assert v.coerce_amount_usd(10_000_000)[0] == 10_000_000
    assert v.coerce_amount_usd("$2.5B")[0] == 2_500_000_000
    assert v.coerce_amount_usd("$1,500,000")[0] == 1_500_000


def test_amount_converts_non_usd_instead_of_treating_it_as_dollars():
    # "R$30M" is Brazilian reais; it was being written to the sheet as $30M.
    assert v.coerce_amount_usd("R$30M")[0] == 5_400_000
    assert v.coerce_amount_usd("Rs 43.7 Cr")[0] == 5_244_000
    assert v.coerce_amount_usd("₹10.2 Cr")[0] == 1_224_000


def test_amount_rejects_revenue_and_valuation_figures():
    # Genrobotics: "operating revenue" became a raise amount.
    assert v.coerce_amount_usd("Robotics startup with ₹43.7 Cr operating revenue")[0] is None
    assert v.coerce_amount_usd("valued at $40 billion")[0] is None


def test_amount_rejects_implausible_magnitudes():
    assert v.coerce_amount_usd(0)[0] is None
    assert v.coerce_amount_usd(500)[0] is None


def test_stage_amount_conflict_is_flagged():
    # Uforce was written as a $4B Series A.
    assert v.amount_stage_conflict("Series A", 4_000_000_000)
    assert v.amount_stage_conflict("Seed", 500_000_000)
    assert v.amount_stage_conflict("Series A", 15_000_000) == ""
    assert v.amount_stage_conflict("Series C", 750_000_000) == ""


def test_hiring_prose_is_split_into_flag_and_text():
    assert v.coerce_hiring("Staff reduction of 20% announced")[0] == "No"
    assert v.coerce_hiring("24 open positions in Massachusetts")[0] == "Yes"
    flag, text = v.coerce_hiring("Hiring 44 new employees")
    assert (flag, text) == ("Yes", "Hiring 44 new employees")
    assert v.coerce_hiring("")[0] == "Unknown"


def test_url_placeholders_are_discarded():
    assert v.coerce_url("N/A") == ""
    assert v.coerce_url("unknown") == ""
    assert v.coerce_url("example.com") == "https://example.com"


def test_row_with_no_company_is_unusable():
    # This is the last guard against blank rows reaching the webhook.
    clean, _ = v.validate_extraction({"company": ""})
    assert v.is_usable(clean) is False


def test_placeholder_names_are_dropped():
    clean, _ = v.validate_extraction({"company": "X", "founders": ["Ann", "null", "N/A"]})
    assert clean["founders"] == ["Ann"]


def test_event_type_enum_echo_falls_back_to_other():
    assert v.coerce_event_type("Funding round|Other")[0] == "Other"
    assert v.coerce_event_type("Funding round")[0] == "Funding round"


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
