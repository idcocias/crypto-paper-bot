"""
Bot AGRESIV de paper trading crypto — rulează în paralel cu bot.py (conservator).

Simulare cu 1000 EUR virtuali, 30 de zile, prețuri reale Kraken. Nicio tranzacție reală.

Profil de risc: acceptă o pierdere de până la ~70% din capital.
Strategie (fixă pe toată durata testului):
  - 4 monede, câte 250 EUR: BTC, ETH, SOL, DOGE (ultimele două sunt mult mai volatile).
  - Levier simulat 3x (tip futures/margin), cu comision pe valoarea totală a poziției,
    cost de finanțare (funding) și LICHIDARE dacă pierderea mănâncă marja.
  - Long ȘI short: urmărește trendul în ambele direcții.
  - Semnale mai rapide: EMA9 / EMA21, filtru de trend EMA100.
  - Stop-loss trailing mai strâns: 1,5 x ATR14.
  - Kill switch: dacă valoarea totală scade la 30% din start (-70%), botul se oprește
    definitiv până la finalul testului.

Utilizare:
  python bot_agresiv.py run | backtest | report
"""

import csv
import json
import os
import sys
import time

import bot as base   # refolosim descărcarea datelor și indicatorii din botul conservator

START_CAPITAL_EUR = 1000.0
TEST_DAYS = 30
PAIRS = {"BTC": "XBTEUR", "ETH": "ETHEUR", "SOL": "SOLEUR", "DOGE": "XDGEUR"}
LEVERAGE = 3.0
FEE_RATE = 0.0040          # 0,40% din valoarea poziției (cu levier!) la fiecare execuție
SLIPPAGE = 0.0010          # 0,10% — mai mare, monedele mici au lichiditate mai slabă
FUNDING_PER_CANDLE = 0.0001 / 32   # ~0,01% la 8 ore din valoarea poziției
LIQ_MARGIN_LEFT = 0.10     # lichidare când rămâne doar 10% din marjă
EMA_FAST, EMA_SLOW, EMA_TREND = 9, 21, 100
ATR_LEN, ATR_MULT = 14, 1.5
KILL_SWITCH = 0.30         # oprire la 30% din capitalul inițial
MIN_ORDER_EUR = 5.0

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data-agresiv")
STATE_FILE = os.path.join(DATA, "state.json")
TRADES_FILE = os.path.join(DATA, "trades.csv")
EQUITY_FILE = os.path.join(DATA, "equity.csv")
REPORT_FILE = os.path.join(ROOT, "RAPORT-AGRESIV.md")

iso = base.iso
TRADE_HEADER = ["timp_utc", "moneda", "actiune", "pret", "cantitate",
                "valoare_pozitie_eur", "comision_eur", "motiv", "pnl_eur"]


def indicators(candles):
    closes = [c["close"] for c in candles]
    return {"fast": base.ema_series(closes, EMA_FAST),
            "slow": base.ema_series(closes, EMA_SLOW),
            "trend": base.ema_series(closes, EMA_TREND),
            "atr": base.atr_series(candles, ATR_LEN)}


# ---------------------------------------------------------------- stare
def new_state(first_prices, start_ts):
    alloc = START_CAPITAL_EUR / len(PAIRS)
    return {"started_at": start_ts, "ends_at": start_ts + TEST_DAYS * 86400,
            "finished": False, "killed": False, "benchmark_start_prices": first_prices,
            "fees_paid": 0.0, "funding_paid": 0.0, "liquidations": 0,
            "pairs": {s: {"cash": alloc, "side": 0, "qty": 0.0, "entry": None,
                          "margin": 0.0, "stop": None, "liq": None, "last_ts": None}
                      for s in PAIRS}}


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


# ---------------------------------------------------------------- execuție simulată
def pair_value(p, price):
    """Valoarea unei monede = cash + marjă + profit/pierdere nerealizat (min. 0)."""
    if p["side"] == 0:
        return p["cash"]
    pnl = p["side"] * p["qty"] * (price - p["entry"])
    return p["cash"] + max(0.0, p["margin"] + pnl)


def open_pos(state, sym, side, price, ts, atr, reason, log):
    p = state["pairs"][sym]
    if p["cash"] < MIN_ORDER_EUR:
        return
    fill = price * (1 + SLIPPAGE * side)
    margin = p["cash"]
    notional = margin * LEVERAGE
    fee = notional * FEE_RATE
    margin -= fee
    notional = margin * LEVERAGE
    qty = notional / fill
    # prețul la care pierderea lasă doar 10% din marjă
    liq = fill - side * (margin * (1 - LIQ_MARGIN_LEFT)) / qty
    stop = fill - side * ATR_MULT * atr
    state["fees_paid"] += fee
    p.update(cash=0.0, side=side, qty=qty, entry=fill, margin=margin, stop=stop, liq=liq)
    log(ts, sym, "LONG" if side > 0 else "SHORT", fill, qty, notional, fee, reason, "")


def close_pos(state, sym, price, ts, reason, log, liquidated=False):
    p = state["pairs"][sym]
    if p["side"] == 0:
        return
    side = p["side"]
    if liquidated:
        fill, fee = price, 0.0
        pnl = -p["margin"] * (1 - LIQ_MARGIN_LEFT)
        state["liquidations"] += 1
    else:
        fill = price * (1 - SLIPPAGE * side)
        fee = p["qty"] * fill * FEE_RATE
        pnl = side * p["qty"] * (fill - p["entry"]) - fee
    back = max(0.0, p["margin"] + pnl)
    state["fees_paid"] += fee
    log(ts, sym, "INCHIDE " + ("LONG" if side > 0 else "SHORT"), fill, p["qty"],
        p["qty"] * fill, fee, reason, round(back - p["margin"], 4))
    p.update(cash=p["cash"] + back, side=0, qty=0.0, entry=None, margin=0.0,
             stop=None, liq=None)


def step(state, sym, candles, ind, i, log):
    c, p = candles[i], state["pairs"][sym]
    atr = ind["atr"][i]
    if atr is None or i < EMA_TREND:
        return
    fast, slow, trend = ind["fast"][i], ind["slow"][i], ind["trend"][i]
    pf, ps = ind["fast"][i - 1], ind["slow"][i - 1]
    cross_up = pf <= ps and fast > slow
    cross_dn = pf >= ps and fast < slow

    if p["side"] != 0:
        side = p["side"]
        # cost de finanțare pe lumânare
        f = p["qty"] * c["close"] * FUNDING_PER_CANDLE
        p["margin"] -= f
        state["funding_paid"] += f
        worst = c["low"] if side > 0 else c["high"]
        # 1) lichidare (verificată înaintea stop-ului: e cel mai rău caz)
        if (side > 0 and worst <= p["liq"] and p["liq"] >= p["stop"]) or \
           (side < 0 and worst >= p["liq"] and p["liq"] <= p["stop"]):
            close_pos(state, sym, p["liq"], c["ts"], "LICHIDARE", log, liquidated=True)
            return
        # 2) stop-loss
        if (side > 0 and worst <= p["stop"]) or (side < 0 and worst >= p["stop"]):
            px = min(p["stop"], c["open"]) if side > 0 else max(p["stop"], c["open"])
            # dacă gap-ul trece și de lichidare, e lichidare
            if (side > 0 and px <= p["liq"]) or (side < 0 and px >= p["liq"]):
                close_pos(state, sym, p["liq"], c["ts"], "LICHIDARE (gap)", log, liquidated=True)
            else:
                close_pos(state, sym, px, c["ts"], "stop-loss", log)
            return
        # 3) semnal opus -> închide și, dacă trendul permite, întoarce poziția
        if (side > 0 and cross_dn) or (side < 0 and cross_up):
            close_pos(state, sym, c["close"], c["ts"], "semnal opus", log)
        else:
            new_stop = c["close"] - side * ATR_MULT * atr
            p["stop"] = max(p["stop"], new_stop) if side > 0 else min(p["stop"], new_stop)
            return

    if p["side"] == 0:
        if cross_up and c["close"] > trend:
            open_pos(state, sym, +1, c["close"], c["ts"], atr, "EMA9 peste EMA21, trend sus", log)
        elif cross_dn and c["close"] < trend:
            open_pos(state, sym, -1, c["close"], c["ts"], atr, "EMA9 sub EMA21, trend jos", log)


def equity(state, prices):
    return sum(pair_value(p, prices[s]) for s, p in state["pairs"].items())


def benchmark(state, prices):
    alloc = START_CAPITAL_EUR / len(PAIRS)
    return sum(alloc * (1 - FEE_RATE) / (px0 * (1 + SLIPPAGE)) * prices[s]
               for s, px0 in state["benchmark_start_prices"].items())


def check_kill(state, prices, ts, log):
    if not state["killed"] and equity(state, prices) <= START_CAPITAL_EUR * KILL_SWITCH:
        for s in PAIRS:
            close_pos(state, s, prices[s], ts, "KILL SWITCH (-70%)", log)
        state["killed"] = True


# ---------------------------------------------------------------- rulare
def run():
    all_c = {s: base.fetch_candles(pair) for s, pair in PAIRS.items()}
    last = {s: cs[-1]["close"] for s, cs in all_c.items()}
    now_ts = min(cs[-1]["ts"] for cs in all_c.values())

    state = load_state()
    if state is None:
        state = new_state(last, now_ts)
        for s in PAIRS:
            state["pairs"][s]["last_ts"] = all_c[s][-1]["ts"]
        print(f"Test agresiv pornit la {iso(now_ts)} UTC")

    def log(ts, sym, action, px, qty, value, fee, reason, pnl):
        append_csv(TRADES_FILE, TRADE_HEADER,
                   [iso(ts), sym, action, round(px, 6), round(qty, 6),
                    round(value, 2), round(fee, 4), reason, pnl])
        print(f"{iso(ts)} {sym} {action} @ {px} ({reason})")

    if not state["finished"]:
        if not state["killed"]:
            for s, cs in all_c.items():
                ind, p = indicators(cs), state["pairs"][s]
                for i, c in enumerate(cs):
                    if c["ts"] <= p["last_ts"] or c["ts"] >= state["ends_at"]:
                        continue
                    step(state, s, cs, ind, i, log)
                    p["last_ts"] = c["ts"]
                    if state["killed"]:
                        break
                check_kill(state, last, now_ts, log)
                if state["killed"]:
                    break
        if now_ts >= state["ends_at"]:
            for s in PAIRS:
                close_pos(state, s, last[s], now_ts, "final test (30 zile)", log)
            state["finished"] = True

        append_csv(EQUITY_FILE, ["timp_utc", "bot_eur", "buy_and_hold_eur"] +
                   [f"pret_{s.lower()}" for s in PAIRS],
                   [iso(now_ts), round(equity(state, last), 2),
                    round(benchmark(state, last), 2)] + [last[s] for s in PAIRS])
    save_state(state)
    write_report(state, last)
    print(f"Agresiv: {equity(state, last):.2f} EUR | Buy & hold: {benchmark(state, last):.2f} EUR")


# ---------------------------------------------------------------- raport
def write_report(state, prices):
    eq, bm = equity(state, prices), benchmark(state, prices)
    trades = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            trades = list(csv.DictReader(f))
    closed = [t for t in trades if t["actiune"].startswith("INCHIDE")]
    wins = [t for t in closed if float(t["pnl_eur"] or 0) > 0]
    now = time.time()
    days = min(TEST_DAYS, max(0, (now - state["started_at"]) / 86400))
    if state["killed"]:
        status = "OPRIT de kill switch (-70%)"
    elif state["finished"]:
        status = "ÎNCHEIAT"
    else:
        status = f"în curs — ziua {days:.1f} din {TEST_DAYS}"
    S = START_CAPITAL_EUR
    L = ["# Raport — bot AGRESIV (levier 3x, long/short, 4 monede)", "",
         f"**Stare:** {status}  ",
         f"**Perioadă:** {iso(state['started_at'])} → {iso(state['ends_at'])} UTC  ",
         f"**Actualizat:** {iso(int(now))} UTC", "",
         "| | Valoare | Rezultat |", "|---|---:|---:|",
         f"| Bot agresiv | {eq:.2f} € | {eq - S:+.2f} € ({(eq / S - 1) * 100:+.1f}%) |",
         f"| Buy & hold (BTC/ETH/SOL/DOGE, câte 25%) | {bm:.2f} € | {bm - S:+.2f} € ({(bm / S - 1) * 100:+.1f}%) |",
         "",
         f"- Tranzacții închise: **{len(closed)}**, câștigătoare: **{len(wins)}**"
         + (f" ({len(wins) / len(closed) * 100:.0f}%)" if closed else ""),
         f"- Lichidări: **{state['liquidations']}**",
         f"- Comisioane: **{state['fees_paid']:.2f} €**, finanțare: **{state['funding_paid']:.2f} €**",
         "", "## Poziții deschise", ""]
    op = [(s, p) for s, p in state["pairs"].items() if p["side"] != 0]
    if op:
        L += ["| Monedă | Direcție | Intrare | Acum | Stop | Lichidare | P/L nerealizat |",
              "|---|---|---:|---:|---:|---:|---:|"]
        for s, p in op:
            u = p["side"] * p["qty"] * (prices[s] - p["entry"])
            L.append(f"| {s} | {'LONG' if p['side'] > 0 else 'SHORT'} | {p['entry']:.4f} | "
                     f"{prices[s]:.4f} | {p['stop']:.4f} | {p['liq']:.4f} | {u:+.2f} € |")
    else:
        L.append("Niciuna — botul e pe cash.")
    L += ["", "## Ultimele tranzacții", ""]
    if trades:
        L += ["| Timp (UTC) | Monedă | Acțiune | Preț | Motiv | P/L |", "|---|---|---|---:|---|---:|"]
        for t in trades[-15:][::-1]:
            L.append(f"| {t['timp_utc']} | {t['moneda']} | {t['actiune']} | {t['pret']} | {t['motiv']} | {t['pnl_eur']} |")
    else:
        L.append("Încă nicio tranzacție — botul așteaptă un semnal.")
    L += ["", "_Simulare. Nicio tranzacție reală nu a fost plasată._", ""]
    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(L))


def backtest():
    all_c = {s: base.fetch_candles(pair) for s, pair in PAIRS.items()}
    n = min(len(cs) for cs in all_c.values())
    all_c = {s: cs[-n:] for s, cs in all_c.items()}
    st = EMA_TREND
    state = new_state({s: cs[st]["close"] for s, cs in all_c.items()}, all_c["BTC"][st]["ts"])
    out = []
    log = lambda ts, sym, a, px, q, v, fee, r, pnl: out.append((iso(ts), sym, a, round(px, 4), r, pnl))
    for s, cs in all_c.items():
        ind = indicators(cs)
        for i in range(st, n):
            step(state, s, cs, ind, i, log)
    last = {s: cs[-1]["close"] for s, cs in all_c.items()}
    for t in out:
        print(*t)
    print(f"\nBacktest {(n - st) * 15 / 1440:.1f} zile: agresiv {equity(state, last):.2f} € | "
          f"buy & hold {benchmark(state, last):.2f} € | lichidări {state['liquidations']} | "
          f"comisioane {state['fees_paid']:.2f} €")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "backtest":
        backtest()
    elif cmd == "report":
        s = load_state()
        if s:
            write_report(s, {k: base.fetch_candles(p)[-1]["close"] for k, p in PAIRS.items()})
