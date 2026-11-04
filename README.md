# Orblex Industries

An AI analyst that proposes swing trades, and Python rules that decide what gets through.

Each weekday, a pipeline turns market data into trade plans held for 1 to 10 days. Claude reads the context and proposes a call with an entry, a stop and a target. Python code owns every number, cap and order rule, and can only make a trade smaller or stop it. A person approves every order.

**Live demo:** the dashboard on made-up tickers and numbers, with no login. See [Run the demo](#run-the-demo).

> **Not financial advice.** This is a personal project, shared so others can learn from the design. It has not shown a proven edge, and real trading can lose money. If you connect a brokerage account, you are responsible for every order it places.

---

## How a trade gets made

1. **Market data:** prices, earnings, options flow and a rule-based market regime (bull, choppy, correction, bear).
2. **Analyst:** Claude reads it per ticker and proposes BUY, SELL or HOLD, with entry, stop, target and horizon.
3. **Swing rules** (`swing_policy.py`): clamp the levels, block entries before earnings, and turn a BUY into a HOLD below 1.5R reward:risk.
4. **Risk rules** (`risk_rules.py`): a 1–10 score from volatility, earnings and the regime. 8 or more excludes the ticker.
5. **Sizing and guard** (`trade_plan.py`, `trade_guard.py`): whole shares from a fixed % of equity at risk, then every limit: positions, open risk, entries per day, halts, and no same-day re-buys.
6. **You:** approve on the dashboard, then place the order, or let the optional executor send it to your broker's approval queue.

Limits only tighten: a daily mandate you set can make a code ceiling stricter, never looser. Missing data, an unreadable ledger or a stale price blocks the order.

## What's in the repo

| Area | Files |
|---|---|
| Pipeline | `run_all.py` and the `run_*.py` stages |
| Rules | `swing_policy.py`, `risk_rules.py`, `trade_guard.py`, `exec_limits.py`, `instrument_policy.py` |
| Backtests | `swing_backtest.py`, `swing_screen_backtest.py`, `replay_executor.py` |
| Executor (off by default) | `executor.py`, `rh_client.py` (read-only), `rh_trader.py` (the only module that can send), `run_executor.py` |
| Dashboard | `src/` (React and Vite), `api/run-pipeline.js` |
| Playground | `playground.py` (the engine), `api/playground.py` (the server function), `src/demo/Playground.jsx`, `playground_schema.sql` |
| Database | `*_schema.sql` (Supabase Postgres) |
| Scheduling | `jobs.py`, `deploy/github-workflows/`, `automation_schema.sql` |
| Docs | `docs/REFERENCE.md` (how everything works), `EXECUTOR_DESIGN.md` (design log) |

---

## Run the demo

The demo is the same dashboard on sample data. It needs no database, keys or account.

```bash
npm install
npm run dev:demo
```

Open http://localhost:5174. `npm run build:demo` builds it into `dist-demo/` for any static host.

## The playground

The demo's PLAYGROUND tab runs the real analyst and rules on a chart you shape: price, 20-day trend, the last 5 days, volatility, volume, earnings date, market regime and your own headlines. You make a free account, enter your own Claude API key, and watch each step: Claude's call, the swing rules' corrections, the risk score, and the sized order or the reason it was blocked.

- **Your key:** kept in the browser tab only. It is sent with each run, used once by `api/playground.py`, and never stored, logged or returned.
- **Your account:** holds only your saved runs, and you can delete it from the tab.
- **Limits:** 20 runs per account per day by default, and each costs a few cents of your Claude usage.

### Host the playground

1. Create a **new** Supabase project for playground accounts, separate from any project holding real trades, and run `playground_schema.sql` in it. Turn on email sign-in with "Confirm email", and add your demo address as the Site URL.
2. Create a Vercel project from this repository. `vercel.json` builds the demo and deploys `api/playground.py`.
3. Set its environment variables: `VITE_PLAYGROUND_SUPABASE_URL` and `VITE_PLAYGROUND_SUPABASE_ANON_KEY` (public, built into the page), `PLAYGROUND_SUPABASE_SERVICE_KEY` (server only), and optionally `PLAYGROUND_DAILY_RUNS`.

## Run it with your own accounts

You need Python 3.10+, Node 18+, a [Supabase](https://supabase.com) project and a [Claude API key](https://console.anthropic.com). Robinhood is optional.

1. **Configure.** Copy `.env.example` to `.env` and fill in Supabase and `ANTHROPIC_API_KEY`. You pay for your own Claude usage.
2. **Create the tables.** In the Supabase SQL editor, run the schema files in the order listed in `docs/REFERENCE.md` (Database), then `private_access_schema.sql` last. Add your own email to `trading_admins`, as `approval_schema.sql` shows.
3. **Install.** `pip install -r requirements.txt` and `npm install`.
4. **Try it without writing anything.** `python run_all.py --dry-run` computes every stage and saves nothing. `python run_trade_plan.py plan --paper 5000 --dry-run` sizes a plan with no broker at all.
5. **Run the dashboard.** `npm run dev`, then sign in with the email you added.

### Optional: connect Robinhood

`rh_client.py` talks to Robinhood's official agentic trading MCP server over OAuth. You sign in through your own browser and phone, and the token stays on your machine, or encrypted in your Supabase if you run the scheduled jobs.

```bash
python rh_client.py login
python rh_client.py check
```

Set `RH_AGENTIC_ACCOUNT` (and `RH_MAIN_ACCOUNT` if you use the weekly advice book) in `.env`. With either unset, the client refuses every account call. `rh_client.py` can only read. Sending orders needs `rh_trader.py`, the repository variable `EXECUTOR_LIVE=1`, and Robinhood's own approval setting on, so every order still waits for your tap in the Robinhood app.

### Optional: run it on a schedule

Copy `deploy/github-workflows/*.yml` into `.github/workflows/` in your fork and add the secrets they read. `automation_schema.sql` sets up Supabase `pg_cron` to dispatch them on time, using two Vault secrets: a GitHub token and your `owner/repo`. See `docs/REFERENCE.md` (A trading day).

## Safety design

- **The model proposes, Python disposes.** No model output can set a number or loosen a limit.
- **Fail closed.** Unknown state, a missing mandate or an unreadable count refuses the order. Exits stay allowed, because they reduce risk.
- **Idempotent orders.** Each order carries a fixed id, so a retry or a crash can't send it twice.
- **Private by default.** `private_access_schema.sql` gives the public key no access to any table, and the dashboard needs a sign-in.

## Contributing

Issues and pull requests are welcome. Please never include keys, tokens, account numbers or real trades in an issue, a pull request or a screenshot. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
