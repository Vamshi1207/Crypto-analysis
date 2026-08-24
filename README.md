# Crypto Platform (Paper Desk)

Personal **practice** trading desk for Solana memecoins. It finds coins, runs a safety → forecast → cost → risk checklist, and simulates buys/sells with **fake money** so you can see whether ideas make ~$1 after fees — before any real wallet is touched.

**Live trading stays off** (`LIVE_TRADING=0`) until the readiness scorecard says practice results are strong enough. This repo is for personal use only.

---

## The Architecture (How it works today)

The platform runs three distinct trading engines and a robust parallel testing lab entirely in the background:

### 1. Sniper Engine (The Hybrid "Targeted Tape")
The crown jewel of the platform. Sniping requires sub-millisecond reactions without hitting strict API rate limits. To achieve this for free, the bot uses a **Hybrid Targeted Subscription model**:
- **Discovery:** Connects to the free `PumpPortal` WebSocket to instantly stream all global token creations.
- **Tracking:** Maintains a secondary connection to a free `Helius` RPC. When a token is discovered, it dynamically subscribes to *only* that specific token's `bonding_curve` address to track trades, and unsubscribes when the token expires. 
- **Result:** Reduces WebSocket data volume by 99.9%, bypassing all free-tier API bans while perfectly tracking the `unique_buyers` and momentum required for the Master Book.

### 2. Parallel Labs & The Master Book (`labs.py`)
Instead of testing one set of parameters at a time, the platform spins up isolated parallel environments ("Lanes").
- Each lane starts with its own isolated $1000 paper balance.
- **Tight Tape, Wide Tape, Dollar Tape:** Various experimental configurations running side-by-side on the exact same token feed.
- **The Master Book:** A consolidated strategy that dynamically inherits the most profitable parameters from all other active labs, providing a "best-of-the-best" equity curve visible directly on the Dashboard.

### 3. Scalping (AI Forecast / Swarm)
Continuously pulls DexScreener/Gecko candidates, runs Gate 0 safety, hydrates candles into the live buffer, and executes standard technical analysis & machine-learning forecasts. 
*(Note: Requires a paid RPC plan to bypass HTTP 429 rate limits when run at high volume).*

### 4. Arbitrage Engine (Price-gap)
Hunts for price-gap (buy low/sell high) opportunities across multiple DEX pools. It utilizes Jupiter to validate circular routes (SOL → mint → SOL).
*(Note: Jupiter recently restricted complex `restrict_intermediate_tokens=false` circular routes to paid API users).*

---

## Plain English: what you’re looking at on the Dashboard

| Term on the board | Normal meaning |
|---|---|
| **Paper / practice** | Fake dollars. No real crypto moves. |
| **Total value (equity)** | Practice account balance: cash + open trades. Starts at **$1000**. |
| **Sitting cash** | Fake money not currently in a trade. |
| **Profit locked in** | Wins/losses from trades already sold (fees included). |
| **Open gain/loss** | If you sold open trades *right now*, roughly how much you’d be up/down. |
| **Master Book** | The smartest parallel testing lane running our most proven sniper parameters. |
| **Quick trade (scalp)** | Buy a coin, sell soon; aim for about **$1 net** on a ~$40 size after fees. |
| **Expected edge** | Forecast upside **minus** fees. Positive is hopeful; it still must clear the ~$1 target. |
| **Chart agree? (TA)** | Do RSI/MACD-style signals support a buy, or fight it? |
| **Readiness score** | Checklist: “Is practice strong enough to *consider* real money?” |

Dashboard: open `http://127.0.0.1:8080/` (hard-refresh after UI changes). Hover the **?** next to each metric for a short explanation; the bottom **Plain English** section is the glossary.

---

## Quick start (laptop or overnight server)

One command after `.env` exists — Flask **auto-starts** (no manual `./dev-server.sh`).

```bash
# From repo root
cp .env.example .env
# Edit .env: keep LIVE_TRADING=0; drop in a fresh Helius/QuickNode free key.

docker compose up -d --build

# Dashboard + APIs (host 8080 → container 8000)
# open http://127.0.0.1:8080/   
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
```

### Overnight server deploy

```bash
git clone <your-repo-url> crypto_platform && cd crypto_platform
git checkout codex   # or your deploy branch
cp .env.example .env && nano .env   # paste secrets; LIVE_TRADING=0

docker compose up -d --build
docker compose ps    # python_server should be healthy / up
# Leave it running. Logs:
docker compose logs -f python_server
```

---

## Pipeline (gates)

Cheap checks first; expensive ones later:

| Step | Name | Job in plain English |
|---|---|---|
| Gate 0 | Safety | Skip obvious rugs / bad pools before spending brainpower. |
| Gate 1 | History | Enough candle history to forecast. |
| Forecast | Ensemble + calibration | “Where might price go?” with uncertainty. |
| Gate 2 | Edge vs costs | After fees/slip, is there ~$1 left on a $40 size? |
| Indicators | Chart agree? | Block buys that fight the tape. |
| Gate 3 | Risk / vibe | Skip blow-offs and messy disagreement. |
| Phase 4 | Paper execute | Fake buy/sell with stops, take-profit, cool-downs. |

---

## Safety rules (do not skip)

- Keep **`LIVE_TRADING=0`** until readiness looks ready **and** you’ve reviewed the paper log yourself.
- Do not commit `.env` (secrets). Use `.env.example` as the template.
- Free-tier APIs (DexScreener, Helius free, etc.) are rate-limited — discover backs off; don’t burn paid credits until paper EV is clear.
- This is **not** financial advice. Memecoins can go to zero; paper success does not guarantee live success.

---

## Repo map

```text
crypto_platform/
  .env.example          # All tunables documented
  docker-compose.yml    # python_server on :8080
  python_server/
    server.py           # Flask app + workers wiring
    templates/dashboard.html
    decision/           # Gates, paper, discover, swarm, arb, readiness
      snipe_feed.py     # PumpPortal + Helius hybrid data pipeline
      labs.py           # Parallel strategy books and Master Book
    store/              # Daily JSONL logs (local runtime data)
```
