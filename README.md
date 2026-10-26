# Bot paper trading crypto — test 30 de zile

Simulare cu **1000 € virtuali** pe BTC și ETH (contra EUR), pe prețuri reale de la Kraken.
**Nu plasează nicio tranzacție reală** și nu are acces la niciun cont: citește doar prețuri publice.

Rezultatul curent se vede în **[RAPORT.md](RAPORT.md)**, actualizat la fiecare 15 minute.

## Strategia (fixă — nu se schimbă în timpul testului)

- Capital împărțit 50/50 între BTC și ETH, doar poziții long.
- **Intrare:** EMA20 trece peste EMA50, iar prețul e peste EMA200.
- **Ieșire:** EMA20 trece sub EMA50 sau e atins stop-loss-ul (2 × ATR14, trailing).
- Lumânări de 15 minute.
- Costuri incluse: comision 0,40% pe execuție plus slippage 0,05%.
- Termen de comparație: „buy & hold”, adică 500 € în BTC și 500 € în ETH din prima zi, fără nicio altă mișcare.

## Pornire (o singură dată)

1. Pe github.com creezi un repository nou, **privat**, de exemplu `crypto-paper-bot`.
2. Urci fișierele din acest folder, inclusiv folderul `.github`.
3. Deschizi tab-ul **Actions** și activezi workflow-urile dacă GitHub cere asta.
4. Intri în **Paper trading bot**, apeși **Run workflow**, iar testul pornește din acel moment.

După asta botul rulează singur la fiecare 15 minute, timp de 30 de zile.

## Fișiere

- `bot.py` — botul. Comanda `python bot.py backtest` rulează un test rapid pe ultimele ~7 zile.
- `data/trades.csv` — jurnalul fiecărei tranzacții simulate.
- `data/equity.csv` — evoluția valorii, cu bot și buy & hold, la fiecare rulare.
- `data/state.json` — starea botului. Nu-l edita în timpul testului.

## Cum citești rezultatul

Dacă după 30 de zile botul nu bate buy & hold după costuri, strategia nu merită bani reali.
Chiar dacă îl bate, 30 de zile reprezintă un eșantion mic. Următorul pas ar fi încă o lună de test, nu bani pe masă.
