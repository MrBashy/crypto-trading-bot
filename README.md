# Crypto Day-Trading Bot (proof of concept)

A small, readable crypto trading bot with a password-protected web dashboard.
It **paper trades** (simulated, no money) out of the box and can be switched to
**live trading** on a real exchange from the dashboard.

> ⚠️ This is a proof of concept. Simple strategies like this one usually **lose money
> after fees**: check the Backtest tab. Only ever trade money you can afford to lose.

## What it does

- Watches live prices on a crypto exchange (Kraken by default, via [ccxt](https://github.com/ccxt/ccxt))
- **Strategy:** EMA crossover with an RSI filter
  - **Buy** when the fast EMA crosses above the slow EMA on a closed candle (and RSI isn't "overbought")
  - **Sell** on whichever comes first: stop-loss, trailing stop, take-profit, or the fast EMA crossing back below
- **Risk controls:** trade size as % of account, hard max per trade, max open positions,
  stop-loss, take-profit, trailing stop, and a **daily loss limit** that sells everything and stops for the day
- **Dashboard:** live candlestick chart with the bot's buys and sells, the full TradingView chart,
  positions, trades, activity log, account value chart, settings, backtester, and a paper/live switch

## Dashboard

Open `http://<this-machine's-IP>:8080` and log in with your dashboard password.

| Tab | What it's for |
|---|---|
| **Dashboard** | Account value, P&L, chart (*Bot chart* shows the bot's trades; *TradingView* is the full TradingView chart), open positions with a **Sell now** button, recent trades, activity log |
| **Backtest** | Runs your current settings over past candles so you can judge them before risking anything |
| **Settings** | Every variable (pairs, candle size, EMAs, RSI, trade size, stop-loss, etc.), each with a short explanation. Changes apply immediately |
| **Paper / Live** | Switch between paper and live trading, save and test exchange API keys, reset the paper account |
| **Password** | Change the dashboard password |

**Header buttons**

- **Start**: the bot begins opening new trades.
- **Stop**: no *new* trades. Open positions stay protected by their stop-loss and take-profit.
- **Emergency: sell all & stop**: sells every open position at market price and stops.

## Going live (real money)

1. On your exchange's website, create an API key with **view balance + trade** permission only.
   **Never enable withdrawals.** Most exchanges also let you restrict the key to this machine's IP.
2. In **Paper / Live**, paste the key and secret, press **Save keys**, then **Test connection**.
3. Press **Stop**, close any open paper positions, then **Switch to LIVE trading…** and type `LIVE`.
4. Review **Settings** (especially *Max trade size*, *Stop-loss*, *Daily loss limit*), then press **Start**.

Switching back to paper uses the same button in reverse. The bot only manages the coins **it** bought;
it never touches other holdings in your account.

**Important things to know about live mode**

- Stop-loss and take-profit are carried out **by the bot** (they are not orders resting on the exchange),
  so they only work while the bot is running. If this machine is off, your positions are unprotected.
- The daily loss limit compares against your account value at 00:00 UTC. Withdrawing money from the
  exchange during the day counts as a "loss" for this check.
- Orders are market orders. Exchange minimum order sizes apply; trades below them are skipped and logged.
- Live order handling has been tested against a simulated exchange in the test suite, but not with real
  money. Start with a small *Max trade size*.

## Running it

### First-time setup
```bash
cd crypto-trading-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot.cli set-password      # choose your dashboard password
```

### Run as a background service (recommended, starts on boot)
```bash
./deploy/install-service.sh
```
Useful commands:
```bash
systemctl --user status crypto-bot      # is it running?
systemctl --user restart crypto-bot     # restart (e.g. after updating the code)
systemctl --user stop crypto-bot        # stop completely
journalctl --user -u crypto-bot -f      # follow the logs
```

### Or run it in the foreground
```bash
.venv/bin/python run.py
```

Environment variables: `BOT_HOST` (default `0.0.0.0`, so it's reachable from your local network),
`BOT_PORT` (default `8080`), `BOT_DATA_DIR` (default `./data`).

### Forgot the password?
```bash
.venv/bin/python -m bot.cli set-password
```

## Security notes

- The dashboard is meant for your **local network**. It uses plain HTTP, so don't expose port 8080
  to the internet. For remote access use a VPN (e.g. Tailscale) or an SSH tunnel:
  `ssh -L 8080:localhost:8080 user@this-machine`, then open `http://localhost:8080`.
- Login is locked for 5 minutes after 5 wrong passwords. Passwords are stored as scrypt hashes.
- API keys and the password hash live in `data/secrets.json` (file mode 600, git-ignored).
- The `data/` folder is git-ignored and is never committed.

## How it works (code tour)

```
run.py               start the web server + trading engine
bot/config.py        all settings, defaults and validation
bot/strategy.py      the trading rules (shared by live trading and the backtester)
bot/indicators.py    EMA and RSI in plain Python
bot/engine.py        the trading loop: prices → protect positions → daily limit → signals → trades
bot/brokers.py       PaperBroker (simulated fills) and LiveBroker (real exchange orders)
bot/market.py        public market data (prices, candles) via ccxt
bot/backtest.py      backtester (fills at next candle open, fees + slippage, stop-first)
bot/web.py           dashboard API, login and sessions
bot/db.py            SQLite storage (positions, trades, orders, equity, log)
web/, static/        dashboard pages, JavaScript, styles, TradingView Lightweight Charts
deploy/              systemd service installer
tests/               automated tests
```

A few design rules that keep it honest:

- Signals only use **closed** candles, never the one still forming.
- Each candle can trigger at most one action, and this survives restarts.
- Backtests fill at the **next** candle's open, charge fees and slippage, and assume the stop is hit
  first when a candle touches both stop and target.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Credits

Charts use [TradingView Lightweight Charts™](https://www.tradingview.com/lightweight-charts/)
(Apache 2.0, see `static/LIGHTWEIGHT-CHARTS-LICENSE.txt`) and the TradingView Advanced Chart widget.
