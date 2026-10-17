"""Optional, free RootData API connection check. Never spends project-detail credits."""
from __future__ import annotations
import os
import requests

API_KEY = os.getenv("ROOTDATA_API_KEY", "").strip()
BASE = "https://api.rootdata.com/open"


def post(path: str, body: dict | None = None) -> dict:
    response = requests.post(
        f"{BASE}/{path}",
        headers={"apikey": API_KEY, "language": "en", "Content-Type": "application/json"},
        json=body or {},
        timeout=(4, 15),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("result") not in (None, 200, "200"):
        raise RuntimeError(f"RootData {path} returned an API error")
    return payload


def main() -> int:
    if not API_KEY:
        print("[rootdata-check] ROOTDATA_API_KEY secret not configured; RootData enrichment is disabled. Other sources will continue.")
        return 0
    try:
        quota = post("quotacredits").get("data")
        if not isinstance(quota, dict) or quota.get("credits") is None:
            raise RuntimeError("quota endpoint did not return current credits")
        remaining = int(quota["credits"])
        total = int(quota.get("total_credits") or 1000)
        level = str(quota.get("level") or "unknown")
        # Search is documented as unlimited per the RootData API guide and does not use the 1,000-credit balance.
        search = post("ser_inv", {"query": "Ethereum"}).get("data")
        if not isinstance(search, (list, dict)):
            raise RuntimeError("project search returned an unexpected payload")
        print(f"[rootdata-check] connected; plan={level}; remaining={remaining}/{total} credits; free project search reachable; no detail credits spent")
        print("[rootdata-check] tracker caps: <=24 credits/day, <=800/month, preserve >=200 account credits; each uncached project detail costs 2 credits")
    except Exception as exc:
        print(f"[rootdata-check] WARNING: RootData API check failed ({type(exc).__name__}: {exc}). Tracker will continue using non-RootData sources; verify ROOTDATA_API_KEY and account access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
