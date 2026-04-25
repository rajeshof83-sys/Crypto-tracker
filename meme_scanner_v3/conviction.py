"""
Conviction Scoring Engine
=========================
Computes a composite 1-10 conviction score for each token alert
based on all data-driven factors discovered from tracker analysis.
"""

from datetime import datetime, timezone

# ============================================================
# SCORING WEIGHTS (calibrated from 304-token backtest)
# ============================================================

def compute_conviction_score(metrics, safety_score, narratives, state, token_key, batch_data):
    """
    Compute composite conviction score (1.0 - 10.0).
    
    Returns (score, breakdown_dict) for transparency in alerts.
    """
    breakdown = {}
    score = 0.0

    liq = metrics["liquidity"]
    vol_liq = metrics["vol_liq_ratio"]
    pc = metrics["price_change_6h"]
    symbol = metrics["symbol"]
    name = metrics["name"]
    is_new = token_key not in state.get("seen_tokens", {})

    # ── 1. LIQUIDITY ZONE (max 1.5 pts) ──
    if 25_000 <= liq <= 75_000:
        breakdown["liq_zone"] = 1.5
    elif 20_000 <= liq <= 100_000:
        breakdown["liq_zone"] = 1.0
    elif 100_000 <= liq <= 200_000:
        breakdown["liq_zone"] = 0.5
    else:
        breakdown["liq_zone"] = 0.0
    score += breakdown["liq_zone"]

    # ── 2. VOL/LIQ SWEET SPOT (max 2.0 pts) ──
    if 10 <= vol_liq <= 30:
        breakdown["vol_liq"] = 2.0   # gold zone: 51% hit >50% gains
    elif 8 <= vol_liq <= 35:
        breakdown["vol_liq"] = 1.5
    elif 3 <= vol_liq <= 50:
        breakdown["vol_liq"] = 0.5
    else:
        breakdown["vol_liq"] = 0.0
    score += breakdown["vol_liq"]

    # ── 3. SAFETY SCORE (max 2.0 pts) ──
    if safety_score == 9:
        breakdown["safety"] = 2.0    # 9 outperforms all: median +143%
    elif safety_score == 10:
        breakdown["safety"] = 1.5    # safe but lower upside
    elif safety_score == 8:
        breakdown["safety"] = 1.0
    elif safety_score == 7:
        breakdown["safety"] = 0.5
    else:
        breakdown["safety"] = 0.0
    score += breakdown["safety"]

    # ── 4. FIRST APPEARANCE BONUS (max 1.0 pt) ──
    if is_new:
        breakdown["freshness"] = 1.0  # one-offs: median +23%, 34% hit 100%+
    else:
        # Check if it's compounding (MAGA pattern)
        history = state.get("seen_tokens", {}).get(token_key, [])
        if len(history) >= 2:
            first_liq = history[0].get("liquidity", 0)
            if first_liq > 0 and liq > first_liq * 2:
                breakdown["freshness"] = 0.8  # strong compounder
            elif first_liq > 0 and liq > first_liq * 1.5:
                breakdown["freshness"] = 0.5
            else:
                breakdown["freshness"] = 0.0
        else:
            breakdown["freshness"] = 0.3
    score += breakdown["freshness"]

    # ── 5. DAY/TIME QUALITY (max 1.0 pt) ──
    now = datetime.now(timezone.utc)
    day = now.weekday()
    hour = now.hour

    day_score = {0: 1.0, 6: 1.0, 4: 0.7, 1: 0.5, 5: 0.5, 2: 0.3, 3: 0.0}
    time_score = 0.3 if 9 <= hour <= 12 else 0.0  # morning UTC boost

    breakdown["timing"] = min(1.0, day_score.get(day, 0.3) + time_score)
    score += breakdown["timing"]

    # ── 6. NAME CHARACTERISTICS (max 0.5 pts) ──
    name_score = 0.0
    if symbol != symbol.upper() or not symbol.isalpha():
        name_score += 0.3  # mixed case outperforms ALL CAPS by 4x
    if 5 <= len(symbol) <= 8:
        name_score += 0.2  # medium-length names: median +26%
    breakdown["name"] = min(0.5, name_score)
    score += breakdown["name"]

    # ── 7. NARRATIVE QUALITY (max 1.0 pt) ──
    narrative_scores = {
        "🐾 Animal/Mascot": 1.0,      # median +92%, 40% >100%
        "🎌 Japanese/Anime": 1.0,      # 100% positive rate
        "🪞 Self-Aware/Meta": 0.8,     # median +53%, 87% positive
        "🤖 Tech/AI": 0.6,
        "🏛 Political": 0.5,           # bifurcated but accelerating
        "🚀 Space": 0.4,
        "🛢 Oil/Energy": 0.2,          # never hits 100%+
        "🎲 Uncategorized": 0.3,
        "💀 Edgy/Shock": 0.1,
    }
    best_narrative = max((narrative_scores.get(n, 0.3) for n in narratives), default=0.3)
    breakdown["narrative"] = best_narrative
    score += breakdown["narrative"]

    # ── 8. BATCH SENTIMENT (max 1.0 pt) ──
    if batch_data:
        positive_count = sum(1 for d in batch_data if d.get("price_change_6h", 0) > 0)
        pos_pct = positive_count / len(batch_data)
        if pos_pct >= 0.75:
            breakdown["batch"] = 1.0
        elif pos_pct >= 0.60:
            breakdown["batch"] = 0.5
        elif pos_pct >= 0.40:
            breakdown["batch"] = 0.2
        else:
            breakdown["batch"] = 0.0
    else:
        breakdown["batch"] = 0.5
    score += breakdown["batch"]

    # Clamp to 1-10
    final_score = max(1.0, min(10.0, score))

    return round(final_score, 1), breakdown


def format_conviction_bar(score):
    """Visual conviction bar for Telegram."""
    filled = int(score)
    empty = 10 - filled
    bar = "█" * filled + "░" * empty
    
    if score >= 8:
        label = "HIGH CONVICTION"
    elif score >= 6:
        label = "MODERATE"
    elif score >= 4:
        label = "LOW"
    else:
        label = "WEAK"

    return f"🎯 {bar} {score}/10 — {label}"


def format_conviction_breakdown(breakdown):
    """Format score breakdown for detailed view."""
    labels = {
        "liq_zone": "💰 Liquidity zone",
        "vol_liq": "📊 Vol/Liq sweet spot",
        "safety": "🛡 Safety score",
        "freshness": "🆕 Freshness/compound",
        "timing": "⏰ Day/time quality",
        "name": "📝 Name characteristics",
        "narrative": "🏷 Narrative quality",
        "batch": "📦 Batch sentiment",
    }
    lines = []
    for key, label in labels.items():
        val = breakdown.get(key, 0)
        if val > 0:
            lines.append(f"  {label}: +{val:.1f}")
    return "\n".join(lines)
