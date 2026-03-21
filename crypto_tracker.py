"""
Viral Meme Coin Tracker v3 — GitHub Actions Edition
Fixed: Reddit 403 blocking with 3 fallback methods + test mode.
"""

import requests
import json
import time
import os
import sys
import html
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote as url_quote

CONFIG = {
    "subreddits": ["cryptocurrency", "cryptomoonshots", "memecoins"],
    "posts_per_subreddit": 10,
    "min_upvotes": 1500,
    "min_comments": 200,
    "max_token_age_hours": 48,
    "min_liquidity_usd": 10000,
    "min_volume_24h": 5000,
    "min_transactions_24h": 20,
    "min_safety_score": 5,
    "dex_pairs_to_scan": 10,
    "claude_model": "claude-haiku-4-5-20251001",
    "dedup_file": "alerted_tokens.json",
}

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tracker")

def safe_num(val):
    if val is None: return 0.0
    try: return float(val)
    except (ValueError, TypeError): return 0.0

def http_get(url, headers=None, retries=1):
    for attempt in range(retries + 1):
        try: return requests.get(url, headers=headers or {}, timeout=15)
        except requests.exceptions.RequestException as e:
            if attempt < retries: time.sleep(3)
            else: raise e

def http_post(url, headers=None, json_data=None, retries=1):
    for attempt in range(retries + 1):
        try: return requests.post(url, headers=headers, json=json_data, timeout=30)
        except requests.exceptions.RequestException as e:
            if attempt < retries: time.sleep(3)
            else: raise e

# --- Deduplication ---
def load_alerted_tokens():
    path = CONFIG["dedup_file"]
    if not os.path.exists(path): return {}
    try:
        with open(path, "r") as f: data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items() if now - v < 72 * 3600}
    except: return {}

def save_alerted_tokens(tokens):
    with open(CONFIG["dedup_file"], "w") as f: json.dump(tokens, f)

def already_alerted(addr): return addr in load_alerted_tokens()

def mark_alerted(addr):
    t = load_alerted_tokens(); t[addr] = time.time(); save_alerted_tokens(t)

# --- Reddit: 3 fallback methods ---
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
}

def _parse_reddit_json(resp_json, subreddit):
    posts = []
    for child in resp_json.get("data", {}).get("children", []):
        d = child.get("data", {})
        posts.append({"title": d.get("title",""), "score": d.get("score",0) or 0,
                       "num_comments": d.get("num_comments",0) or 0, "subreddit": d.get("subreddit", subreddit)})
    return posts

def fetch_reddit_json(sub, limit=10):
    url = f"https://www.reddit.com/r/{sub}/top/.json?t=day&limit={limit}"
    try:
        r = http_get(url, headers={**BROWSER_HEADERS, "Accept": "application/json"}, retries=1)
        if r.status_code != 200: return None, r.status_code
        return _parse_reddit_json(r.json(), sub), 200
    except: return None, 0

def fetch_reddit_old(sub, limit=10):
    url = f"https://old.reddit.com/r/{sub}/top/.json?t=day&limit={limit}"
    try:
        r = http_get(url, headers={"User-Agent": "ViralCoinTracker/2.1 (educational)"}, retries=1)
        if r.status_code != 200: return None, r.status_code
        return _parse_reddit_json(r.json(), sub), 200
    except: return None, 0

def fetch_reddit_rss(sub, limit=10):
    url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day&limit={limit}"
    try:
        r = http_get(url, headers=BROWSER_HEADERS, retries=1)
        if r.status_code != 200: return None, r.status_code
        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        posts = []
        for entry in root.findall("atom:entry", ns):
            t = entry.find("atom:title", ns)
            posts.append({"title": t.text if t is not None else "", "score": 9999, "num_comments": 9999, "subreddit": sub})
        return posts, 200
    except: return None, 0

def fetch_reddit_posts(sub, limit=10):
    for method_name, method_fn in [("json", fetch_reddit_json), ("old.reddit", fetch_reddit_old), ("rss", fetch_reddit_rss)]:
        posts, code = method_fn(sub, limit)
        if posts is not None:
            log.info(f"  r/{sub}: {len(posts)} posts ({method_name})")
            return posts
        log.warning(f"  r/{sub}: {method_name} failed ({code})")
    log.error(f"  r/{sub}: ALL methods failed")
    return []

# --- DexScreener fallback ---
def fetch_dexscreener_trending():
    log.info("  Using DexScreener boosted tokens as fallback...")
    try:
        r = http_get("https://api.dexscreener.com/token-boosts/top/v1", retries=1)
        if r.status_code != 200: return []
        tokens = r.json()
        if not isinstance(tokens, list): return []
        names, seen = [], set()
        for t in tokens[:20]:
            addr = t.get("tokenAddress", "")
            chain = t.get("chainId", "")
            desc = t.get("description", "") or addr[:12]
            if addr not in seen:
                names.append({"name": desc[:50], "ticker": addr[:12], "reason": f"Boosted on {chain} (DexScreener)", "_token_address": addr, "_chain": chain})
                seen.add(addr)
        return names
    except Exception as e:
        log.error(f"  DexScreener trending error: {e}"); return []

def search_dexscreener_by_address(chain, address):
    try:
        r = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", retries=1)
        if r.status_code != 200: return None
        pairs = r.json().get("pairs") or []
        for p in pairs:
            if p.get("chainId","") == chain: return p
        return pairs[0] if pairs else None
    except: return None

# --- Core ---
def extract_trending_names(posts):
    posts_text = "\n".join(f"{p['title']} ({p['score']} up, {p['num_comments']} comments) | r/{p['subreddit']}" for p in posts)
    prompt = ("You analyze viral Reddit crypto posts. Extract ONLY names, memes, projects, or tickers EXPLICITLY mentioned. "
              "Do NOT invent tickers.\n\nRespond ONLY with a JSON array. No markdown.\n"
              'Example: [{"name":"ghibli","ticker":"GHIBLI","reason":"Ghibli memes trending"}]\nIf nothing: []\n\n'
              f"Posts:\n{posts_text}")
    try:
        r = http_post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json_data={"model": CONFIG["claude_model"], "max_tokens": 1024, "messages": [{"role":"user","content":prompt}]}, retries=1)
        if r.status_code != 200: log.error(f"  Claude {r.status_code}: {r.text[:200]}"); return []
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"): text = text.split("\n",1)[-1].rsplit("```",1)[0].strip()
        result = json.loads(text)
        if not isinstance(result, list): return []
        return [i for i in result if isinstance(i, dict) and (i.get("ticker") or i.get("name"))]
    except json.JSONDecodeError: log.error("  Claude non-JSON"); return []
    except Exception as e: log.error(f"  Claude error: {e}"); return []

def search_dexscreener(query):
    encoded = url_quote(query.strip())
    try:
        r = http_get(f"https://api.dexscreener.com/latest/dex/search?q={encoded}", retries=1)
        if r.status_code != 200: return None
        pairs = r.json().get("pairs") or []
        if not pairs: return None
        now_ms = int(time.time() * 1000)
        max_age_ms = CONFIG["max_token_age_hours"] * 3600 * 1000
        for i in range(min(CONFIG["dex_pairs_to_scan"], len(pairs))):
            created = pairs[i].get("pairCreatedAt", 0) or 0
            if created > 0 and (now_ms - created) <= max_age_ms: return pairs[i]
        return None
    except Exception as e: log.error(f"  DexScreener error '{query}': {e}"); return None

def passes_quality_filters(pair):
    liq = safe_num(pair.get("liquidity",{}).get("usd"))
    if liq < CONFIG["min_liquidity_usd"]: return False, f"liq ${liq:,.0f}"
    vol = safe_num(pair.get("volume",{}).get("h24"))
    if vol < CONFIG["min_volume_24h"]: return False, f"vol ${vol:,.0f}"
    txns = pair.get("txns",{}).get("h24",{})
    total = safe_num(txns.get("buys")) + safe_num(txns.get("sells"))
    if total < CONFIG["min_transactions_24h"]: return False, f"txns {int(total)}"
    return True, "ok"

def safety_score(pair):
    s = 0
    liq = safe_num(pair.get("liquidity",{}).get("usd")); vol = safe_num(pair.get("volume",{}).get("h24"))
    txns = pair.get("txns",{}).get("h24",{}); total_tx = safe_num(txns.get("buys"))+safe_num(txns.get("sells"))
    pc = safe_num(pair.get("priceChange",{}).get("h6"))
    s += 3 if liq>50000 else 2 if liq>20000 else 1 if liq>10000 else 0
    s += 3 if vol>100000 else 2 if vol>25000 else 1 if vol>5000 else 0
    s += 2 if total_tx>500 else 1 if total_tx>100 else 0
    s += 2 if 10<pc<300 else 0
    return s

def age_hours(pair):
    c = pair.get("pairCreatedAt",0) or 0
    return "?" if not c else f"{(int(time.time()*1000)-c)/3600000:.1f}"

def send_telegram(item, pair, score):
    txns = pair.get("txns",{}).get("h24",{}); buys=int(safe_num(txns.get("buys"))); sells=int(safe_num(txns.get("sells")))
    chain=html.escape(str(pair.get("chainId","?"))); addr=pair.get("pairAddress","")
    link=f"https://dexscreener.com/{pair.get('chainId','')}/{addr}"
    symbol=html.escape(str(pair.get("baseToken",{}).get("symbol","?"))); name=html.escape(str(pair.get("baseToken",{}).get("name","?")))
    reason=html.escape(str(item.get("reason","N/A")))
    liq=safe_num(pair.get("liquidity",{}).get("usd")); vol=safe_num(pair.get("volume",{}).get("h24"))
    pc=safe_num(pair.get("priceChange",{}).get("h6")); fdv=safe_num(pair.get("fdv"))
    msg = (f"\U0001f525 <b>VIRAL TOKEN ALERT</b>\n\n\U0001f4cc <b>Trending:</b> {reason}\n"
           f"\U0001fa99 <b>Token:</b> {symbol} ({name})\n\u26d3 <b>Chain:</b> {chain}\n"
           f"\u23f0 <b>Age:</b> {age_hours(pair)} hours\n\n\U0001f4ca <b>Safety Score: {score}/10</b>\n"
           f"\U0001f4b0 Liquidity: ${liq:,.0f}\n\U0001f4c8 Volume 24h: ${vol:,.0f}\n"
           f"\U0001f504 Buys: {buys:,} | Sells: {sells:,} (24h)\n\U0001f4c8 6h Change: {pc}%\n"
           f"\U0001f48e FDV: ${fdv:,.0f}\n\n"
           f'\U0001f517 <a href="{link}">View on DexScreener</a>\n\n\u26a0\ufe0f DYOR \u2014 Not financial advice')
    try:
        r = http_post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json_data={"chat_id":TELEGRAM_CHAT_ID,"parse_mode":"HTML","text":msg}, retries=1)
        if r and r.status_code==200: log.info(f"  \u2705 Telegram sent: {symbol}")
        else: log.error(f"  \u274c Telegram {r.status_code if r else 'no resp'}: {r.text[:100] if r else ''}")
    except Exception as e: log.error(f"  \u274c Telegram failed: {e}")

def log_to_sheets(item, pair, score):
    if not GOOGLE_SHEET_WEBHOOK: return
    chain=pair.get("chainId",""); addr=pair.get("pairAddress","")
    row = {"timestamp":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),"topic":item.get("reason",""),
           "symbol":pair.get("baseToken",{}).get("symbol",""),"name":pair.get("baseToken",{}).get("name",""),
           "chain":chain,"liquidity":safe_num(pair.get("liquidity",{}).get("usd")),
           "volume":safe_num(pair.get("volume",{}).get("h24")),"priceChange":safe_num(pair.get("priceChange",{}).get("h6")),
           "score":score,"link":f"https://dexscreener.com/{chain}/{addr}"}
    try:
        r = requests.post(GOOGLE_SHEET_WEBHOOK, json=row, timeout=15)
        if r.status_code < 400: log.info("  \u2705 Logged to Google Sheets")
        else: log.warning(f"  Sheets {r.status_code}")
    except Exception as e: log.error(f"  Sheets error: {e}")

# --- Main ---
def main():
    test_mode = "--test" in sys.argv
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"{'='*55}")
    log.info(">>> VERSION CHECK: v3 with Reddit fallbacks <<<")
    log.info(f"\U0001f680 SCAN STARTED \u2014 {ts}{'  [TEST MODE]' if test_mode else ''}")
    log.info(f"{'='*55}")
    if not CLAUDE_API_KEY: log.error("\u274c CLAUDE_API_KEY not set"); sys.exit(1)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: log.error("\u274c Telegram secrets not set"); sys.exit(1)

    log.info("\U0001f4f1 Phase 1: Fetching Reddit...")
    all_posts = []
    for sub in CONFIG["subreddits"]:
        all_posts.extend(fetch_reddit_posts(sub, CONFIG["posts_per_subreddit"]))
        time.sleep(2)

    use_fallback = False; names = []
    if all_posts:
        viral = filter_viral(all_posts)
        log.info(f"  \u2192 {len(viral)} viral out of {len(all_posts)}")
        if viral:
            log.info("\U0001f916 Phase 2: Claude AI...")
            names = extract_trending_names(viral)
            log.info(f"  \u2192 {len(names)} names:")
            for n in names: log.info(f"    \u2022 {n.get('ticker','?')} \u2014 {n.get('reason','')}")

    if not names:
        log.warning("  Reddit failed or empty. Fallback to DexScreener trending...")
        use_fallback = True
        names = fetch_dexscreener_trending()
        if not names: log.info("  Nothing found anywhere. Done."); return
        log.info(f"  \u2192 {len(names)} boosted tokens")

    log.info("\U0001f50d Phase 3: Searching tokens...")
    if test_mode:
        log.info("  [TEST: all filters disabled]")
        CONFIG.update({"min_liquidity_usd":0,"min_volume_24h":0,"min_transactions_24h":0,"min_safety_score":0,"max_token_age_hours":87600})
    alerts = 0
    for item in names:
        ticker = (item.get("ticker") or item.get("name") or "").strip()
        if not ticker or len(ticker)<2: continue
        log.info(f"  Searching: {ticker}")
        if use_fallback and item.get("_token_address"):
            pair = search_dexscreener_by_address(item.get("_chain",""), item["_token_address"])
        else:
            pair = search_dexscreener(ticker)
        time.sleep(0.5)
        if not pair: log.info(f"    \u2192 No token found"); continue
        sym=pair.get("baseToken",{}).get("symbol","?"); addr=pair.get("pairAddress","")
        log.info(f"    \u2192 Found: {sym} on {pair.get('chainId','?')} (age: {age_hours(pair)}h)")
        ok, reason = passes_quality_filters(pair)
        if not ok: log.info(f"    \u2192 Filtered: {reason}"); continue
        sc = safety_score(pair)
        log.info(f"    \u2192 Score: {sc}/10")
        if sc < CONFIG["min_safety_score"]: log.info(f"    \u2192 Too low"); continue
        if already_alerted(addr) and not test_mode: log.info(f"    \u2192 Already alerted"); continue
        log.info(f"    \U0001f525 ALERT: {sym}")
        send_telegram(item, pair, sc)
        log_to_sheets(item, pair, sc)
        mark_alerted(addr); alerts += 1
        if test_mode and alerts>=1: log.info("  [TEST: done after 1 alert]"); break

    log.info(f"{'='*55}")
    log.info(f"\u2705 DONE \u2014 {alerts} alert(s) sent")
    log.info(f"{'='*55}")

def filter_viral(posts):
    return [p for p in posts if p["score"]>=CONFIG["min_upvotes"] or p["num_comments"]>=CONFIG["min_comments"]]

if __name__ == "__main__":
    main()
