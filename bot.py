"""
Bot de paper trading crypto — test de 30 de zile, 100 EUR virtuali.

Rulează la fiecare 15 minute (GitHub Actions). La fiecare rulare:
  1. Descarcă lumânările de 15 min de pe API-ul public Kraken (fără cont, fără chei).
  2. Procesează, în ordine, toate lumânările ÎNCHISE apărute de la ultima rulare
     (dacă o rulare e întârziată sau sărită, nu pierde nimic).
  3. Simulează ordinele cu comision și slippage, salvează starea și jurnalul.

Nicio tranzacție reală nu este plasată. Botul nu are acces la niciun cont.

Strategie (fixă pe toată durata testului, NU se modifică pe parcurs):
  - Doar long (spot), capital împărțit 50/50 între BTC și ETH.
  - Intrare: EMA20 trece peste EMA50 ȘI prețul e peste EMA200 (filtru de trend).
  - Ieșire: EMA20 trece sub EMA50, SAU stop-loss la 2 x ATR14 sub intrare
    (stop-ul urcă odată cu prețul — trailing — dar nu coboară niciodată).
  - Ordinele se execută la închiderea lumânării-semnal; stop-ul la nivelul lui.

Utilizare:
  python bot.py run        # rularea normală (o face GitHub Actions)
  python bot.py backtest   # test rapid pe ultimele ~7 zile de date
  python bot.py report     # regenerează doar raportul
"""

import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- configurare
START_CAPITAL_EUR = 100.0
TEST_DAYS = 30
PAIRS = {"BTC": "XBTEUR", "ETH": "ETHEUR"}   # alocare egală
INTERVAL_MIN = 15
FEE_RATE = 0.0040        # comision taker Kraken Pro, nivelul de bază (0,40%)
SLIPPAGE = 0.0005        # 0,05% pe fiecare execuție
EMA_FAST, EMA_SLOW, EMA_TREND = 20, 50, 200
ATR_LEN, ATR_MULT = 14, 2.0
MIN_ORDER_EUR = 5.0

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "state.json")
TRADES_FILE = os.path.join(DATA, "trades.csv")
EQUITY_FILE = os.path.join(DATA, "equity.csv")
REPORT_FILE = os.path.join(ROOT, "RAPORT.md")


# ---------------------------------------------------------------- date de piață
def fetch_candles(kraken_pair):
    url = (f"https://api.kraken.com/0/public/OHLC"
           f"?pair={kraken_pair}&interval={INTERVAL_MIN}")
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.load(r)
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            key = next(k for k in payload["result"] if k != "last")
            rows = payload["result"][key]
            candles = [{
                "ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]),
            } for c in rows]
            # ultima lumânare e încă deschisă — o eliminăm
            return candles[:-1]
        except Exception as e:  # rețea / API temporar indisponibil
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Nu am putut descărca {kraken_pair}: {last_err}")


# ---------------------------------------------------------------- indicatori
def ema_series(values, n):
    k = 2 / (n + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr_series(candles, n):
    trs, out, prev_close, prev_atr = [], [], None, None
    for c in candles:
        tr = c["high"] - c["low"] if prev_close is None else max(
            c["high"] - c["low"], abs(c["high"] - prev_close), abs(c["low"] - prev_close))
        prev_close = c["close"]
        trs.append(tr)
        if len(trs) < n:
            out.append(None)
        elif len(trs) == n:
            prev_atr = sum(trs) / n
            out.append(prev_atr)
        else:
            prev_atr = (prev_atr * (n - 1) + tr) / n   # Wilder
            out.append(prev_atr)
    return out


def indicators(candles):
    closes = [c["close"] for c in candles]
    return {
        "fast": ema_series(closes, EMA_FAST),
        "slow": ema_series(closes, EMA_SLOW),
        "trend": ema_series(closes, EMA_TREND),
        "atr": atr_series(candles, ATR_LEN),
    }


# ---------------------------------------------------------------- stare
def new_state(first_prices, start_ts):
    alloc = START_CAPITAL_EUR / len(PAIRS)
    return {
        "started_at": start_ts,
        "ends_at": start_ts + TEST_DAYS * 86400,
        "finished": False,
        "benchmark_start_prices": first_prices,
        "pairs": {sym: {"cash": alloc, "qty": 0.0, "entry": None, "stop": None,
                        "entry_ts": None, "last_ts": None}
                  for sym in PAIRS},
        "fees_paid": 0.0,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_csv(path, header, row):
    os.makedirs(DATA, exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        w.writerow(row)


TRADE_HEADER = ["timp_utc", "moneda", "actiune", "pret", "cantitate",
                "valoare_eur", "comision_eur", "motiv", "pnl_eur"]


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- execuție simulată
def buy(state, sym, price, ts, stop, reason, log):
    p = state["pairs"][sym]
    if p["cash"] < MIN_ORDER_EUR:
        return
    fill = price * (1 + SLIPPAGE)
    fee = p["cash"] * FEE_RATE
    qty = (p["cash"] - fee) / fill
    log(ts, sym, "CUMPARA", fill, qty, p["cash"], fee, reason, "")
    state["fees_paid"] += fee
    p.update(qty=qty, cash=0.0, entry=fill, stop=stop, entry_ts=ts)


def sell(state, sym, price, ts, reason, log):
    p = state["pairs"][sym]
    if p["qty"] <= 0:
        return
    fill = price * (1 - SLIPPAGE)
    gross = p["qty"] * fill
    fee = gross * FEE_RATE
    cost_basis = p["qty"] * p["entry"]
    pnl = gross - fee - cost_basis
    log(ts, sym, "VINDE", fill, p["qty"], gross, fee, reason, round(pnl, 4))
    state["fees_paid"] += fee
    p.update(cash=p["cash"] + gross - fee, qty=0.0, entry=None, stop=None, entry_ts=None)


def step(state, sym, candles, ind, i, log):
    """Procesează lumânarea închisă de la indexul i pentru o monedă."""
    c, p = candles[i], state["pairs"][sym]
    atr = ind["atr"][i]
    if i < 1 or atr is None or i < EMA_TREND:
        return
    fast, slow, trend = ind["fast"][i], ind["slow"][i], ind["trend"][i]
    pf, ps = ind["fast"][i - 1], ind["slow"][i - 1]

    if p["qty"] > 0:
        # 1) stop-loss atins în timpul lumânării
        if c["low"] <= p["stop"]:
            exit_px = min(p["stop"], c["open"])   # gap sub stop -> ieșire la open
            sell(state, sym, exit_px, c["ts"], "stop-loss", log)
            return
        # 2) semnal de ieșire
        if pf >= ps and fast < slow:
            sell(state, sym, c["close"], c["ts"], "EMA20 sub EMA50", log)
            return
        # 3) trailing stop — doar urcă
        p["stop"] = max(p["stop"], c["close"] - ATR_MULT * atr)
    else:
        if pf <= ps and fast > slow and c["close"] > trend:
            buy(state, sym, c["close"], c["ts"],
                c["close"] - ATR_MULT * atr, "EMA20 peste EMA50, trend sus", log)


def equity(state, last_prices):
    total = 0.0
    for sym, p in state["pairs"].items():
        total += p["cash"] + p["qty"] * last_prices[sym]
    return total


def benchmark(state, last_prices):
    alloc = START_CAPITAL_EUR / len(PAIRS)
    total = 0.0
    for sym, start_px in state["benchmark_start_prices"].items():
        qty = alloc * (1 - FEE_RATE) / (start_px * (1 + SLIPPAGE))
        total += qty * last_prices[sym]
    return total


# ---------------------------------------------------------------- rulare
def run():
    all_candles = {sym: fetch_candles(pair) for sym, pair in PAIRS.items()}
    last_prices = {sym: cs[-1]["close"] for sym, cs in all_candles.items()}
    now_ts = min(cs[-1]["ts"] for cs in all_candles.values())

    state = load_state()
    if state is None:
        state = new_state(last_prices, now_ts)
        for sym in PAIRS:   # începem de la lumânarea curentă, nu din trecut
            state["pairs"][sym]["last_ts"] = all_candles[sym][-1]["ts"]
        print(f"Test pornit la {iso(now_ts)} UTC, se încheie la {iso(state['ends_at'])} UTC")

    if state["finished"]:
        print("Testul s-a încheiat deja.")
        write_report(state, last_prices)
        return

    def log(ts, sym, action, px, qty, value, fee, reason, pnl):
        append_csv(TRADES_FILE, TRADE_HEADER,
                   [iso(ts), sym, action, round(px, 2), round(qty, 8),
                    round(value, 2), round(fee, 4), reason, pnl])
        print(f"{iso(ts)} {sym} {action} @ {px:.2f} ({reason})")

    for sym, candles in all_candles.items():
        ind = indicators(candles)
        p = state["pairs"][sym]
        for i, c in enumerate(candles):
            if c["ts"] <= p["last_ts"] or c["ts"] >= state["ends_at"]:
                continue
            step(state, sym, candles, ind, i, log)
            p["last_ts"] = c["ts"]

    # final de test: închidem pozițiile la ultimul preț
    if now_ts >= state["ends_at"]:
        for sym in PAIRS:
            sell(state, sym, last_prices[sym], now_ts, "final test (30 zile)", log)
        state["finished"] = True

    eq, bm = equity(state, last_prices), benchmark(state, last_prices)
    append_csv(EQUITY_FILE, ["timp_utc", "bot_eur", "buy_and_hold_eur",
                             "pret_btc", "pret_eth"],
               [iso(now_ts), round(eq, 2), round(bm, 2),
                last_prices["BTC"], last_prices["ETH"]])
    save_state(state)
    write_report(state, last_prices)
    print(f"Bot: {eq:.2f} EUR | Buy & hold: {bm:.2f} EUR")


# ---------------------------------------------------------------- raport
def read_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE) as f:
        return list(csv.DictReader(f))


def write_report(state, last_prices):
    eq, bm = equity(state, last_prices), benchmark(state, last_prices)
    trades = read_trades()
    closed = [t for t in trades if t["actiune"] == "VINDE"]
    wins = [t for t in closed if float(t["pnl_eur"] or 0) > 0]
    now = time.time()
    days_done = min(TEST_DAYS, max(0, (now - state["started_at"]) / 86400))
    status = "ÎNCHEIAT" if state["finished"] else f"în curs — ziua {days_done:.1f} din {TEST_DAYS}"

    lines = [
        "# Raport — bot paper trading crypto",
        "",
        f"**Stare:** {status}  ",
        f"**Perioadă:** {iso(state['started_at'])} → {iso(state['ends_at'])} UTC  ",
        f"**Actualizat:** {iso(int(now))} UTC",
        "",
        "| | Valoare | Rezultat |",
        "|---|---:|---:|",
        f"| Bot | {eq:.2f} € | {eq - START_CAPITAL_EUR:+.2f} € ({(eq / START_CAPITAL_EUR - 1) * 100:+.1f}%) |",
        f"| Buy & hold (50% BTC / 50% ETH) | {bm:.2f} € | {bm - START_CAPITAL_EUR:+.2f} € ({(bm / START_CAPITAL_EUR - 1) * 100:+.1f}%) |",
        "",
        f"- Tranzacții închise: **{len(closed)}**, câștigătoare: **{len(wins)}**"
        + (f" ({len(wins) / len(closed) * 100:.0f}%)" if closed else ""),
        f"- Comisioane plătite: **{state['fees_paid']:.2f} €**",
        "",
        "## Poziții deschise",
        "",
    ]
    open_pos = [(s, p) for s, p in state["pairs"].items() if p["qty"] > 0]
    if open_pos:
        lines += ["| Monedă | Intrare | Acum | Stop | P/L nerealizat |", "|---|---:|---:|---:|---:|"]
        for s, p in open_pos:
            unreal = p["qty"] * (last_prices[s] - p["entry"])
            lines.append(f"| {s} | {p['entry']:.2f} | {last_prices[s]:.2f} | {p['stop']:.2f} | {unreal:+.2f} € |")
    else:
        lines.append("Niciuna — botul e pe cash.")
    lines += ["", "## Ultimele tranzacții", ""]
    if trades:
        lines += ["| Timp (UTC) | Monedă | Acțiune | Preț | Motiv | P/L |", "|---|---|---|---:|---|---:|"]
        for t in trades[-15:][::-1]:
            lines.append(f"| {t['timp_utc']} | {t['moneda']} | {t['actiune']} | {float(t['pret']):.2f} | {t['motiv']} | {t['pnl_eur']} |")
    else:
        lines.append("Încă nicio tranzacție — botul așteaptă un semnal.")
    lines += ["", "_Simulare. Nicio tranzacție reală nu a fost plasată._", ""]
    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- backtest rapid
def backtest():
    """Rulează aceeași strategie pe ultimele ~7,5 zile disponibile la Kraken."""
    all_candles = {sym: fetch_candles(pair) for sym, pair in PAIRS.items()}
    start_i = EMA_TREND
    first = {s: cs[start_i]["close"] for s, cs in all_candles.items()}
    state = new_state(first, all_candles["BTC"][start_i]["ts"])
    trades = []

    def log(ts, sym, action, px, qty, value, fee, reason, pnl):
        trades.append((iso(ts), sym, action, round(px, 2), reason, pnl))

    for sym, candles in all_candles.items():
        ind = indicators(candles)
        for i in range(start_i, len(candles)):
            step(state, sym, candles, ind, i, log)
    last = {s: cs[-1]["close"] for s, cs in all_candles.items()}
    for t in trades:
        print(*t)
    days = (len(all_candles["BTC"]) - start_i) * INTERVAL_MIN / 1440
    print(f"\nBacktest pe {days:.1f} zile: bot {equity(state, last):.2f} € | "
          f"buy & hold {benchmark(state, last):.2f} € | comisioane {state['fees_paid']:.2f} €")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "backtest":
        backtest()
    elif cmd == "report":
        s = load_state()
        if s:
            write_report(s, {sym: fetch_candles(p)[-1]["close"] for sym, p in PAIRS.items()})
    else:
        print(__doc__)
