"""
Analytics Engines
=================
- Token Velocity Tracker: measures liquidity growth rate
- Narrative Lifecycle: detects emerging/peak/saturated/dead themes
- Correlation Detector: groups related tokens and warns on cluster risk
- Social Catalyst: lightweight trending check
"""

from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import re
import requests
import os


# ============================================================
# TOKEN VELOCITY TRACKER
# ============================================================

def compute_velocity(history):
    """
    Compute liquidity velocity (% change per day) across appearances.
    Returns (velocity_pct_per_day, trend_label, detail_string).
    """
    if len(history) < 2:
        return 0, "UNKNOWN", "Insufficient data"

    first = history[0]
    last = history[-1]

    first_liq = first.get("liquidity", 0)
    last_liq = last.get("liquidity", 0)

    if first_liq <= 0:
        return 0, "UNKNOWN", "No initial liquidity"

    # Parse timestamps
    try:
        t0 = datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00"))
        days = max((t1 - t0).total_seconds() / 86400, 0.1)
    except (KeyError, ValueError):
        days = len(history)  # fallback: assume ~1 day between appearances

    growth = last_liq / first_liq
    daily_pct = ((growth ** (1 / days)) - 1) * 100

    # Check acceleration (is growth speeding up or slowing?)
    if len(history) >= 3:
        mid = len(history) // 2
        first_half_liqs = [h["liquidity"] for h in history[:mid+1]]
        second_half_liqs = [h["liquidity"] for h in history[mid:]]

        first_half_growth = first_half_liqs[-1] / first_half_liqs[0] if first_half_liqs[0] > 0 else 1
        second_half_growth = second_half_liqs[-1] / second_half_liqs[0] if second_half_liqs[0] > 0 else 1

        if second_half_growth > first_half_growth * 1.2:
            trend = "ACCELERATING 🚀"
        elif second_half_growth < first_half_growth * 0.5:
            trend = "DECELERATING 📉"
        elif growth > 1.5:
            trend = "STEADY GROWTH 📈"
        elif growth > 0.8:
            trend = "FLAT ➡️"
        else:
            trend = "DECLINING 🔻"
    else:
        if growth > 2:
            trend = "STRONG GROWTH 📈"
        elif growth > 1.2:
            trend = "GROWING 📈"
        elif growth > 0.8:
            trend = "FLAT ➡️"
        else:
            trend = "DECLINING 🔻"

    detail = (f"${first_liq:,.0f} → ${last_liq:,.0f} "
              f"({growth:.1f}x in {days:.0f}d, {daily_pct:+.1f}%/day)")

    return daily_pct, trend, detail


def format_velocity(history):
    """Format velocity for Telegram display."""
    vel, trend, detail = compute_velocity(history)
    return f"⚡ Velocity: {trend}\n   {detail}"


# ============================================================
# NARRATIVE LIFECYCLE
# ============================================================

NARRATIVE_KEYWORDS = {
    "🛢 Oil/Energy": ["oil", "petroleum", "gas", "energy", "crude", "reserve", "lng", "brent", "opec"],
    "🏛 Political": ["trump", "maga", "president", "king", "vote", "election", "congress", "biden", "pope"],
    "🎌 Japanese/Anime": ["anime", "manga", "inu", "neko", "doge", "shiba", "chan", "kun", "samurai", "ninja"],
    "🐾 Animal/Mascot": ["cat", "dog", "frog", "bull", "bear", "whale", "otter", "panda", "monkey", "ape", "penguin", "duck", "fox"],
    "🤖 Tech/AI": ["ai", "quantum", "neural", "crypto", "blockchain", "protocol", "defi", "llm", "gpt", "bot", "agent"],
    "🪞 Self-Aware/Meta": ["dumb", "smart", "ruined", "honest", "retard", "rug", "pump", "moon", "cope", "shill", "degen", "larp", "noob", "schizo"],
    "🚀 Space": ["asteroid", "moon", "nasa", "rocket", "mars", "space", "orbit", "star"],
    "💀 Edgy/Shock": ["death", "kill", "drug", "fentanyl", "war", "weapon", "nuke"],
}


def detect_narratives(symbol, name):
    text = f" {symbol} {name} ".lower()
    # Tokenize into words for matching to avoid substring false positives
    words = set(re.findall(r'[a-z]{2,}', text))
    tags = []
    for narrative, keywords in NARRATIVE_KEYWORDS.items():
        if any(kw in words for kw in keywords):
            tags.append(narrative)
    return tags if tags else ["🎲 Uncategorized"]


def compute_narrative_lifecycle(narrative_history):
    """
    Analyze each narrative's lifecycle stage.
    
    narrative_history: dict of {narrative: [list of dates it appeared]}
    
    Returns dict of {narrative: (stage, detail_string)}
    """
    today = datetime.now(timezone.utc).date()
    results = {}

    for narrative, dates in narrative_history.items():
        date_objs = []
        for d in dates:
            try:
                date_objs.append(datetime.strptime(d, "%Y-%m-%d").date())
            except ValueError:
                continue

        if not date_objs:
            results[narrative] = ("💤 DORMANT", "No recent activity")
            continue

        date_objs.sort()
        unique_days = len(set(date_objs))
        span = (date_objs[-1] - date_objs[0]).days + 1
        days_since_last = (today - date_objs[-1]).days
        recent_dates = [d for d in date_objs if (today - d).days <= 7]

        # Lifecycle classification
        if days_since_last > 7:
            stage = "💀 DEAD"
            detail = f"Last seen {days_since_last}d ago. Avoid."
        elif unique_days <= 2 and span <= 3:
            stage = "🌱 EMERGING"
            detail = f"{unique_days} days old. Highest upside, highest risk."
        elif unique_days <= 5 and len(recent_dates) >= 2:
            stage = "🔥 PEAK"
            detail = f"Active {unique_days} days, still generating tokens. Sweet spot."
        elif unique_days > 5 and len(recent_dates) >= 1:
            stage = "🫧 SATURATED"
            detail = f"Running {unique_days}+ days. Copycats flooding in. Take profits."
        elif len(recent_dates) == 0:
            stage = "💤 DORMANT"
            detail = f"No activity in 7 days."
        else:
            stage = "📈 ACTIVE"
            detail = f"{unique_days} days, {len(recent_dates)} appearances this week."

        results[narrative] = (stage, detail)

    return results


# ============================================================
# CORRELATION DETECTOR
# ============================================================

def detect_correlations(batch_data, scanner_state):
    """
    Group tokens by narrative overlap and name similarity.
    Returns list of correlation groups with risk assessment.
    """
    groups = defaultdict(list)

    for token in batch_data:
        narratives = token.get("narratives", [])
        for n in narratives:
            if n != "🎲 Uncategorized":
                groups[n].append(token)

    # Also group by name similarity (simple: shared words)
    name_groups = defaultdict(list)
    for token in batch_data:
        words = set(re.findall(r'[a-zA-Z]{3,}', token.get("name", "").lower()))
        for word in words:
            if word not in ("the", "coin", "token", "solana", "official"):
                name_groups[word].append(token)

    # Merge name groups with narrative groups
    for word, tokens in name_groups.items():
        if len(tokens) >= 2:
            groups[f"🔗 '{word}' family"].extend(tokens)

    # Deduplicate within groups
    cluster_risks = []
    for group_name, tokens in groups.items():
        # Deduplicate by symbol
        seen = set()
        unique_tokens = []
        for t in tokens:
            if t["symbol"] not in seen:
                seen.add(t["symbol"])
                unique_tokens.append(t)

        if len(unique_tokens) >= 2:
            # Check if cluster is declining together
            declining = sum(1 for t in unique_tokens if t.get("price_change_6h", 0) < -5)
            all_declining = declining >= len(unique_tokens) * 0.6

            cluster_risks.append({
                "name": group_name,
                "tokens": unique_tokens,
                "count": len(unique_tokens),
                "declining_count": declining,
                "cluster_risk": all_declining,
                "avg_pc": sum(t.get("price_change_6h", 0) for t in unique_tokens) / len(unique_tokens),
            })

    return sorted(cluster_risks, key=lambda x: -x["count"])


def format_correlation_warnings(cluster_risks):
    """Format correlation warnings for Telegram."""
    warnings = []
    for cluster in cluster_risks:
        if cluster["cluster_risk"] and cluster["count"] >= 2:
            symbols = ", ".join(t["symbol"] for t in cluster["tokens"][:5])
            warnings.append(
                f"⚠️ CLUSTER RISK: {cluster['name']}\n"
                f"   {cluster['declining_count']}/{cluster['count']} tokens declining "
                f"({symbols})\n"
                f"   Avg 6h change: {cluster['avg_pc']:+.1f}% — reduce sector exposure"
            )
    return "\n\n".join(warnings) if warnings else None


# ============================================================
# SOCIAL CATALYST (lightweight)
# ============================================================

def check_social_catalyst(symbol, name):
    """
    Lightweight check for whether token name is trending.
    Uses a simple heuristic: search-engine-friendly names that match
    real-world events or cultural moments get flagged.
    
    Returns (is_trending, catalyst_note).
    """
    # Known catalyst patterns (expand over time)
    catalyst_keywords = {
        "asteroid": "Space/astronomy event",
        "pope": "Religious/political event",
        "trump": "US political cycle",
        "maga": "US political cycle",
        "election": "Election cycle",
        "tariff": "Trade policy",
        "fed": "Monetary policy",
        "war": "Geopolitical tension",
        "earthquake": "Natural disaster",
        "eclipse": "Astronomical event",
        "olympics": "Sports event",
        "worldcup": "Sports event",
        "halving": "Crypto event",
        "etf": "Financial product",
        "cr7": "Celebrity/sports",
        "ghibli": "Entertainment/culture",
    }

    text = f"{symbol} {name}".lower()
    for keyword, catalyst in catalyst_keywords.items():
        if keyword in text:
            return True, f"🔥 Catalyst: {catalyst}"

    return False, None


def check_twitter_mentions(symbol):
    """
    Optional: Check Twitter/X mention volume.
    Requires TWITTER_BEARER_TOKEN env var.
    Returns (mention_count, is_trending).
    """
    bearer = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer:
        return None, False

    try:
        url = "https://api.twitter.com/2/tweets/counts/recent"
        headers = {"Authorization": f"Bearer {bearer}"}
        params = {"query": f"${symbol} crypto", "granularity": "hour"}
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("meta", {}).get("total_tweet_count", 0)
            return total, total > 50  # arbitrary threshold
    except Exception:
        pass

    return None, False
