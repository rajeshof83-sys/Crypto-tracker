"""
Exit Signal Monitor
===================
Monitors previously alerted tokens for deterioration signals.
Fires exit warnings when tokens show signs of dying.
"""

import json
import os
from datetime import datetime, timezone

STATE_FILE = "scanner_state.json"
EXIT_STATE_FILE = "exit_state.json"


def load_exit_state():
    default = {"peak_metrics": {}, "consecutive_negative": {}, "exit_alerts_sent": {}}
    if os.path.exists(EXIT_STATE_FILE):
        try:
            with open(EXIT_STATE_FILE, "r") as f:
                data = json.load(f)
            # Ensure all keys exist
            for key in default:
                if key not in data:
                    data[key] = default[key]
            return data
        except (json.JSONDecodeError, Exception):
            return default
    return default


def save_exit_state(state):
    with open(EXIT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_exit_signals(token_key, metrics, safety_score, scanner_state):
    """
    Check if a previously alerted token is deteriorating.
    
    Returns list of (signal_type, severity, message) tuples.
    """
    signals = []
    history = scanner_state.get("seen_tokens", {}).get(token_key, [])

    if len(history) < 2:
        return signals

    exit_state = load_exit_state()

    # Initialize peak tracking
    if token_key not in exit_state["peak_metrics"]:
        peak_liq = max(h.get("liquidity", 0) for h in history)
        peak_vol = max(h.get("volume_24h", 0) for h in history)
        peak_safety = max(h.get("safety_score", 0) for h in history)
        exit_state["peak_metrics"][token_key] = {
            "peak_liq": peak_liq,
            "peak_vol": peak_vol,
            "peak_safety": peak_safety,
        }
    else:
        # Update peaks
        peak = exit_state["peak_metrics"][token_key]
        peak["peak_liq"] = max(peak["peak_liq"], metrics["liquidity"])
        peak["peak_vol"] = max(peak["peak_vol"], metrics["volume_24h"])
        peak["peak_safety"] = max(peak["peak_safety"], safety_score)

    peak = exit_state["peak_metrics"][token_key]
    current_liq = metrics["liquidity"]
    current_vol = metrics["volume_24h"]
    current_pc = metrics["price_change_6h"]

    # ── SIGNAL 1: Liquidity collapse ──
    if peak["peak_liq"] > 0:
        liq_pct = current_liq / peak["peak_liq"]
        if liq_pct < 0.5:
            signals.append(("LIQ_COLLAPSE", "🔴 CRITICAL",
                f"Liquidity collapsed {(1-liq_pct)*100:.0f}% from peak "
                f"(${peak['peak_liq']:,.0f} → ${current_liq:,.0f})"))
        elif liq_pct < 0.7:
            signals.append(("LIQ_DECLINE", "🟠 WARNING",
                f"Liquidity down {(1-liq_pct)*100:.0f}% from peak "
                f"(${peak['peak_liq']:,.0f} → ${current_liq:,.0f})"))

    # ── SIGNAL 2: Volume death ──
    if peak["peak_vol"] > 0:
        vol_pct = current_vol / peak["peak_vol"]
        if vol_pct < 0.2:
            signals.append(("VOL_DEATH", "🔴 CRITICAL",
                f"Volume collapsed {(1-vol_pct)*100:.0f}% from peak — interest dying"))
        elif vol_pct < 0.4:
            signals.append(("VOL_DECLINE", "🟠 WARNING",
                f"Volume down {(1-vol_pct)*100:.0f}% from peak"))

    # ── SIGNAL 3: Safety score drop ──
    if safety_score <= peak["peak_safety"] - 2:
        signals.append(("SAFETY_DROP", "🔴 CRITICAL",
            f"Safety dropped {peak['peak_safety']} → {safety_score} "
            f"(contract quality deteriorating)"))

    # ── SIGNAL 4: Consecutive negative scans ──
    if token_key not in exit_state["consecutive_negative"]:
        exit_state["consecutive_negative"][token_key] = 0

    if current_pc < 0:
        exit_state["consecutive_negative"][token_key] += 1
    else:
        exit_state["consecutive_negative"][token_key] = 0

    neg_streak = exit_state["consecutive_negative"][token_key]
    if neg_streak >= 3:
        signals.append(("NEG_STREAK", "🟠 WARNING",
            f"{neg_streak} consecutive negative scans — sustained selling pressure"))

    # ── SIGNAL 5: Turnover death (vol/liq below 3x) ──
    vol_liq = metrics["vol_liq_ratio"]
    if vol_liq < 2.0 and len(history) >= 3:
        # Check if it was previously active
        prev_vol_liqs = [h.get("vol_liq_ratio", 0) for h in history[-3:]]
        if any(vl > 5.0 for vl in prev_vol_liqs):
            signals.append(("TURNOVER_DEATH", "🟡 WATCH",
                f"Turnover dropped to {vol_liq:.1f}x — was previously active"))

    # ── SIGNAL 6: Below entry liquidity ──
    first_liq = history[0].get("liquidity", 0)
    if first_liq > 0 and current_liq < first_liq * 0.8:
        signals.append(("BELOW_ENTRY", "🔴 CRITICAL",
            f"Liquidity below first detection "
            f"(${first_liq:,.0f} → ${current_liq:,.0f}) — net LP withdrawal"))

    save_exit_state(exit_state)
    return signals


def format_exit_alert(token_key, metrics, signals, scanner_state):
    """Format exit signal alert for Telegram."""
    history = scanner_state.get("seen_tokens", {}).get(token_key, [])
    appearances = len(history)

    # Determine overall severity
    has_critical = any(s[1].startswith("🔴") for s in signals)
    has_warning = any(s[1].startswith("🟠") for s in signals)

    if has_critical:
        header = "🚨 EXIT SIGNAL"
        action = "Consider closing position"
    elif has_warning:
        header = "⚠️ DETERIORATION"
        action = "Monitor closely — tighten stops"
    else:
        header = "👀 WATCH"
        action = "No action needed yet"

    lines = [
        f"<b>{header}: {metrics['symbol']}</b>",
        f"{metrics['name']} | {metrics['chain']}",
        f"",
        f"<b>Current State:</b>",
        f"  💰 Liq: ${metrics['liquidity']:,.0f}",
        f"  📊 Vol: ${metrics['volume_24h']:,.0f} ({metrics['vol_liq_ratio']:.1f}x)",
        f"  📈 6h: {metrics['price_change_6h']:+.1f}%",
        f"  📍 Appearances: {appearances}",
        f"",
        f"<b>Signals:</b>",
    ]

    for signal_type, severity, message in signals:
        lines.append(f"  {severity} {message}")

    lines.extend([
        f"",
        f"💡 <b>{action}</b>",
        f"",
        f"<a href='{metrics.get('dex_url', '')}'>DexScreener</a>",
    ])

    return "\n".join(lines)


def get_active_holdings(scanner_state):
    """
    Get list of tokens that have triggered T1 or T2 alerts 
    and are still being tracked (not yet flagged as dead).
    """
    exit_state = load_exit_state()
    active = []

    for token_key, history in scanner_state.get("seen_tokens", {}).items():
        if len(history) >= 2:
            # Check if not already sent a CRITICAL exit
            sent_critical = exit_state.get("exit_alerts_sent", {}).get(token_key, {}).get("critical", False)
            if not sent_critical:
                latest = history[-1]
                active.append({
                    "token_key": token_key,
                    "symbol": token_key.split(":")[-1][:8],  # approximate
                    "liquidity": latest.get("liquidity", 0),
                    "volume_24h": latest.get("volume_24h", 0),
                    "safety_score": latest.get("safety_score", 0),
                    "appearances": len(history),
                    "first_liq": history[0].get("liquidity", 0),
                })

    return active
