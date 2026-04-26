"""
Meme Coin Signal Scanner v3
============================
Full-featured hourly scanner with:
- Two-tier unified filter (Fresh Signal + Compounder)
- Conviction scoring (1-10 composite)
- Exit signal monitoring
- Narrative tagging + lifecycle
- Velocity tracking
- Correlation risk warnings
- Social catalyst detection
- Smart digest system (4x daily)
- Win rate self-diagnostics
"""

import os
import json
import time
import requests
from datetime import datetime, timezone
from collections import Counter, defaultdict

# Local modules
from conviction import compute_conviction_score, format_conviction_bar, format_conviction_breakdown
from exit_signals import check_exit_signals, format_exit_alert, load_exit_state, save_exit_state
from analytics import (
    detect_narratives, compute_velocity, format_velocity,
    compute_narrative_lifecycle, detect_correlations,
    format_correlation_warnings, check_social_catalyst
)
from digest import get_digest_type, generate_digest, should_send_digest, mark_digest_sent

# ============================================================
# CONFIGURATION
# ============================================================

DEXSCREENER_BOOST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SUPPORTED_CHAINS = ["solana", "bsc", "base", "ethereum"]
STATE_FILE = "scanner_state.json"

# Tier 1: Fresh Signal
T1_LIQ_MIN, T1_LIQ_MAX = 20_000, 75_000
T1_VOL_LIQ_MIN, T1_VOL_LIQ_MAX = 8, 30
T1_SAFETY_MIN, T1_SAFETY_MAX = 8, 9
T1_PRICE_CHANGE_MIN = -10

# Tier 2: Compounder
T2_LIQ_MULTIPLE_MIN = 1.5
T2_VOL_LIQ_MIN, T2_VOL_LIQ_MAX = 3, 15
T2_SAFETY_MIN = 8

# Anti-filter
ANTI_SAFETY_MAX = 6
ANTI_VOL_LIQ_MAX = 50
ANTI_LIQ_MIN = 15_000
ANTI_PRICE_DUMP = -30
ANTI_LIQ_DECLINE = 0.7

# Dedup guard: minimum minutes between hourly scans.
# Set to 50 so 4 staggered crons (15 min apart) only let ONE run per hour,
# while still tolerating ~5 min of GitHub Actions cron jitter.
MIN_MINUTES_BETWEEN_SCANS = 50


# ============================================================
# STATE
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_tokens": {}, "last_scan": None, "narrative_history": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_token_key(chain, pair_address):
    return f"{chain}:{pair_address}"


def should_run_hourly_scan(state, min_minutes=MIN_MINUTES_BETWEEN_SCANS):
    """
    Returns (should_run: bool, minutes_since_last: float|None).

    Prevents the 4 staggered hourly crons from each running a full scan.
    Uses state['last_scan'] which is already set at the end of every
    successful scan, so no new state field is needed.

    Returns True if last_scan is missing, malformed, or older than min_minutes.
    Digest runs bypass this check (handled in run_scan, not here).
    """
    last_scan_iso = state.get("last_scan")
    if not last_scan_iso:
        return True, None
    try:
        last_scan = datetime.fromisoformat(last_scan_iso)
        # Tolerate naive datetimes from older state files
        if last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60
        return (minutes_since >= min_minutes), minutes_since
    except (ValueError, TypeError) as e:
        print(f"[WARN] Could not parse last_scan timestamp: {e} — running anyway")
        return True, None


# ============================================================
# DEXSCREENER API
# ============================================================

def fetch_boosted_tokens():
    try:
        resp = requests.get(DEXSCREENER_BOOST_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ERROR] Boost fetch: {e}")
        return []

def fetch_token_pairs(chain_id, token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])
        chain_pairs = [p for p in pairs if p.get("chainId") == chain_id]
        if chain_pairs:
            return max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    except Exception as e:
        print(f"[ERROR] Token fetch {token_address}: {e}")
    return None

def extract_metrics(pair_data):
    if not pair_data:
        return None
    try:
        liq = float(pair_data.get("liquidity", {}).get("usd", 0) or 0)
        vol = float(pair_data.get("volume", {}).get("h24", 0) or 0)
        pc = float(pair_data.get("priceChange", {}).get("h6", 0) or 0)
        txns = pair_data.get("txns", {})
        base = pair_data.get("baseToken", {})
        return {
            "symbol": base.get("symbol", "???"),
            "name": base.get("name", "Unknown"),
            "chain": pair_data.get("chainId", ""),
            "pair_address": pair_data.get("pairAddress", ""),
            "liquidity": liq,
            "volume_24h": vol,
            "vol_liq_ratio": vol / liq if liq > 0 else 0,
            "price_change_6h": pc,
            "price_usd": float(pair_data.get("priceUsd", 0) or 0),
            "buys_1h": int(txns.get("h1", {}).get("buys", 0) or 0),
            "sells_1h": int(txns.get("h1", {}).get("sells", 0) or 0),
            "buys_24h": int(txns.get("h24", {}).get("buys", 0) or 0),
            "sells_24h": int(txns.get("h24", {}).get("sells", 0) or 0),
            "fdv": float(pair_data.get("fdv", 0) or 0),
            "dex_url": pair_data.get("url", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"[ERROR] Extract: {e}")
        return None


def compute_safety(pair_data):
    score = 5
    if not pair_data: return score
    info = pair_data.get("info", {})
    liq = float(pair_data.get("liquidity", {}).get("usd", 0) or 0)
    fdv = float(pair_data.get("fdv", 0) or 0)
    if liq > 50_000: score += 1
    if liq > 100_000: score += 1
    if info:
        if info.get("socials"): score += 1
        if info.get("websites"): score += 1
    if fdv > 0 and liq > 0:
        r = fdv / liq
        if r < 100: score += 1
        if r > 1000: score -= 1
    txns = pair_data.get("txns", {})
    b = int(txns.get("h24", {}).get("buys", 0) or 0)
    s = int(txns.get("h24", {}).get("sells", 0) or 0)
    if b > 0 and s > 0 and 0.5 <= b/s <= 2.0: score += 1
    return min(10, max(1, score))


# ============================================================
# FILTER
# ============================================================

def classify(metrics, safety, state, token_key):
    # Anti-filter
    if safety <= ANTI_SAFETY_MAX: return "SKIP", "safety"
    if metrics["vol_liq_ratio"] > ANTI_VOL_LIQ_MAX: return "SKIP", "vol_extreme"
    if metrics["liquidity"] < ANTI_LIQ_MIN: return "SKIP", "liq_low"
    if metrics["price_change_6h"] < ANTI_PRICE_DUMP: return "SKIP", "dumping"
    if token_key in state["seen_tokens"]:
        h = state["seen_tokens"][token_key]
        if h and h[-1].get("liquidity", 0) > 0:
            if metrics["liquidity"] < h[-1]["liquidity"] * ANTI_LIQ_DECLINE:
                return "SKIP", "liq_decline"

    liq, vl, pc = metrics["liquidity"], metrics["vol_liq_ratio"], metrics["price_change_6h"]
    is_new = token_key not in state["seen_tokens"]

    if is_new:
        if (T1_LIQ_MIN <= liq <= T1_LIQ_MAX and T1_VOL_LIQ_MIN <= vl <= T1_VOL_LIQ_MAX
            and T1_SAFETY_MIN <= safety <= T1_SAFETY_MAX and pc > T1_PRICE_CHANGE_MIN):
            return "T1", "fresh_signal"
    else:
        h = state["seen_tokens"][token_key]
        if h:
            fl = h[0].get("liquidity", 0)
            ps = h[-1].get("safety_score", 0)
            lm = liq / fl if fl > 0 else 0
            if lm >= T2_LIQ_MULTIPLE_MIN and safety >= ps and safety >= T2_SAFETY_MIN and T2_VOL_LIQ_MIN <= vl <= T2_VOL_LIQ_MAX:
                return "T2", f"compounder_{lm:.1f}x"
    return None, None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        print(text)
        print("---")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        ).raise_for_status()
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


# ============================================================
# ALERT FORMATTERS
# ============================================================

def format_token_alert(metrics, tier, reason, safety, narratives, conviction, breakdown, state, token_key):
    emoji = "🟢" if tier == "T1" else "🔵"
    label = "FRESH SIGNAL" if tier == "T1" else "COMPOUNDER"

    # Catalyst check
    is_catalyst, catalyst_note = check_social_catalyst(metrics["symbol"], metrics["name"])

    lines = [
        f"{emoji} <b>[{label}] {metrics['symbol']}</b>",
        f"<b>{metrics['name']}</b> | {metrics['chain']}",
        f"",
        format_conviction_bar(conviction),
    ]

    if is_catalyst:
        lines.append(catalyst_note)

    lines.extend([
        f"",
        f"💰 Liq: ${metrics['liquidity']:,.0f}",
        f"📊 Vol: ${metrics['volume_24h']:,.0f} ({metrics['vol_liq_ratio']:.1f}x)",
        f"📈 6h: {metrics['price_change_6h']:+.1f}%",
        f"🛡 Safety: {safety}/10",
        f"💲 Price: ${metrics['price_usd']:.8f}",
        f"🔄 Txns 1h: {metrics['buys_1h']}B / {metrics['sells_1h']}S",
        f"🏷 {' | '.join(narratives)}",
    ])

    # Velocity for returning tokens
    history = state.get("seen_tokens", {}).get(token_key, [])
    if len(history) >= 2:
        lines.append(f"\n{format_velocity(history)}")

    # Compounder details
    if tier == "T2" and history:
        first = history[0]
        lm = metrics['liquidity'] / first["liquidity"] if first["liquidity"] > 0 else 0
        sp = " → ".join(str(h["safety_score"]) for h in history) + f" → {safety}"
        lines.extend([
            f"\n📜 <b>History ({len(history)+1} appearances)</b>",
            f"   Liq: ${first['liquidity']:,.0f} → ${metrics['liquidity']:,.0f} ({lm:.1f}x)",
            f"   Safety: {sp}",
        ])

    # Score breakdown
    lines.extend([
        f"\n<b>Score breakdown:</b>",
        format_conviction_breakdown(breakdown),
        f"\n<a href='{metrics['dex_url']}'>DexScreener</a>",
    ])

    return "\n".join(lines)


def format_batch_summary(batch_data, alerts, exit_alerts, cluster_warnings, state):
    now = datetime.now(timezone.utc)
    day_q = {0:"🟢 Mon",1:"🟡 Tue",2:"🟡 Wed",3:"🔴 Thu",4:"🟡 Fri",5:"🟡 Sat",6:"🟢 Sun"}
    total = len(batch_data)
    if total == 0:
        return "📋 <b>Scan Summary</b>\nNo boosted tokens found."

    pos = sum(1 for d in batch_data if d.get("price_change_6h", 0) > 0)
    pcs = [d["price_change_6h"] for d in batch_data if abs(d["price_change_6h"]) < 10000]
    med = sorted(pcs)[len(pcs)//2] if pcs else 0
    pos_pct = 100 * pos / total

    mood = "🟢 BULLISH" if pos_pct >= 75 else "🟡 MIXED" if pos_pct >= 50 else "🔴 BEARISH"
    mood_note = {
        "🟢 BULLISH": "Strong batch — high-conviction scan",
        "🟡 MIXED": "Neutral — filter carefully",
        "🔴 BEARISH": "Weak batch — consider sitting out",
    }[mood]

    narr_counts = Counter()
    for d in batch_data:
        narr_counts.update(d.get("narratives", []))

    lines = [
        f"📋 <b>HOURLY SUMMARY</b>",
        f"⏰ {now.strftime('%H:%M UTC')} | {day_q.get(now.weekday(), '')}",
        f"",
        f"{mood} — {pos}↑/{total-pos}↓ ({pos_pct:.0f}%) | median {med:+.1f}%",
        f"💡 {mood_note}",
    ]

    # Alerts
    if alerts:
        lines.append(f"\n<b>ALERTS:</b>")
        for m, t, r, s, n, c, _ in alerts:
            e = "🟢" if t == "T1" else "🔵"
            lines.append(f"  {e} {m['symbol']} — 🎯{c}/10 | ${m['liquidity']:,.0f} | {m['price_change_6h']:+.0f}%")

    # Exit signals
    if exit_alerts:
        lines.append(f"\n<b>EXIT SIGNALS:</b>")
        for symbol, signals in exit_alerts:
            worst = max(signals, key=lambda s: 1 if "CRITICAL" in s[1] else 0)
            lines.append(f"  {worst[1]} {symbol}: {worst[2][:60]}")

    # Cluster warnings
    if cluster_warnings:
        lines.append(f"\n{cluster_warnings[:200]}")

    # Narratives
    top_narr = narr_counts.most_common(4)
    if top_narr:
        lines.append(f"\n<b>NARRATIVES:</b> {' | '.join(f'{n}({c})' for n,c in top_narr)}")

    # Top movers
    sorted_batch = sorted(batch_data, key=lambda d: d.get("price_change_6h", 0), reverse=True)
    if sorted_batch:
        best = sorted_batch[0]
        lines.append(f"\n🟩 Top: {best['symbol']} {best.get('price_change_6h',0):+.0f}%")
        if sorted_batch[-1].get("price_change_6h", 0) < -20:
            worst = sorted_batch[-1]
            lines.append(f"🟥 Bot: {worst['symbol']} {worst.get('price_change_6h',0):+.0f}%")

    return "\n".join(lines)


# ============================================================
# SHEETS LOGGING
# ============================================================

def log_to_sheets(metrics, tier, reason, safety, narratives, conviction):
    creds_b64 = os.environ.get("GSHEET_CREDS_JSON")
    sheet_id = os.environ.get("GSHEET_SHEET_ID")
    if not creds_b64 or not sheet_id: return
    try:
        import base64, gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(base64.b64decode(creds_b64)),
            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        )
        ws = gspread.authorize(creds).open_by_key(sheet_id).sheet1
        ws.append_row([
            metrics["timestamp"], tier, reason, metrics["symbol"], metrics["name"],
            metrics["chain"], metrics["pair_address"], metrics["liquidity"],
            metrics["volume_24h"], metrics["vol_liq_ratio"], metrics["price_change_6h"],
            safety, conviction, metrics["price_usd"], metrics["buys_1h"], metrics["sells_1h"],
            metrics["buys_24h"], metrics["sells_24h"], metrics["dex_url"],
            " | ".join(narratives), "", "", "", "", "",
        ], value_input_option="RAW")
    except Exception as e:
        print(f"[ERROR] Sheets: {e}")


# ============================================================
# MAIN
# ============================================================

def run_scan():
    print(f"\n{'='*60}")
    print(f"SCAN v3: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")

    state = load_state()

    # ── Check if digest is due (digests bypass dedup guard) ──
    digest_type = get_digest_type()
    digest_was_sent = False
    if digest_type and should_send_digest(digest_type):
        print(f"[DIGEST] Generating {digest_type} digest")
        digest_msg = generate_digest(digest_type)
        if digest_msg:
            send_telegram(digest_msg)
            mark_digest_sent(digest_type)
            digest_was_sent = True

    # ── Dedup guard for staggered hourly crons ──
    # The workflow fires 4x/hour for reliability against GitHub cron jitter.
    # Only the first one within each hour-window should run a full scan.
    # If a digest just sent, we still skip the scan portion to avoid
    # double-running (digest already informed the user).
    should_run, minutes_since = should_run_hourly_scan(state)
    if not should_run:
        if minutes_since is not None:
            print(f"⏭ Skipping scan — last scan was {minutes_since:.1f} min ago "
                  f"(threshold: {MIN_MINUTES_BETWEEN_SCANS} min)")
        else:
            print(f"⏭ Skipping scan — within dedup window")
        # Don't save state here — we made no changes
        return

    # ── Fetch and process tokens ──
    boosted = fetch_boosted_tokens()
    print(f"Fetched {len(boosted)} boosts")

    if not boosted:
        summary = format_batch_summary([], [], [], None, state)
        send_telegram(summary)
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    batch_data = []
    alerts = []       # (metrics, tier, reason, safety, narratives, conviction, breakdown)
    exit_alerts = []   # (symbol, signals)
    skipped = 0

    for boost in boosted:
        chain_id = boost.get("chainId", "")
        token_address = boost.get("tokenAddress", "")
        if chain_id not in SUPPORTED_CHAINS:
            continue

        time.sleep(0.25)
        pair_data = fetch_token_pairs(chain_id, token_address)
        if not pair_data:
            continue

        pair_address = pair_data.get("pairAddress", "")
        token_key = get_token_key(chain_id, pair_address)
        metrics = extract_metrics(pair_data)
        if not metrics:
            continue

        safety = compute_safety(pair_data)
        narratives = detect_narratives(metrics["symbol"], metrics["name"])

        metrics["safety_score"] = safety
        metrics["narratives"] = narratives
        metrics["is_returning"] = token_key in state["seen_tokens"]
        metrics["token_key"] = token_key

        batch_data.append(metrics)

        # ── Classify ──
        tier, reason = classify(metrics, safety, state, token_key)

        if tier == "SKIP":
            skipped += 1
        elif tier in ("T1", "T2"):
            conviction, breakdown = compute_conviction_score(
                metrics, safety, narratives, state, token_key, batch_data)
            alerts.append((metrics, tier, reason, safety, narratives, conviction, breakdown))
            print(f"  ALERT [{tier}] {metrics['symbol']} — conviction {conviction}/10")

        # ── Exit signal check for returning tokens ──
        if metrics["is_returning"]:
            signals = check_exit_signals(token_key, metrics, safety, state)
            if signals:
                exit_alerts.append((metrics["symbol"], signals))
                print(f"  EXIT SIGNAL: {metrics['symbol']} — {len(signals)} signals")

        # ── Update state ──
        if token_key not in state["seen_tokens"]:
            state["seen_tokens"][token_key] = []
        state["seen_tokens"][token_key].append({
            "timestamp": metrics["timestamp"],
            "liquidity": metrics["liquidity"],
            "volume_24h": metrics["volume_24h"],
            "vol_liq_ratio": metrics["vol_liq_ratio"],
            "price_change_6h": metrics["price_change_6h"],
            "safety_score": safety,
            "price_usd": metrics["price_usd"],
            "buys_24h": metrics["buys_24h"],
            "sells_24h": metrics["sells_24h"],
        })
        if len(state["seen_tokens"][token_key]) > 20:
            state["seen_tokens"][token_key] = state["seen_tokens"][token_key][-20:]

    # ── Update narrative history ──
    all_narr = set()
    for d in batch_data:
        all_narr.update(d.get("narratives", []))
    if "narrative_history" not in state:
        state["narrative_history"] = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for n in all_narr:
        if n not in state["narrative_history"]:
            state["narrative_history"][n] = []
        if today not in state["narrative_history"][n]:
            state["narrative_history"][n].append(today)
        state["narrative_history"][n] = state["narrative_history"][n][-30:]

    # ── Correlation check ──
    correlations = detect_correlations(batch_data, state)
    cluster_warnings = format_correlation_warnings(correlations)

    # ── Send individual token alerts ──
    for metrics, tier, reason, safety, narratives, conviction, breakdown in alerts:
        token_key = metrics["token_key"]
        alert_text = format_token_alert(
            metrics, tier, reason, safety, narratives, conviction, breakdown, state, token_key)
        send_telegram(alert_text)
        log_to_sheets(metrics, tier, reason, safety, narratives, conviction)

        try:
            from outcome_tracker import register_alert
            register_alert(token_key, metrics["chain"], metrics["pair_address"],
                          metrics["price_usd"], metrics["liquidity"], tier, metrics["symbol"])
        except Exception:
            pass

    # ── Send exit alerts ──
    for symbol, signals in exit_alerts:
        # Find matching metrics
        matching = [d for d in batch_data if d["symbol"] == symbol]
        if matching:
            exit_text = format_exit_alert(matching[0]["token_key"], matching[0], signals, state)
            send_telegram(exit_text)

    # ── Send batch summary ──
    summary = format_batch_summary(batch_data, alerts, exit_alerts, cluster_warnings, state)
    send_telegram(summary)

    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"\nDONE: {len(batch_data)} processed, {len(alerts)} alerts, "
          f"{len(exit_alerts)} exits, {skipped} skipped")


if __name__ == "__main__":
    run_scan()
  
