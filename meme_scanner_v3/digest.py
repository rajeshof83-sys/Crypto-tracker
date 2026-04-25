"""
Smart Digest System
===================
Generates 4 daily intelligence briefings at 6AM, 11AM, 4PM, 9PM IST.
Each digest has a different focus based on time of day.

Schedule (IST → UTC):
  6:00 AM IST = 00:30 UTC  → Morning Brief (overnight recap + day plan)
  11:00 AM IST = 05:30 UTC → Midday Pulse (morning scan results)
  4:00 PM IST = 10:30 UTC  → Afternoon Intel (peak signal window)
  9:00 PM IST = 15:30 UTC  → Evening Wrap (day summary + overnight watchlist)
"""

import json
import os
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

STATE_FILE = "scanner_state.json"
EXIT_STATE_FILE = "exit_state.json"
OUTCOMES_FILE = "outcomes_state.json"
DIGEST_STATE_FILE = "digest_state.json"


def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================
# DIGEST DATA COLLECTORS
# ============================================================

def get_recent_scans(scanner_state, hours=12):
    """Get all token appearances from the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    recent = []

    for token_key, history in scanner_state.get("seen_tokens", {}).items():
        for entry in history:
            if entry.get("timestamp", "") >= cutoff:
                entry["token_key"] = token_key
                recent.append(entry)

    return sorted(recent, key=lambda x: x.get("timestamp", ""), reverse=True)


def get_compounders(scanner_state, min_appearances=3):
    """Find tokens with consistent liquidity growth."""
    compounders = []

    for token_key, history in scanner_state.get("seen_tokens", {}).items():
        if len(history) < min_appearances:
            continue

        first_liq = history[0].get("liquidity", 0)
        last_liq = history[-1].get("liquidity", 0)

        if first_liq <= 0:
            continue

        growth = last_liq / first_liq
        if growth >= 1.5:
            # Check if still healthy (not declining in recent scans)
            recent = history[-3:]
            recent_liqs = [h.get("liquidity", 0) for h in recent]
            is_declining = len(recent_liqs) >= 2 and recent_liqs[-1] < recent_liqs[-2] * 0.85

            compounders.append({
                "token_key": token_key,
                "appearances": len(history),
                "first_liq": first_liq,
                "current_liq": last_liq,
                "growth": growth,
                "safety": history[-1].get("safety_score", 0),
                "is_declining": is_declining,
                "last_timestamp": history[-1].get("timestamp", ""),
            })

    return sorted(compounders, key=lambda x: -x["growth"])


def get_almost_compounders(scanner_state):
    """Tokens close to triggering Tier 2 but not quite there yet."""
    almost = []

    for token_key, history in scanner_state.get("seen_tokens", {}).items():
        if len(history) < 2:
            continue

        first_liq = history[0].get("liquidity", 0)
        last_liq = history[-1].get("liquidity", 0)

        if first_liq <= 0:
            continue

        growth = last_liq / first_liq
        safety = history[-1].get("safety_score", 0)

        # Close to Tier 2 but missing one criterion
        if 1.2 <= growth < 1.5 and safety >= 7:
            almost.append({
                "token_key": token_key,
                "appearances": len(history),
                "growth": growth,
                "safety": safety,
                "current_liq": last_liq,
                "needed": f"Needs {1.5/growth:.0f}% more liq growth" if growth < 1.5 else "Needs safety ≥8",
            })

    return sorted(almost, key=lambda x: -x["growth"])[:5]


def get_win_rate_stats(outcomes_state):
    """Compute win rate statistics from outcome data."""
    alerts = outcomes_state.get("tracked_alerts", {})
    completed = {k: v for k, v in alerts.items() if v.get("completed")}

    if len(completed) < 3:
        return None

    stats = {
        "total": len(completed),
        "by_label": Counter(v["outcome_label"] for v in completed.values()),
        "by_tier": defaultdict(lambda: Counter()),
    }

    for k, v in completed.items():
        tier = v.get("tier", "unknown")
        label = v.get("outcome_label", "unknown")
        stats["by_tier"][tier][label] += 1

    # Compute win rates
    moon_winner = stats["by_label"].get("moon", 0) + stats["by_label"].get("winner", 0)
    stats["overall_win_rate"] = moon_winner / stats["total"] if stats["total"] > 0 else 0
    stats["rug_rate"] = stats["by_label"].get("rug", 0) / stats["total"] if stats["total"] > 0 else 0

    # Self-diagnosis
    if stats["rug_rate"] > 0.15:
        stats["diagnosis"] = "⚠️ Rug rate above 15% — consider tightening safety threshold"
    elif stats["overall_win_rate"] < 0.25:
        stats["diagnosis"] = "⚠️ Win rate below 25% — filter may be too loose"
    elif stats["overall_win_rate"] > 0.60:
        stats["diagnosis"] = "✅ Strong performance — filter is well-calibrated"
    else:
        stats["diagnosis"] = "📊 Normal performance — continue collecting data"

    return stats


# ============================================================
# DIGEST FORMATTERS
# ============================================================

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_QUALITY = {
    0: "🟢 PRIME", 1: "🟡 GOOD", 2: "🟡 AVG",
    3: "🔴 WEAK", 4: "🟡 GOOD", 5: "🟡 AVG", 6: "🟢 PRIME",
}


def format_morning_brief(scanner_state, outcomes_state):
    """
    6 AM IST — Morning Brief
    Focus: What happened overnight + what to watch today
    """
    now_utc = datetime.now(timezone.utc)
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = now_utc + ist_offset
    day_name = DAY_NAMES[now_ist.weekday()]
    day_quality = DAY_QUALITY[now_ist.weekday()]

    recent = get_recent_scans(scanner_state, hours=12)
    compounders = get_compounders(scanner_state)
    almost = get_almost_compounders(scanner_state)
    win_stats = get_win_rate_stats(outcomes_state)

    # Overnight summary
    overnight_positive = sum(1 for r in recent if r.get("price_change_6h", 0) > 0)
    overnight_total = len(recent) or 1

    lines = [
        f"☀️ <b>MORNING BRIEF</b>",
        f"📅 {day_name}, {now_ist.strftime('%d %b %Y')} | {day_quality}",
        f"",
        f"<b>═══ OVERNIGHT RECAP ═══</b>",
        f"📦 {len(recent)} tokens scanned (last 12h)",
        f"📊 {overnight_positive}/{overnight_total} positive ({100*overnight_positive/overnight_total:.0f}%)",
    ]

    # New tokens detected overnight
    new_overnight = [r for r in recent if len(scanner_state.get("seen_tokens", {}).get(r.get("token_key", ""), [])) == 1]
    if new_overnight:
        lines.append(f"🆕 {len(new_overnight)} new tokens detected")

    # Active compounders status
    lines.append(f"\n<b>═══ COMPOUNDER STATUS ═══</b>")
    if compounders:
        healthy = [c for c in compounders if not c["is_declining"]]
        declining = [c for c in compounders if c["is_declining"]]

        for c in healthy[:5]:
            key_short = c["token_key"].split(":")[-1][:12]
            lines.append(
                f"  ✅ {key_short}… — {c['growth']:.1f}x growth, "
                f"${c['current_liq']:,.0f} liq, safety {c['safety']}"
            )
        for c in declining[:3]:
            key_short = c["token_key"].split(":")[-1][:12]
            lines.append(
                f"  ⚠️ {key_short}… — {c['growth']:.1f}x but DECLINING")
    else:
        lines.append("  No active compounders")

    # Almost compounders (watchlist)
    if almost:
        lines.append(f"\n<b>═══ WATCHLIST ═══</b>")
        lines.append(f"Tokens close to Tier 2 trigger:")
        for a in almost[:4]:
            key_short = a["token_key"].split(":")[-1][:12]
            lines.append(
                f"  👀 {key_short}… — {a['growth']:.1f}x growth, {a['needed']}")

    # Day outlook
    lines.extend([
        f"\n<b>═══ TODAY'S OUTLOOK ═══</b>",
        f"📅 {day_name} historical: {day_quality}",
    ])

    if now_ist.weekday() in (0, 6):
        lines.append("💡 Prime day — scans during 2:30-5:30 PM IST are highest signal")
    elif now_ist.weekday() == 3:
        lines.append("💡 Historically weak day — be selective, raise conviction threshold to 7+")
    else:
        lines.append("💡 Standard day — follow the filter normally")

    # Win rate tracker
    if win_stats:
        lines.extend([
            f"\n<b>═══ SYSTEM PERFORMANCE ═══</b>",
            f"📊 {win_stats['total']} outcomes tracked",
            f"🏆 Win rate: {win_stats['overall_win_rate']*100:.0f}% (moon+winner)",
            f"💀 Rug rate: {win_stats['rug_rate']*100:.0f}%",
            f"{win_stats['diagnosis']}",
        ])

    return "\n".join(lines)


def format_midday_pulse(scanner_state):
    """
    11 AM IST — Midday Pulse
    Focus: Morning scan results + emerging narratives
    """
    now_utc = datetime.now(timezone.utc)
    recent = get_recent_scans(scanner_state, hours=6)

    from analytics import compute_narrative_lifecycle

    narrative_history = scanner_state.get("narrative_history", {})
    lifecycle = compute_narrative_lifecycle(narrative_history)

    lines = [
        f"🕚 <b>MIDDAY PULSE</b>",
        f"⏰ {now_utc.strftime('%H:%M UTC')}",
        f"",
        f"<b>═══ MORNING RESULTS ═══</b>",
        f"📦 {len(recent)} tokens in last 6h",
    ]

    if recent:
        pcs = [r.get("price_change_6h", 0) for r in recent]
        pos = sum(1 for p in pcs if p > 0)
        big_movers = [r for r in recent if abs(r.get("price_change_6h", 0)) > 100]

        lines.append(f"📊 {pos}/{len(recent)} positive")

        if big_movers:
            lines.append(f"\n<b>Big movers:</b>")
            for m in sorted(big_movers, key=lambda x: -x.get("price_change_6h", 0))[:5]:
                key_short = m.get("token_key", "???").split(":")[-1][:12]
                lines.append(
                    f"  {'🟩' if m.get('price_change_6h', 0) > 0 else '🟥'} "
                    f"{key_short}… {m.get('price_change_6h', 0):+.0f}% "
                    f"(${m.get('liquidity', 0):,.0f} liq)")

    # Narrative lifecycle
    lines.append(f"\n<b>═══ NARRATIVE LIFECYCLE ═══</b>")
    for narrative, (stage, detail) in sorted(lifecycle.items(), key=lambda x: x[1][0]):
        if stage not in ("💤 DORMANT", "💀 DEAD"):
            lines.append(f"  {narrative}")
            lines.append(f"    {stage} — {detail}")

    # Emerging narratives get extra emphasis
    emerging = [n for n, (s, _) in lifecycle.items() if s == "🌱 EMERGING"]
    if emerging:
        lines.append(f"\n💡 <b>New narratives appearing:</b> {', '.join(emerging)}")
        lines.append(f"   Early movers in new themes have highest upside")

    saturated = [n for n, (s, _) in lifecycle.items() if s == "🫧 SATURATED"]
    if saturated:
        lines.append(f"\n⚠️ <b>Saturated themes:</b> {', '.join(saturated)}")
        lines.append(f"   Consider taking profits on these narratives")

    return "\n".join(lines)


def format_afternoon_intel(scanner_state):
    """
    4 PM IST — Afternoon Intel
    Focus: Peak signal window analysis + correlation risks
    """
    now_utc = datetime.now(timezone.utc)
    recent = get_recent_scans(scanner_state, hours=6)
    compounders = get_compounders(scanner_state)

    from analytics import detect_correlations, format_correlation_warnings, compute_velocity

    lines = [
        f"🌆 <b>AFTERNOON INTEL</b>",
        f"⏰ {now_utc.strftime('%H:%M UTC')} — Peak signal window",
        f"",
    ]

    # Velocity leaderboard
    lines.append(f"<b>═══ VELOCITY LEADERBOARD ═══</b>")
    velocities = []
    for token_key, history in scanner_state.get("seen_tokens", {}).items():
        if len(history) >= 3:
            vel, trend, detail = compute_velocity(history)
            if vel > 5:  # only show meaningful velocity
                velocities.append((token_key, vel, trend, detail, history[-1]))

    velocities.sort(key=lambda x: -x[1])
    if velocities:
        for tk, vel, trend, detail, latest in velocities[:6]:
            key_short = tk.split(":")[-1][:12]
            lines.append(f"  {trend}")
            lines.append(f"    {key_short}… — {detail}")
            lines.append(f"    Safety: {latest.get('safety_score', '?')}/10")
    else:
        lines.append("  No tokens with significant velocity yet")

    # Correlation risk check
    if recent:
        batch_data = []
        for r in recent:
            from analytics import detect_narratives
            r["narratives"] = detect_narratives(
                r.get("token_key", "").split(":")[-1][:8],
                r.get("token_key", "")
            )
            r["symbol"] = r.get("token_key", "").split(":")[-1][:8]
            r["name"] = r.get("token_key", "")
            batch_data.append(r)

        correlations = detect_correlations(batch_data, scanner_state)
        warnings = format_correlation_warnings(correlations)

        if warnings:
            lines.append(f"\n<b>═══ CORRELATION RISK ═══</b>")
            lines.append(warnings)
        else:
            lines.append(f"\n<b>═══ CORRELATION RISK ═══</b>")
            lines.append("  ✅ No correlated clusters declining together")

    # Liquidity flow summary
    total_liq_recent = sum(r.get("liquidity", 0) for r in recent)
    lines.extend([
        f"\n<b>═══ LIQUIDITY FLOW ═══</b>",
        f"💰 Total liq across {len(recent)} tokens: ${total_liq_recent:,.0f}",
    ])

    if compounders:
        compounder_liq = sum(c["current_liq"] for c in compounders[:10])
        lines.append(f"📈 Top 10 compounder liq: ${compounder_liq:,.0f}")

    return "\n".join(lines)


def format_evening_wrap(scanner_state, outcomes_state):
    """
    9 PM IST — Evening Wrap
    Focus: Full day summary + overnight watchlist + self-diagnostics
    """
    now_utc = datetime.now(timezone.utc)
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = now_utc + ist_offset
    tomorrow = now_ist + timedelta(days=1)
    tomorrow_quality = DAY_QUALITY[tomorrow.weekday()]
    tomorrow_name = DAY_NAMES[tomorrow.weekday()]

    day_scans = get_recent_scans(scanner_state, hours=24)
    compounders = get_compounders(scanner_state)
    win_stats = get_win_rate_stats(outcomes_state)

    lines = [
        f"🌙 <b>EVENING WRAP</b>",
        f"📅 {now_ist.strftime('%d %b %Y')}",
        f"",
        f"<b>═══ TODAY'S SCORECARD ═══</b>",
    ]

    if day_scans:
        total = len(day_scans)
        positive = sum(1 for r in day_scans if r.get("price_change_6h", 0) > 0)
        pcs = [r.get("price_change_6h", 0) for r in day_scans if abs(r.get("price_change_6h", 0)) < 10000]
        med_pc = sorted(pcs)[len(pcs)//2] if pcs else 0

        lines.extend([
            f"📦 Tokens scanned: {total}",
            f"📊 Positive: {positive}/{total} ({100*positive/total:.0f}%)",
            f"📈 Median 6h change: {med_pc:+.1f}%",
        ])

        # Best and worst
        sorted_by_pc = sorted(day_scans, key=lambda x: x.get("price_change_6h", 0), reverse=True)
        if sorted_by_pc:
            best = sorted_by_pc[0]
            worst = sorted_by_pc[-1]
            lines.append(f"\n🏆 Best: {best.get('token_key', '???').split(':')[-1][:12]}… "
                        f"({best.get('price_change_6h', 0):+.0f}%)")
            if worst.get("price_change_6h", 0) < -20:
                lines.append(f"💀 Worst: {worst.get('token_key', '???').split(':')[-1][:12]}… "
                            f"({worst.get('price_change_6h', 0):+.0f}%)")

    # Compounder health check
    lines.append(f"\n<b>═══ PORTFOLIO HEALTH ═══</b>")
    if compounders:
        healthy = [c for c in compounders if not c["is_declining"]]
        declining = [c for c in compounders if c["is_declining"]]
        lines.append(f"✅ Healthy compounders: {len(healthy)}")
        lines.append(f"⚠️ Declining compounders: {len(declining)}")

        for c in declining[:3]:
            key_short = c["token_key"].split(":")[-1][:12]
            lines.append(f"  🟠 {key_short}… — was {c['growth']:.1f}x, now declining")
    else:
        lines.append("  No compounders being tracked")

    # Tomorrow's outlook
    lines.extend([
        f"\n<b>═══ TOMORROW ═══</b>",
        f"📅 {tomorrow_name}: {tomorrow_quality}",
    ])
    if tomorrow.weekday() in (0, 6):
        lines.append("💡 Prime day — increase scan attention")
    elif tomorrow.weekday() == 3:
        lines.append("💡 Weak day — be selective")

    # Win rate + self-diagnostics
    if win_stats:
        lines.extend([
            f"\n<b>═══ SYSTEM HEALTH ═══</b>",
            f"📊 Outcomes tracked: {win_stats['total']}",
            f"🏆 Win rate: {win_stats['overall_win_rate']*100:.0f}%",
            f"💀 Rug rate: {win_stats['rug_rate']*100:.0f}%",
        ])

        # Per-tier breakdown
        for tier in ["T1", "T2"]:
            tier_counts = win_stats["by_tier"].get(tier, {})
            tier_total = sum(tier_counts.values())
            if tier_total > 0:
                tier_wins = tier_counts.get("moon", 0) + tier_counts.get("winner", 0)
                lines.append(f"  {tier}: {tier_wins}/{tier_total} wins "
                            f"({100*tier_wins/tier_total:.0f}%)")

        lines.append(f"\n{win_stats['diagnosis']}")

        # Threshold adjustment suggestions
        if win_stats["rug_rate"] > 0.15:
            lines.append("🔧 Suggestion: Raise T1_SAFETY_MIN from 8 to 9")
        if win_stats["overall_win_rate"] < 0.20:
            lines.append("🔧 Suggestion: Tighten T1_VOL_LIQ range to 10-25x")

    # Overnight watchlist
    almost = get_almost_compounders(scanner_state)
    if almost:
        lines.append(f"\n<b>═══ OVERNIGHT WATCHLIST ═══</b>")
        for a in almost[:4]:
            key_short = a["token_key"].split(":")[-1][:12]
            lines.append(f"  👀 {key_short}… — {a['growth']:.1f}x, {a['needed']}")

    return "\n".join(lines)


# ============================================================
# DIGEST DISPATCHER
# ============================================================

def get_digest_type():
    """
    Determine which digest to send based on current IST time.
    Returns digest type or None if not a digest time.
    
    Schedule:
      6:00 AM IST (00:30 UTC) → morning
      11:00 AM IST (05:30 UTC) → midday
      4:00 PM IST (10:30 UTC) → afternoon
      9:00 PM IST (15:30 UTC) → evening
    """
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    minute = now_utc.minute

    # Allow 15-minute window around each scheduled time
    schedules = {
        "morning": (0, 30),     # 00:30 UTC = 6:00 AM IST
        "midday": (5, 30),      # 05:30 UTC = 11:00 AM IST
        "afternoon": (10, 30),  # 10:30 UTC = 4:00 PM IST
        "evening": (15, 30),    # 15:30 UTC = 9:00 PM IST
    }

    for digest_type, (sched_hour, sched_min) in schedules.items():
        if hour == sched_hour and abs(minute - sched_min) <= 15:
            return digest_type

    return None


def generate_digest(digest_type):
    """Generate the appropriate digest message."""
    scanner_state = load_json(STATE_FILE)
    outcomes_state = load_json(OUTCOMES_FILE)

    if digest_type == "morning":
        return format_morning_brief(scanner_state, outcomes_state)
    elif digest_type == "midday":
        return format_midday_pulse(scanner_state)
    elif digest_type == "afternoon":
        return format_afternoon_intel(scanner_state)
    elif digest_type == "evening":
        return format_evening_wrap(scanner_state, outcomes_state)
    else:
        return None


def should_send_digest(digest_type):
    """Check if this digest was already sent today."""
    state = load_json(DIGEST_STATE_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent_key = f"{today}_{digest_type}"
    return sent_key not in state.get("sent_digests", [])


def mark_digest_sent(digest_type):
    """Record that this digest was sent."""
    state = load_json(DIGEST_STATE_FILE)
    if "sent_digests" not in state:
        state["sent_digests"] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state["sent_digests"].append(f"{today}_{digest_type}")
    # Keep last 30 days
    state["sent_digests"] = state["sent_digests"][-120:]
    save_json(DIGEST_STATE_FILE, state)


if __name__ == "__main__":
    # Test: generate all digests
    for dt in ["morning", "midday", "afternoon", "evening"]:
        print(f"\n{'='*60}")
        print(f"DIGEST: {dt.upper()}")
        print(f"{'='*60}")
        msg = generate_digest(dt)
        if msg:
            print(msg)
