"""Tests for the SEC Form D source.

Form D is the only source here whose data is first-party and attested rather than
inferred from prose, so the parser must not quietly degrade it. Fixtures in
tests/fixtures/ mirror the real EDGAR shapes: an operating company, a VC fund
(which must be filtered out), and a filing where nothing has been sold yet.

NOT COVERED: these run against fixtures, not live EDGAR. The environment this was
written in had outbound network disabled, so field paths are verified against the
documented Form D schema rather than against a real response.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")

import sec_form_d as sec
from scoring import score_clean_row


def _fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return handle.read()


def test_index_picks_only_original_form_d_filings():
    entries = sec.parse_form_d_entries(_fixture("form_index_sample.idx"))
    names = [e["company"] for e in entries]
    assert names == ["ACME ROBOTICS INC.", "EARLYBIRD AI LABS INC"]
    # "D/A" amendments and "DEF 14A" must not match a "D " prefix test.
    assert not any("AMENDED" in n or "SOME BIG CORP" in n for n in names)


def test_index_entry_fields_survive_company_names_with_spaces():
    entry = sec.parse_form_d_entries(_fixture("form_index_sample.idx"))[0]
    assert entry["cik"] == "1234567"
    assert entry["filed_date"] == "20261127"
    assert entry["txt_path"].endswith(".txt")


def test_primary_doc_url_derivation():
    url = sec.primary_doc_url("1234567", "edgar/data/1234567/000123456726000123.txt")
    assert url.endswith("/Archives/edgar/data/1234567/000123456726000123/primary_doc.xml")


def test_operating_company_fields_come_from_the_filing():
    clean = sec.parse_primary_doc(_fixture("form_d_sample.xml"))
    assert clean["company"] == "ACME ROBOTICS INC."
    assert clean["amount_usd"] == 4_200_000
    assert clean["founded_year"] == 2025
    assert clean["sector"] == "Other Technology"
    assert clean["confidence"] == "High"
    assert clean["_total_offering"] == 5_000_000


def test_officers_and_directors_become_named_contacts():
    # The main thing Form D gives that news sources do not.
    clean = sec.parse_primary_doc(_fixture("form_d_sample.xml"))
    assert clean["founders"] == ["Jane Doe", "Raj Patel"]


def test_us_state_is_not_written_into_the_country_field():
    clean = sec.parse_primary_doc(_fixture("form_d_sample.xml"))
    assert clean["hq_city"] == "San Francisco, CA"
    assert clean["hq_country"] == "United States"


def test_nothing_sold_yet_is_unknown_not_zero():
    # A company can file before selling any of its offering. 0 is not a $0 round.
    clean = sec.parse_primary_doc(_fixture("form_d_nothing_sold.xml"))
    assert clean["amount_usd"] is None
    assert clean["_total_offering"] == 3_000_000


def test_pooled_investment_funds_are_filtered_out():
    # A VC raising its own fund files a Form D; it is not an outreach target.
    clean = sec.parse_primary_doc(_fixture("form_d_fund.xml"))
    assert clean is not None
    assert sec.is_relevant(clean) is False


def test_operating_companies_pass_the_relevance_filter():
    for name in ("form_d_sample.xml", "form_d_nothing_sold.xml"):
        assert sec.is_relevant(sec.parse_primary_doc(_fixture(name))) is True


def test_stage_is_left_unknown_rather_than_invented():
    # Form D does not state a round stage. Inferring "Seed" from the amount would
    # be a guess written into a validated column.
    clean = sec.parse_primary_doc(_fixture("form_d_sample.xml"))
    assert clean["stage"] == "Unknown"
    assert clean["one_liner"] == ""


def test_malformed_input_returns_none_rather_than_raising():
    for bad in ("<<<not xml", "", "<?xml version='1.0'?><edgarSubmission/>"):
        assert sec.parse_primary_doc(bad) is None


def test_weekends_are_skipped_in_the_index_url_list():
    urls = sec.daily_index_urls(7)
    import datetime as dt
    for url in urls:
        stamp = url.rsplit("form.", 1)[1].removesuffix(".idx")
        assert dt.datetime.strptime(stamp, "%Y%m%d").weekday() < 5


def test_a_form_d_row_scores_without_stage_or_b2b():
    # Form D rows lack stage (+5) and B2B (+2), so they cluster in B/C rather
    # than A. They must still score on what the filing does provide.
    clean = sec.parse_primary_doc(_fixture("form_d_sample.xml"))
    clean["event_date"] = "2026-11-27"
    score, why = score_clean_row(clean, fallback_date="2026-11-27")
    assert score > 0
    assert "NAMED-CONTACTS(2)" in why
    assert "NEW-CO(2025)" in why
    assert "$1-40M" in why


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
