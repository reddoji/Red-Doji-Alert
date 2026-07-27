#!/usr/bin/env python3
"""
XAUUSD Symmetric Red-Doji → Telegram notifier  [NSTC]
────────────────────────────────────────────────────────────────────────────
Mirrors the Pine indicator's doji definition (red candle, small body,
symmetric wicks) on the 5m and 15m timeframes and sends a Telegram message
the moment a doji CLOSES. Free — no broker, no TradingView paid plan.

Data: Twelve Data (free tier: 800 requests/day, 8/min). Polling 2 timeframes
every 5 minutes uses ~576 requests/day, safely inside the free limit.

Run:
    python doji_telegram_bot.py            # loop forever (recommended)
    python doji_telegram_bot.py --once     # single check (for cron / CI)
"""

import os
import sys
import json
import time
import argparse
import datetime as dt

import requests

# ══════════════════════════════════════════════════════════════════════════
#  CONFIG  — set these as environment variables, or paste them here directly
# ══════════════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PASTE_BOT_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "PASTE_CHAT_ID")
TD_KEY         = os.getenv("TWELVEDATA_KEY", "PASTE_TWELVEDATA_KEY")

SYMBOL       = os.getenv("SYMBOL", "XAU/USD")
TIMEFRAMES   = ["5min", "15min"]     # matches your 5m / 15m strategy
BODY_PCT     = 0.20                   # max body as % of range  (0.20 = 20%)
WICK_TOL     = 0.15                   # wick symmetry tolerance (0.15 = 15%)
REQUIRE_RED  = True                   # only red (bearish) dojis
POLL_SECONDS = 300                    # 5 minutes (keeps under the free API cap)
STATE_FILE   = os.getenv("STATE_FILE", ".doji_state.json")

TD_URL = "https://api.twelvedata.com/time_series"


# ── state (so the same candle isn't alerted twice) ──
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("state save error:", e)


# ── data ──
def interval_minutes(interval):
    return int(interval.replace("min", ""))

def fetch_candles(interval, n=6):
    params = {
        "symbol":     SYMBOL,
        "interval":   interval,
        "outputsize": n,
        "timezone":   "UTC",
        "apikey":     TD_KEY,
        "format":     "JSON",
    }
    r = requests.get(TD_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(data.get("message", "no data returned"))
    out = []
    for v in data["values"]:                     # newest first
        out.append({
            "t": v["datetime"],
            "o": float(v["open"]),
            "h": float(v["high"]),
            "l": float(v["low"]),
            "c": float(v["close"]),
        })
    return out

def last_closed(candles, interval):
    """Newest candle that has actually closed (avoids the forming bar)."""
    mins = interval_minutes(interval)
    now = dt.datetime.utcnow()
    for c in candles:                            # newest first
        start = dt.datetime.strptime(c["t"], "%Y-%m-%d %H:%M:%S")
        if start + dt.timedelta(minutes=mins) <= now + dt.timedelta(seconds=5):
            return c
    return candles[1] if len(candles) > 1 else None


# ── doji test (identical logic to the Pine indicator) ──
def is_symmetric_red_doji(c):
    rng = c["h"] - c["l"]
    if rng <= 0:
        return False
    body  = abs(c["o"] - c["c"])
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    small_body = body <= rng * BODY_PCT
    symmetric  = abs(upper - lower) <= rng * WICK_TOL
    red        = c["c"] < c["o"]
    return small_body and symmetric and (red or not REQUIRE_RED)


# ── telegram ──
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT, "text": text,
                                     "parse_mode": "HTML"}, timeout=20)
        if r.status_code != 200:
            print("telegram error:", r.text)
    except Exception as e:
        print("telegram send failed:", e)


# ── main check ──
def check_tf(interval, state):
    candles = fetch_candles(interval, n=12)
    mins = interval_minutes(interval)
    now = dt.datetime.utcnow()
    # keep only CLOSED candles, oldest -> newest
    closed = []
    for c in candles:
        start = dt.datetime.strptime(c["t"], "%Y-%m-%d %H:%M:%S")
        if start + dt.timedelta(minutes=mins) <= now + dt.timedelta(seconds=5):
            closed.append(c)
    closed.sort(key=lambda c: c["t"])
    if not closed:
        return

    last_alerted = state.get(interval, "")
    # first time we see this timeframe: set a baseline so we don't spam history
    if not last_alerted:
        state[interval] = closed[-1]["t"]
        save_state(state)
        return

    newest = last_alerted
    for c in closed:
        if c["t"] <= last_alerted:        # already handled in a previous run
            continue
        if is_symmetric_red_doji(c):
            tf = interval.replace("min", "m")
            send_telegram(f"🔴 Red Doji — {SYMBOL} {tf} @ {c['c']:.2f}")
            print("ALERT", interval, c["t"])
        newest = c["t"]                    # advance past every candle we've now seen
    if newest != last_alerted:
        state[interval] = newest
        save_state(state)

def run_once():
    state = load_state()
    for tf in TIMEFRAMES:
        try:
            check_tf(tf, state)
        except Exception as e:
            print(f"[{tf}] error:", e)

def check_config():
    missing = [n for n, v in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                              ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT),
                              ("TWELVEDATA_KEY", TD_KEY)) if v.startswith("PASTE")]
    if missing:
        print("⚠  Missing config:", ", ".join(missing))
        print("   Set them as environment variables or edit the CONFIG block.")
        sys.exit(1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--test", action="store_true", help="send a test Telegram message then exit")
    args = ap.parse_args()

    # test mode: prove the Telegram path works (no market data needed)
    if args.test or os.getenv("SEND_TEST") == "true":
        send_telegram("✅ Doji bot test — working. XAUUSD watcher is live.")
        print("test message sent")
        return

    check_config()
    if args.once or os.getenv("RUN_ONCE"):
        run_once()
        return
    print(f"Doji bot running… {SYMBOL} {TIMEFRAMES}  every {POLL_SECONDS}s")
    send_telegram(f"✅ Doji bot started — watching {SYMBOL} "
                  + ", ".join(t.replace('min', 'm') for t in TIMEFRAMES))
    while True:
        run_once()
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
