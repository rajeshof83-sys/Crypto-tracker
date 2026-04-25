"""
Outcome Tracker - Follow-up Price Capture
==========================================
Runs hourly alongside scanner. Checks tokens that were alerted
and captures follow-up prices at 1h, 6h, 24h, 7d intervals.
Updates Google Sheets with outcome data.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

STATE_FILE = "scanner_state.json"
OUTCOMES_FILE = "outcomes_state.json"

# Intervals to capture (in hours)
CAPTURE_INTERVALS = {
    "price_1h": 1,
    "price_6h": 6,
    "price_24h": 24,
    "price_7d": 168,
}


def load_outcomes():
    if os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE, "r") as f:
            return json.load(f)
    return {"tracked_alerts": {}}


def save_outcomes(outcomes):
    with open(OUTCOMES_FILE, "w") as f:
        json.dump(outcomes, f, indent=2)


def fetch_current_price(chain, pair_address):
    """Fetch current price and liquidity for a pair."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        if pairs:
            pair = pairs[0]
            return {
                "price_usd": float(pair.get("priceUsd", 0) or 0),
                "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                "txns_1h_buys": int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0),
                "txns_1h_sells": int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0),
            }
    except Exception as e:
        print(f"[ERROR] Price fetch failed for {chain}/{pair_address}: {e}")
    return None


def compute_outcome_label(detection_price, peak_price, current_price, current_liq, detection_liq):
    """
    Classify outcome based on peak multiple and rug detection.
    
    Labels:
      moon     - peak >= 3x detection price
      winner   - peak 1.5x to 3x
      flat     - peak 0.8x to 1.5x
      bleed    - peak 0.3x to 0.8x
      rug      - liquidity dropped >80% or peak < 0.3x
    """
    if detection_price <= 0:
        return "unknown"

    peak_multiple = peak_price / detection_price
    liq_ratio = current_liq / detection_liq if detection_liq > 0 else 0

    # Rug check first
    if liq_ratio < 0.2:
        return "rug"
    if peak_multiple < 0.3:
        return "rug"

    if peak_multiple >= 3.0:
        return "moon"
    elif peak_multiple >= 1.5:
        return "winner"
    elif peak_multiple >= 0.8:
        return "flat"
    else:
        return "bleed"


def register_alert(token_key, chain, pair_address, detection_price, detection_liq, tier, symbol):
    """Register a new alert for outcome tracking."""
    outcomes = load_outcomes()

    if token_key not in outcomes["tracked_alerts"]:
        outcomes["tracked_alerts"][token_key] = {
            "chain": chain,
            "pair_address": pair_address,
            "symbol": symbol,
            "tier": tier,
            "detection_time": datetime.now(timezone.utc).isoformat(),
            "detection_price": detection_price,
            "detection_liq": detection_liq,
            "peak_price": detection_price,
            "captures": {},
            "outcome_label": None,
            "completed": False,
        }
        save_outcomes(outcomes)
        print(f"[OK] Registered {symbol} for outcome tracking")


def run_outcome_check():
    """Check all tracked alerts and capture follow-up prices."""
    print(f"\n{'='*60}")
    print(f"OUTCOME CHECK: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    outcomes = load_outcomes()
    now = datetime.now(timezone.utc)
    updates = 0

    for token_key, alert in outcomes["tracked_alerts"].items():
        if alert.get("completed"):
            continue

        detection_time = datetime.fromisoformat(alert["detection_time"])
        hours_elapsed = (now - detection_time).total_seconds() / 3600

        chain = alert["chain"]
        pair_address = alert["pair_address"]
        symbol = alert["symbol"]

        needs_fetch = False
        for interval_name, interval_hours in CAPTURE_INTERVALS.items():
            if interval_name not in alert["captures"]:
                if hours_elapsed >= interval_hours:
                    needs_fetch = True
                    break

        if not needs_fetch:
            continue

        # Rate limiting
        time.sleep(0.3)

        price_data = fetch_current_price(chain, pair_address)
        if not price_data:
            continue

        current_price = price_data["price_usd"]
        current_liq = price_data["liquidity"]

        # Update peak price
        if current_price > alert.get("peak_price", 0):
            alert["peak_price"] = current_price

        # Capture intervals
        for interval_name, interval_hours in CAPTURE_INTERVALS.items():
            if interval_name not in alert["captures"] and hours_elapsed >= interval_hours:
                alert["captures"][interval_name] = {
                    "price": current_price,
                    "liquidity": current_liq,
                    "volume_24h": price_data["volume_24h"],
                    "captured_at": now.isoformat(),
                    "hours_elapsed": round(hours_elapsed, 1),
                }
                multiple = current_price / alert["detection_price"] if alert["detection_price"] > 0 else 0
                print(f"  [{interval_name}] {symbol}: ${current_price:.8f} ({multiple:.2f}x detection)")
                updates += 1

        # Check if 7d capture is done → compute final outcome
        if "price_7d" in alert["captures"]:
            alert["outcome_label"] = compute_outcome_label(
                alert["detection_price"],
                alert["peak_price"],
                current_price,
                current_liq,
                alert["detection_liq"],
            )
            alert["completed"] = True
            peak_mult = alert["peak_price"] / alert["detection_price"] if alert["detection_price"] > 0 else 0
            print(f"  OUTCOME: {symbol} → {alert['outcome_label']} (peak {peak_mult:.1f}x)")

        # Early rug detection (24h)
        if "price_24h" in alert["captures"] and not alert.get("completed"):
            if current_liq < alert["detection_liq"] * 0.2:
                alert["outcome_label"] = "rug"
                alert["completed"] = True
                print(f"  EARLY RUG: {symbol} (liq collapsed)")

    save_outcomes(outcomes)

    # Summary
    total = len(outcomes["tracked_alerts"])
    completed = sum(1 for a in outcomes["tracked_alerts"].values() if a.get("completed"))
    print(f"\nOutcome tracking: {completed}/{total} completed, {updates} new captures")

    # Print outcome distribution if we have enough data
    if completed >= 5:
        labels = [a["outcome_label"] for a in outcomes["tracked_alerts"].values() if a.get("completed")]
        print("\nOutcome distribution:")
        for label in ["moon", "winner", "flat", "bleed", "rug"]:
            count = labels.count(label)
            if count:
                print(f"  {label}: {count} ({100*count/len(labels):.0f}%)")

        # Split by tier
        for tier in ["T1", "T2"]:
            tier_alerts = [a for a in outcomes["tracked_alerts"].values()
                         if a.get("completed") and a["tier"] == tier]
            if tier_alerts:
                tier_labels = [a["outcome_label"] for a in tier_alerts]
                moon_winner = sum(1 for l in tier_labels if l in ("moon", "winner"))
                print(f"\n  {tier} win rate: {100*moon_winner/len(tier_labels):.0f}% "
                      f"({moon_winner}/{len(tier_labels)} moon+winner)")


if __name__ == "__main__":
    run_outcome_check()
