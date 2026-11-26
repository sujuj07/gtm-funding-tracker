"""Strict validation and coercion for model-extracted fields.

Groq's ``response_format={"type": "json_object"}`` guarantees the model returns
syntactically valid JSON. It guarantees nothing about the *values*. In practice
``openai/gpt-oss-20b`` handed a 25-field schema will:

  * echo the whole enum list back      -> stage = "Series A|Series B|Series C|..."
  * return amounts as formatted text   -> amount_usd = "$10M" / "Rs 43.7 Cr"
  * put prose in an enum field         -> hiring_signal = "Staff reduction of 20%"
  * report a valuation as the raise    -> amount_usd taken from "valued at $3B"

Every one of those reached the sheet before this module existed, and any
downstream code doing ``row["stage"] == "Series A"`` or ``float(amount)`` throws
on them. Nothing goes to the sheet without passing through here.

Design rule: when a value cannot be trusted, return the empty/Unknown form and
record a problem note. Never guess, and never silently keep a half-parsed value.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- enums

STAGES = (
    "Pre-seed", "Seed", "Seed Extension", "Bridge", "Series A", "Series B",
    "Series C", "Series D+", "Growth", "Strategic", "Grant", "Debt", "Unknown",
)
_STAGE_LOOKUP = {s.lower(): s for s in STAGES}

EVENT_TYPES = (
    "Funding round", "Company formation/incorporation", "Hiring announcement", "Other",
)
_EVENT_LOOKUP = {e.lower(): e for e in EVENT_TYPES}

CONFIDENCES = ("High", "Medium", "Low")
_CONF_LOOKUP = {c.lower(): c for c in CONFIDENCES}

PRODUCT_TYPES = (
    "software", "hardware", "infrastructure", "financial service", "protocol",
    "marketplace", "other",
)

# Stage phrases, most specific first. Order matters: "pre-seed" must beat "seed",
# and an explicit "series a" must beat a passing mention of a previous seed round.
_STAGE_PATTERNS = (
    (r"\bpre[-\s]?seed\b", "Pre-seed"),
    (r"\bseed\s+extension\b", "Seed Extension"),
    (r"\bextended?\s+its\s+seed\b", "Seed Extension"),
    (r"\bseries\s*a\b", "Series A"),
    (r"\bseries\s*b\b", "Series B"),
    (r"\bseries\s*c\b", "Series C"),
    (r"\bseries\s*[d-h]\b", "Series D+"),
    (r"\bseed\s+round\b", "Seed"),
    (r"\bseed\s+funding\b", "Seed"),
    (r"\bseed\b", "Seed"),
    (r"\bbridge\s+(round|financing|funding)\b", "Bridge"),
    (r"\bbridge\b", "Bridge"),
    (r"\b(growth|pre[-\s]?ipo|late[-\s]stage)\b", "Growth"),
    (r"\b(venture\s+debt|debt\s+(financing|facility|round))\b", "Debt"),
    (r"\bgrant\b", "Grant"),
    (r"\bstrategic\s+(investment|round)\b", "Strategic"),
)

# ---------------------------------------------------------------- amounts

# A clause mentioning any of these is NOT the raise amount. Stripped before any
# prose parsing. "operating revenue of Rs 43.7 Cr" must never become a raise.
_NOT_A_RAISE = re.compile(
    r"[^.;]*\b(valuation|valued\s+at|valuing|post[-\s]money|pre[-\s]money|market\s+cap|"
    r"\baum\b|fund\s+size|corpus|operating\s+revenue|revenue|\barr\b|turnover|\bgmv\b|"
    r"profit|ebitda|debut\s+fund|maiden\s+fund|new\s+fund|assets\s+under)\b[^.;]*",
    re.I,
)

# Rate table is deliberately coarse and explicit. These are not live FX rates;
# they exist so a non-USD figure lands in the right order of magnitude instead of
# being silently treated as dollars (R$30M was becoming $30M).
_FX_TO_USD = {
    "$": 1.0, "usd": 1.0, "us$": 1.0,
    "eur": 1.09, "€": 1.09,
    "gbp": 1.27, "£": 1.27,
    "inr": 0.012, "₹": 0.012, "rs": 0.012,
    "brl": 0.18, "r$": 0.18,
    "aud": 0.65, "a$": 0.65,
    "cad": 0.73, "c$": 0.73,
    "chf": 1.12, "sgd": 0.74, "jpy": 0.0064, "¥": 0.0064,
}

_MULTIPLIERS = (
    (r"\b(billion|bn)\b|\d\s*b\b", 1e9),
    (r"\b(crore|cr)\b", 1e7),
    (r"\blakhs?\b", 1e5),
    (r"\b(million|mn)\b|\d\s*m\b", 1e6),
    (r"\b(thousand)\b|\d\s*k\b", 1e3),
)

# Below this, the figure is almost certainly a unit error rather than a raise.
MIN_PLAUSIBLE_USD = 10_000
# Above this, it is a typo, a market cap, or a currency mix-up.
MAX_PLAUSIBLE_USD = 500_000_000_000


def _detect_currency(text: str) -> float:
    """Return the USD rate for the currency named in ``text`` (default 1.0)."""
    # Longest tokens first so "r$" wins over "$" and "us$" over "$".
    for token in sorted(_FX_TO_USD, key=len, reverse=True):
        if token in text:
            return _FX_TO_USD[token]
    return 1.0


def coerce_amount_usd(raw: Any) -> tuple[float | None, str]:
    """Coerce a *reported* funding amount to a USD float.

    Returns ``(amount_or_None, problem_note)``. ``problem_note`` is "" when the
    value was accepted as-is.

    This reads the amount field only -- never the headline or the body. Numbers
    recovered from prose are guesses, and auditing them showed exactly why that
    is unsafe: "operating revenue of Rs 43.7 Cr" and "valuing it at $40 billion"
    both look like raise amounts to a regex. A guess does not belong in a column
    labelled USD, so an unparseable amount stays empty.
    """
    if raw is None or raw == "":
        return None, ""

    # Already numeric: trust it only if it is plausible as a raise.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value <= 0:
            return None, "amount was zero or negative"
        if value < MIN_PLAUSIBLE_USD:
            return None, f"amount {value:g} below plausible floor (unit error?)"
        if value > MAX_PLAUSIBLE_USD:
            return None, f"amount {value:g} above plausible ceiling"
        return value, ""

    text = str(raw).strip()
    if not text:
        return None, ""

    # A sentence in the amount field means the model ignored the schema. Refuse it
    # rather than regexing a number out of narrative text.
    if len(text.split()) > 6 or _NOT_A_RAISE.search(text):
        return None, "amount field contained prose or a non-raise figure, not an amount"

    low = text.replace(",", "").lower()
    if not re.search(r"\d", low):
        return None, "amount field contained no digits"

    match = re.search(r"(\d+(?:\.\d+)?)", low)
    if not match:
        return None, "could not read a number from the amount"
    number = float(match.group(1))

    multiplier = 1.0
    for pattern, value in _MULTIPLIERS:
        if re.search(pattern, low):
            multiplier = value
            break

    rate = _detect_currency(low)
    # crore/lakh are Indian units; treat them as INR even when the symbol is absent.
    if multiplier in (1e7, 1e5) and rate == 1.0:
        rate = _FX_TO_USD["inr"]

    usd = number * multiplier * rate
    if usd < MIN_PLAUSIBLE_USD:
        return None, f"parsed {usd:,.0f} USD, below plausible floor"
    if usd > MAX_PLAUSIBLE_USD:
        return None, f"parsed {usd:,.0f} USD, above plausible ceiling"
    return round(usd), ("converted from non-USD" if rate != 1.0 else "")


def amount_stage_conflict(stage: str, amount_usd: float | None) -> str:
    """Flag a stage/amount pair that cannot both be right.

    A "$22B Series A" is either a parse error or a mislabelled stage. Returning a
    note lets the caller keep the row but mark it instead of trusting it.
    """
    if amount_usd is None:
        return ""
    if stage in ("Pre-seed", "Seed", "Seed Extension") and amount_usd > 100_000_000:
        return f"{stage} with ${amount_usd:,.0f} is implausible"
    if stage == "Series A" and amount_usd > 150_000_000:
        return f"Series A with ${amount_usd:,.0f} is implausible"
    return ""


# ---------------------------------------------------------------- enum coercion


def coerce_stage(raw: Any, *fallback_text: str) -> tuple[str, str]:
    """Coerce a funding stage to one of ``STAGES``.

    Handles the enum echo ("Series A|Series B|...") by rejecting any value
    containing a pipe, then falls back to reading the stage out of the headline
    and evidence note.
    """
    text = str(raw or "").strip()
    note = ""

    if "|" in text:
        note = "model echoed the stage enum instead of choosing"
        text = ""

    if text:
        exact = _STAGE_LOOKUP.get(text.lower())
        if exact and exact != "Unknown":
            return exact, note
        for pattern, label in _STAGE_PATTERNS:
            if re.search(pattern, text, re.I):
                return label, note
        if text.lower() != "unknown":
            note = note or f"stage {text!r} not in the allowed set"

    blob = " ".join(str(t or "") for t in fallback_text)
    if blob.strip():
        for pattern, label in _STAGE_PATTERNS:
            if re.search(pattern, blob, re.I):
                return label, (note + "; " if note else "") + "stage inferred from source text"
    return "Unknown", note


def coerce_event_type(raw: Any) -> tuple[str, str]:
    """Coerce an event type to one of ``EVENT_TYPES``, defaulting to Other."""
    text = str(raw or "").strip()
    if "|" in text:
        return "Other", "model echoed the event_type enum instead of choosing"
    if not text:
        return "Other", "event_type missing"
    exact = _EVENT_LOOKUP.get(text.lower())
    if exact:
        return exact, ""
    low = text.lower()
    if "fund" in low or "rais" in low:
        return "Funding round", f"event_type {text!r} mapped to Funding round"
    if "incorporat" in low or "formation" in low or "launch" in low:
        return "Company formation/incorporation", f"event_type {text!r} mapped to formation"
    if "hir" in low:
        return "Hiring announcement", f"event_type {text!r} mapped to hiring"
    return "Other", f"event_type {text!r} not in the allowed set"


# Hiring is stored as a clean flag plus the original text, so prose stops
# contaminating what should be a filterable column.
_HIRING_NEGATIVE = re.compile(
    r"\b(layoff|laid\s+off|staff\s+reduction|redundanc|downsiz|hiring\s+freeze|"
    r"job\s+cuts?|cut\s+\d+%|shrink)\b", re.I,
)
_HIRING_POSITIVE = re.compile(
    r"\b(hiring|hire[sd]?|open\s+(roles?|positions?)|recruit|expand(ing)?\s+the\s+team|"
    r"growing\s+the\s+team|headcount\s+(growth|increase)|\d+\s+open\b|join\s+us)\b", re.I,
)


def coerce_hiring(raw: Any) -> tuple[str, str]:
    """Split a hiring signal into (flag, original_text).

    flag is one of Yes / No / Maybe / Unknown. The original text is preserved
    verbatim so nothing the source said is lost -- it just stops sitting in a
    column that other code tries to compare against "Yes".
    """
    text = str(raw or "").strip()
    if not text or text.lower() in ("unknown", "none", "n/a", "null"):
        return "Unknown", ""
    low = text.lower()
    if low in ("yes", "true"):
        return "Yes", ""
    if low in ("no", "false"):
        return "No", ""
    if low == "maybe":
        return "Maybe", ""
    if _HIRING_NEGATIVE.search(text):
        return "No", text
    if _HIRING_POSITIVE.search(text):
        return "Yes", text
    return "Maybe", text


def coerce_confidence(raw: Any) -> str:
    return _CONF_LOOKUP.get(str(raw or "").strip().lower(), "Low")


def coerce_url(raw: Any) -> str:
    """Return a syntactically valid http(s) URL or "".

    The model is told never to invent a website; this catches it doing so anyway
    with values like "N/A", "unknown" or a bare company name.
    """
    text = str(raw or "").strip().rstrip("/")
    if not text or text.lower() in ("null", "none", "n/a", "na", "unknown", "-"):
        return ""
    if not text.startswith(("http://", "https://")):
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", text, re.I):
            text = "https://" + text
        else:
            return ""
    if not re.match(r"https?://[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", text, re.I):
        return ""
    return text


def coerce_name_list(raw: Any) -> list[str]:
    """Normalise a list-of-names field, dropping placeholders."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for item in items:
        name = str(item or "").strip().strip(",")
        if not name or name.lower() in ("null", "none", "n/a", "na", "unknown", "-"):
            continue
        if name not in out:
            out.append(name)
    return out


def clean_text(raw: Any, limit: int = 1200) -> str:
    """Collapse whitespace and strip placeholder values.

    Also strips tabs and newlines: those break the TSV-shaped payload the
    webhook receives and can shift values into neighbouring columns.
    """
    text = str(raw or "").strip()
    if text.lower() in ("null", "none", "n/a", "na", "undefined", "-"):
        return ""
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


# ---------------------------------------------------------------- row validation


def validate_extraction(data: dict) -> tuple[dict, list[str]]:
    """Validate and coerce one model extraction.

    Returns ``(clean, problems)``. ``problems`` is appended to the row's notes so
    a bad extraction is visible in the sheet rather than invisible, and so the
    failure rate of the extraction model is measurable over time.
    """
    problems: list[str] = []

    def note(message: str) -> None:
        if message:
            problems.append(message)

    company = clean_text(data.get("company"), 200)
    if not company:
        note("no company name extracted")

    event_type, event_note = coerce_event_type(data.get("event_type"))
    note(event_note)

    stage, stage_note = coerce_stage(
        data.get("stage"), data.get("one_liner"), data.get("evidence_note"),
    )
    note(stage_note)

    amount, amount_note = coerce_amount_usd(data.get("amount_usd"))
    note(amount_note)

    valuation, _ = coerce_amount_usd(data.get("valuation_usd"))

    note(amount_stage_conflict(stage, amount))

    hiring_flag, hiring_text = coerce_hiring(data.get("hiring_signal"))

    website = coerce_url(data.get("website"))
    if data.get("website") and not website:
        note(f"discarded unusable website value {str(data.get('website'))[:60]!r}")

    product_type = clean_text(data.get("product_type"), 60).lower()
    if product_type and product_type not in PRODUCT_TYPES:
        note(f"product_type {product_type!r} not in the allowed set")
        product_type = "other"

    clean = {
        "company": company,
        "event_type": event_type,
        "stage": stage,
        "amount_usd": amount,
        "valuation_usd": valuation,
        "sector": clean_text(data.get("sector"), 200),
        "one_liner": clean_text(data.get("one_liner"), 400),
        "is_b2b": bool(data.get("is_b2b")),
        "hiring_flag": hiring_flag,
        "hiring_text": hiring_text,
        "website": website,
        "founders": coerce_name_list(data.get("founders")),
        "lead_investor": clean_text(data.get("lead_investor"), 200),
        "other_investors": coerce_name_list(data.get("other_investors")),
        "broad_offerings": clean_text(data.get("broad_offerings"), 600),
        "niche_presence": clean_text(data.get("niche_presence"), 600),
        "product_type": product_type,
        "target_customers": clean_text(data.get("target_customers"), 300),
        "hq_city": clean_text(data.get("hq_city"), 120),
        "hq_country": clean_text(data.get("hq_country"), 120),
        "confidence": coerce_confidence(data.get("confidence")),
        "evidence_note": clean_text(data.get("evidence_note"), 600),
        "event_date": clean_text(data.get("event_date"), 10),
    }
    return clean, problems


def is_usable(clean: dict) -> bool:
    """A row with no company name can never be acted on; reject it outright.

    This is the last line of defence against the blank rows: if the extraction
    produced nothing identifiable, it must not reach the webhook at all.
    """
    return bool(clean.get("company"))
