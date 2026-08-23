# Crypto Platform (paper desk)

Personal **practice** trading desk for Solana memecoins. It finds coins, runs a safety → forecast → cost → risk checklist, and simulates buys/sells with **fake money** so you can see whether ideas make ~$1 after fees — before any real wallet is touched.

**Live trading stays off** (`LIVE_TRADING=0`) until the readiness scorecard says practice results are strong enough. This repo is for personal use only.

---

## Plain English: what you’re looking at

| Term on the board | Normal meaning |
|---|---|
| **Paper / practice** | Fake dollars. No real crypto moves. |
| **Total value (equity)** | Practice account balance: cash + open trades. Starts at **$1000**. |
| **Sitting cash** | Fake money not currently in a trade. |
| **Profit locked in** | Wins/losses from trades already sold (fees included). |
| **Open gain/loss** | If you sold open trades *right now*, roughly how much you’d be up/down. |
| **Quick trade (scalp)** | Buy a coin, sell soon; aim for about **$1 net** on a ~$40 size after fees. |
| **Price-gap trade (arb)** | Same coin cheaper in one pool than another; paper “buy low / sell high.” Books **conservative** PnL (half-gap stress + Jupiter impact). Timer cool-downs are **off by default** — if analysis still clears, it may re-enter. |
| **Expected edge** | Forecast upside **minus** fees. Positive is hopeful; it still must clear the ~$1 target. |
| **Chart agree? (TA)** | Do RSI/MACD-style signals support a buy, or fight it? |
| **Active hunt / Watch only** | Hunting = bot may trade. Watch only = on the board but paused/cooling. |
| **Readiness score** | Checklist: “Is practice strong enough to *consider* real money?” |
| **Reset board** | Archive today’s logs and start a clean practice window from $1000. |
| **Pipeline** | Ordered checks: safety → forecast → costs → risk → paper execute. |

Dashboard: open `http://127.0.0.1:8080/` (hard-refresh after UI changes). Hover the **?** next to each metric for a short explanation; the bottom **Plain English** section is the glossary.

---

## What’s running today

Three background workers (daemon threads, toggled in `.env`):

1. **Discover (finder)** — pulls DexScreener / Gecko candidates, runs Gate 0 safety, hydrates candles into the live buffer. Splits **trade** vs **observe** rosters; cools tokens that keep failing “no edge.”
2. **Swarm (auto-trader)** — continuously runs decide → paper buy/sell on tradeable tokens.
3. **Arb (price-gap)** — paper cross-pool opportunities; optional Jupiter quote check before booking fills.

Supporting pieces:

- **Indicators** (`talipp`) feed a post–Gate-2 alignment score (can veto conflicting buys).
- **Readiness** (`GET /readiness`) scores sample size + average profit (EV) before any live flag.
- **Session reset** (`POST /session/reset` or **Reset board** on the UI) archives today’s JSONL logs and clears paper/cool-offs.

Goal of the practice loop: prove the pipeline can clear ~$1 **net** after realistic fees/slip before spending on paid infra or turning `LIVE_TRADING=1`.

---

## Quick start (laptop or overnight server)

One command after `.env` exists — Flask **auto-starts** (no manual `./dev-server.sh`).

```bash
# From repo root
cp .env.example .env
# Edit .env: keep LIVE_TRADING=0; set HELIUS_API_KEY if you have one (optional for paper)

docker compose up -d --build

# Dashboard + APIs (host 8080 → container 8000)
# open http://127.0.0.1:8080/   # or http://YOUR_SERVER_IP:8080/
curl -s http://127.0.0.1:8080/health | python3 -m json.tool
curl -s http://127.0.0.1:8080/readiness | python3 -m json.tool
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

Workers (discover / swarm / arb) start from `.env` flags inside the process — no extra setup.
Paper JSONL lives under `python_server/store/` (bind-mounted, survives recreate).

After changing `.env`:

```bash
docker compose up -d --force-recreate
```

After code pull on the server:

```bash
git pull
docker compose up -d --build
```

Flask does **not** auto-reload HTML with debug off — recreate/restart after template edits, then hard-refresh the browser.

### Chrome extension (optional)

`extension/` scrapes Axiom candle/stats into the Flask ingest path. Headless discover can run without it; the extension is useful for richer per-token tape when you’re on Axiom.

### Tests

```bash
docker compose exec -T python_server python -m pytest tests/ -q
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

Every decision / paper fill / pipeline event is append-only JSONL under `python_server/store/` (decisions, paper, outcomes, pipeline, safety). **Reset board** renames today’s files aside so readiness isn’t polluted by an earlier messy session.

---

## Important API routes

| Route | Purpose |
|---|---|
| `GET /` | Paper desk dashboard |
| `GET /health` | Workers + token counts + live flag |
| `GET /readiness` | Go / almost / keep-practice scorecard |
| `GET/POST /portfolio` | Practice account; POST can set kill switch |
| `POST /session/reset` | Clean monitoring window |
| `GET/POST /discover`, `/swarm`, `/arb` | Worker status / start-stop |
| `GET/POST /decide`, `/safety`, `/execute` | Single-token path |
| `GET /pipeline`, `/scoreboard`, `/calibrate` | Observability / forecast fit |

---

## Safety rules (do not skip)

- Keep **`LIVE_TRADING=0`** until readiness looks ready **and** you’ve reviewed the paper log yourself.
- Do not commit `.env` (secrets). Use `.env.example` as the template.
- Free-tier APIs (DexScreener, Helius free, etc.) are rate-limited — discover backs off; don’t burn paid credits until paper EV is clear.
- This is **not** financial advice. Memecoins can go to zero; paper success does not guarantee live success.

---

## Repo map

```
crypto_platform/
  .env.example          # All tunables documented
  docker-compose.yml    # python_server on :8080
  extension/            # Chrome → Axiom ingest
  plans/                # Longer design notes
  python_server/
    server.py           # Flask app + workers wiring
    indicators.py       # talipp indicator stack
    templates/dashboard.html
    decision/           # Gates, paper, discover, swarm, arb, readiness, session
    store/              # Daily JSONL logs (local runtime data)
    tests/
```

Deeper design history: [`plans/implementation_plan.md`](plans/implementation_plan.md).

---

## Current status (snapshot)

- **Mode:** paper only; live flag off.
- **Desk UI:** brighter slate dashboard with account metrics, open/finished trades, hunt/watch lists, readiness chip, **Reset board**, and a plain-English glossary.
- **Workers:** discover + swarm + arb configurable; indicator gate after cost edge.
- **Success bar:** enough finished scalps with non-negative average P/L (and related readiness checks) before considering live or paid speed infra.
