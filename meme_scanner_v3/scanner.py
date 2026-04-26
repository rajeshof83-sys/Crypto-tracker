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
- Dual-tab Sheets logging via Google Apps Script webhook:
    * "Scanner v3" tab: T1/T2 alerts only (trading signals)
    * "All Tokens"  tab: every processed token (analytics dataset)
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

# GeckoTerminal enrichment (Phase 2 integration)
# Falls back gracefully if the module is missing or the API is down.
try:
    import geckoterminal
    GT_AVAILABLE = True
except ImportError:
    print("[WARN] geckoterminal module not found — running without enrichment")
    GT_AVAILABLE = False

# ============================================================
# CONFIGURATION
# ============================================================

DEXSCREENER_BOOST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SUPPORTED_CHAINS = ["solana", "bsc", "base", "ethereum"]
STATE_FILE = "scanner_state.json"

# Google Sheets target tabs (must match exactly the tab names in the spreadsheet)
SHEET_TAB_ALERTS = "Scanner v3"   # T1/T2 alerts only — trading signals
SHEET_TAB_ALL = "All Tokens"      # every processed token — analytics dataset

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
    """Returns (should_run, minutes_since_last). Manual & digest runs bypass."""
    last_scan_iso = state.get("last_scan")
    if not last_scan_iso:
        return True, None
    try:
        last_scan = datetime.fromisoformat(last_scan_iso)
        if last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60
        return (minutes_since >= min_minutes), minutes_since
    except (ValueError, TypeError) as e:
        print(f"[WARN] Could not parse last_scan timestamp: {e} — running anyway")
        return True, None


def is_manual_dispatch():
    """True when triggered by 'Run workflow' button. Bypasses dedup guard."""
    return os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"


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


def safe_compute_conviction(metrics, safety, narratives, state, token_key, batch_data):
    """Compute conviction for ANY token; safe to call on skipped tokens."""
    try:
        return compute_conviction_score(
            metrics, safety, narratives, state, token_key, batch_data)
    except Exception as e:
        print(f"[WARN] Conviction calc failed for {metrics.get('symbol', '?')}: {e}")
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

    # ── GeckoTerminal: unique buyer/seller info (Phase 2) ──
    ratio = metrics.get("gt_buyer_seller_ratio_h1")
    if ratio is not None:
        b = metrics.get("gt_buyers_h1") or 0
        s = metrics.get("gt_sellers_h1") or 0
        ratio_emoji = "🟢" if ratio >= 1.2 else "🟡" if ratio >= 0.8 else "🔴"
        lines.append(f"{ratio_emoji} Unique 1h: {b}B / {s}S (ratio {ratio:.2f})")

    history = state.get("seen_tokens", {}).get(token_key, [])
    if len(history) >= 2:
        lines.append(f"\n{format_velocity(history)}")

    if tier == "T2" and history:
        first = history[0]
        lm = metrics['liquidity'] / first["liquidity"] if first["liquidity"] > 0 else 0
        sp = " → ".join(str(h["safety_score"]) for h in history) + f" → {safety}"
        lines.extend([
            f"\n📜 <b>History ({len(history)+1} appearances)</b>",
            f"   Liq: ${first['liquidity']:,.0f} → ${metrics['liquidity']:,.0f} ({lm:.1f}x)",
            f"   Safety: {sp}",
        ])

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

    if alerts:
        lines.append(f"\n<b>ALERTS:</b>")
        for m, t, r, s, n, c, _ in alerts:
            e = "🟢" if t == "T1" else "🔵"
            lines.append(f"  {e} {m['symbol']} — 🎯{c}/10 | ${m['liquidity']:,.0f} | {m['price_change_6h']:+.0f}%")

    if exit_alerts:
        lines.append(f"\n<b>EXIT SIGNALS:</b>")
        for symbol, signals in exit_alerts:
            worst = max(signals, key=lambda s: 1 if "CRITICAL" in s[1] else 0)
            lines.append(f"  {worst[1]} {symbol}: {worst[2][:60]}")

    if cluster_warnings:
        lines.append(f"\n{cluster_warnings[:200]}")

    top_narr = narr_counts.most_common(4)
    if top_narr:
        lines.append(f"\n<b>NARRATIVES:</b> {' | '.join(f'{n}({c})' for n,c in top_narr)}")

    sorted_batch = sorted(batch_data, key=lambda d: d.get("price_change_6h", 0), reverse=True)
    if sorted_batch:
        best = sorted_batch[0]
        lines.append(f"\n🟩 Top: {best['symbol']} {best.get('price_change_6h',0):+.0f}%")
        if sorted_batch[-1].get("price_change_6h", 0) < -20:
            worst = sorted_batch[-1]
            lines.append(f"🟥 Bot: {worst['symbol']} {worst.get('price_change_6h',0):+.0f}%")

    return "\n".join(lines)


# ============================================================
# SHEETS LOGGING (via Google Apps Script webhook)
# ============================================================

def _post_to_webhook(tab_name, rows):
    """
    POST a batch of rows to the Google Apps Script webhook.
    rows: list of lists. Each inner list is one sheet row in column order.
    Returns True on success, False on failure.
    """
    webhook_url = os.environ.get("GOOGLE_SHEET_WEBHOOK")
    if not webhook_url:
        print("[WARN] Sheets: GOOGLE_SHEET_WEBHOOK env var not set — skipping log")
        return False

    if not rows:
        return True

    payload = {"tab": tab_name, "rows": rows}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
        # Apps Script v2 returns JSON {"ok": bool, ...}; older versions return "OK".
        try:
            result = resp.json()
            if isinstance(result, dict) and not result.get("ok", True):
                print(f"[ERROR] Sheets webhook ({tab_name}): {result.get('error', 'unknown')}")
                return False
        except ValueError:
            pass  # plain "OK" text response — treat 200 as success
        print(f"[SHEETS] Logged {len(rows)} row(s) → '{tab_name}'")
        return True
    except Exception as e:
        print(f"[ERROR] Sheets webhook POST ({tab_name}): {e}")
        return False


def log_alert_to_sheets(metrics, tier, reason, safety, narratives, conviction):
    """Log a single T1/T2 alert to the 'Scanner v3' tab."""
    row = [
        metrics["timestamp"], tier, reason, metrics["symbol"], metrics["name"],
        metrics["chain"], metrics["pair_address"], metrics["liquidity"],
        metrics["volume_24h"], round(metrics["vol_liq_ratio"], 2), metrics["price_change_6h"],
        safety, conviction if conviction is not None else "",
        metrics["price_usd"], metrics["buys_1h"], metrics["sells_1h"],
        metrics["buys_24h"], metrics["sells_24h"], metrics["dex_url"],
        " | ".join(narratives), "", "", "", "", "",
        # ── GeckoTerminal enrichment columns (Phase 2) ──
        "TRUE" if metrics.get("gt_enriched") else "FALSE",
        metrics.get("gt_buyers_h1") if metrics.get("gt_buyers_h1") is not None else "",
        metrics.get("gt_sellers_h1") if metrics.get("gt_sellers_h1") is not None else "",
        round(metrics["gt_buyer_seller_ratio_h1"], 3) if metrics.get("gt_buyer_seller_ratio_h1") is not None else "",
        round(metrics["gt_liquidity_usd"], 0) if metrics.get("gt_liquidity_usd") is not None else "",
        round(metrics["gt_volume_h1"], 0) if metrics.get("gt_volume_h1") is not None else "",
    ]
    _post_to_webhook(SHEET_TAB_ALERTS, [row])


def log_all_tokens_to_sheets(token_rows):
    """Batch-log every processed token to 'All Tokens' tab in one POST."""
    if not token_rows:
        return
    rows = []
    for r in token_rows:
        rows.append([
            r["timestamp"],
            r["token_key"],
            r["symbol"],
            r["name"],
            r["chain"],
            r["pair_address"],
            r["tier"] or "",
            r["skip_reason"] or "",
            "TRUE" if r["would_alert"] else "FALSE",
            r["conviction"] if r["conviction"] is not None else "",
            r["liquidity"],
            r["volume_24h"],
            round(r["vol_liq_ratio"], 2),
            r["price_change_6h"],
            r["safety_score"],
            r["price_usd"],
            r["buys_1h"],
            r["sells_1h"],
            r["buys_24h"],
            r["sells_24h"],
            r["fdv"],
            "TRUE" if r["is_returning"] else "FALSE",
            r["appearance_number"],
            r["first_liq"] if r["first_liq"] is not None else "",
            round(r["liq_multiple"], 2) if r["liq_multiple"] is not None else "",
            " | ".join(r["narratives"]),
            "TRUE" if r["catalyst_detected"] else "FALSE",
            r["dex_url"],
            # ── GeckoTerminal enrichment columns (Phase 2) ──
            "TRUE" if r.get("gt_enriched") else "FALSE",
            r.get("gt_buyers_h1") if r.get("gt_buyers_h1") is not None else "",
            r.get("gt_sellers_h1") if r.get("gt_sellers_h1") is not None else "",
            round(r["gt_buyer_seller_ratio_h1"], 3) if r.get("gt_buyer_seller_ratio_h1") is not None else "",
            round(r["gt_liquidity_usd"], 0) if r.get("gt_liquidity_usd") is not None else "",
            round(r["gt_volume_h1"], 0) if r.get("gt_volume_h1") is not None else "",
            round(r["gt_volume_h6"], 0) if r.get("gt_volume_h6") is not None else "",
            r.get("gt_price_change_h1") if r.get("gt_price_change_h1") is not None else "",
            r.get("gt_price_change_h6") if r.get("gt_price_change_h6") is not None else "",
        ])
    _post_to_webhook(SHEET_TAB_ALL, rows)


# ============================================================
# MAIN
# ============================================================

def run_scan():
    print(f"\n{'='*60}")
    print(f"SCAN v3: {datetime.now(timezone.utc).isoformat()}")
    print(f"Trigger: {os.environ.get('GITHUB_EVENT_NAME', 'unknown')}")
    print(f"{'='*60}")

    state = load_state()

    # ── Check if digest is due (digests bypass dedup guard) ──
    digest_type = get_digest_type()
    if digest_type and should_send_digest(digest_type):
        print(f"[DIGEST] Generating {digest_type} digest")
        digest_msg = generate_digest(digest_type)
        if digest_msg:
            send_telegram(digest_msg)
            mark_digest_sent(digest_type)

    # ── Dedup guard for staggered hourly crons ──
    if is_manual_dispatch():
        print("▶ Manual run (workflow_dispatch) — bypassing dedup guard")
    else:
        should_run, minutes_since = should_run_hourly_scan(state)
        if not should_run:
            if minutes_since is not None:
                print(f"⏭ Skipping scan — last scan was {minutes_since:.1f} min ago "
                      f"(threshold: {MIN_MINUTES_BETWEEN_SCANS} min)")
            else:
                print(f"⏭ Skipping scan — within dedup window")
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
    alerts = []
    exit_alerts = []
    all_tokens_rows = []
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

        # ── GeckoTerminal enrichment (Phase 2) ──
        # Adds gt_buyers_h1, gt_sellers_h1, gt_buyer_seller_ratio_h1,
        # gt_volume_h1/h6/h24, gt_price_change_h1/h6/h24, gt_fdv_usd, etc.
        # Never raises — sets gt_enriched=False on any failure.
        if GT_AVAILABLE:
            try:
                metrics = geckoterminal.enrich_token(metrics)
            except Exception as e:
                print(f"[WARN] GT enrichment failed for {metrics['symbol']}: {e}")
                metrics["gt_enriched"] = False
        else:
            metrics["gt_enriched"] = False

        batch_data.append(metrics)

        # ── Classify ──
        tier, reason = classify(metrics, safety, state, token_key)

        # ── Compute conviction for EVERY token (analytics) ──
        conviction, breakdown = safe_compute_conviction(
            metrics, safety, narratives, state, token_key, batch_data)

        is_catalyst, _ = check_social_catalyst(metrics["symbol"], metrics["name"])

        # ── Appearance metadata ──
        history = state.get("seen_tokens", {}).get(token_key, [])
        appearance_number = len(history) + 1
        first_liq = history[0]["liquidity"] if history else None
        liq_multiple = (metrics["liquidity"] / first_liq) if first_liq and first_liq > 0 else None

        # ── Build All Tokens analytics row ──
        all_tokens_rows.append({
            "timestamp": metrics["timestamp"],
            "token_key": token_key,
            "symbol": metrics["symbol"],
            "name": metrics["name"],
            "chain": metrics["chain"],
            "pair_address": metrics["pair_address"],
            "tier": tier if tier else "",
            "skip_reason": reason if tier == "SKIP" else (reason if tier is None else ""),
            "would_alert": tier in ("T1", "T2"),
            "conviction": round(conviction, 2) if conviction is not None else None,
            "liquidity": metrics["liquidity"],
            "volume_24h": metrics["volume_24h"],
            "vol_liq_ratio": metrics["vol_liq_ratio"],
            "price_change_6h": metrics["price_change_6h"],
            "safety_score": safety,
            "price_usd": metrics["price_usd"],
            "buys_1h": metrics["buys_1h"],
            "sells_1h": metrics["sells_1h"],
            "buys_24h": metrics["buys_24h"],
            "sells_24h": metrics["sells_24h"],
            "fdv": metrics["fdv"],
            "is_returning": metrics["is_returning"],
            "appearance_number": appearance_number,
            "first_liq": first_liq,
            "liq_multiple": liq_multiple,
            "narratives": narratives,
            "catalyst_detected": is_catalyst,
            "dex_url": metrics["dex_url"],
            # ── GeckoTerminal enrichment fields (Phase 2) ──
            "gt_enriched": metrics.get("gt_enriched", False),
            "gt_buyers_h1": metrics.get("gt_buyers_h1"),
            "gt_sellers_h1": metrics.get("gt_sellers_h1"),
            "gt_buyer_seller_ratio_h1": metrics.get("gt_buyer_seller_ratio_h1"),
            "gt_liquidity_usd": metrics.get("gt_liquidity_usd"),
            "gt_volume_h1": metrics.get("gt_volume_h1"),
            "gt_volume_h6": metrics.get("gt_volume_h6"),
            "gt_price_change_h1": metrics.get("gt_price_change_h1"),
            "gt_price_change_h6": metrics.get("gt_price_change_h6"),
        })

        # ── Action paths ──
        if tier == "SKIP":
            skipped += 1
        elif tier in ("T1", "T2"):
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

    # ── Send individual token alerts + log to Scanner v3 tab ──
    for metrics, tier, reason, safety, narratives, conviction, breakdown in alerts:
        token_key = metrics["token_key"]
        alert_text = format_token_alert(
            metrics, tier, reason, safety, narratives, conviction, breakdown, state, token_key)
        send_telegram(alert_text)
        log_alert_to_sheets(metrics, tier, reason, safety, narratives, conviction)

        try:
            from outcome_tracker import register_alert
            register_alert(token_key, metrics["chain"], metrics["pair_address"],
                          metrics["price_usd"], metrics["liquidity"], tier, metrics["symbol"])
        except Exception:
            pass

    # ── Send exit alerts ──
    for symbol, signals in exit_alerts:
        matching = [d for d in batch_data if d["symbol"] == symbol]
        if matching:
            exit_text = format_exit_alert(matching[0]["token_key"], matching[0], signals, state)
            send_telegram(exit_text)

    # ── Batch-log all processed tokens to All Tokens tab (single API call) ──
    log_all_tokens_to_sheets(all_tokens_rows)

    # ── Send batch summary ──
    summary = format_batch_summary(batch_data, alerts, exit_alerts, cluster_warnings, state)
    send_telegram(summary)

    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # ── Enrichment quality stats (Phase 2 monitoring) ──
    gt_success = sum(1 for d in batch_data if d.get("gt_enriched"))
    gt_pct = (100 * gt_success / len(batch_data)) if batch_data else 0

    print(f"\nDONE: {len(batch_data)} processed, {len(alerts)} alerts, "
          f"{len(exit_alerts)} exits, {skipped} skipped")
    print(f"      GT enrichment: {gt_success}/{len(batch_data)} ({gt_pct:.0f}%)")


if __name__ == "__main__":
    run_scan()
