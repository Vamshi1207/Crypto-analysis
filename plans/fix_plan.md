# Fix Plan — Automated Trading Desk Review Remediation

Date: 2026-10-11. Source: code review of the paper trading engines
(`paper.py`, `trenches.py`, `jev.py`, `sizing.py`, `exec_sim.py`, `labs.py`,
`costs.py`, `config.py`) plus the test suite.

Baseline test run: **304 passed, 20 skipped, 2 deselected** (inside the dev
container, `python -m pytest -m "not live"`).

---

## Sequencing rationale

1. **Repo hygiene first** — zero risk, unblocks clean diffs for everything else.
2. **Mechanical bugs second** — test-guarded, no behavior debate.
3. **Paper-book honesty third** — small code, but two design decisions.
4. **Validation harness last** — the real work; cleaner on top of the first three.

---

## Phase 0 — Repo hygiene (no behavior change)

### 0.1 Remove `logs.txt` from tracking
- **File:** `.gitignore`
- Add `logs.txt`, `*.log`, `logs/`; `git rm --cached logs.txt`.
- **Verify:** `git ls-files | grep -i logs` returns nothing.

### 0.2 Remove untracked scratch / tooling artifacts
- **Files:** `get_vaults.py`, `skills-lock.json`, `.agents/`, `_archive/`,
  `python_server/.Trash-0/`.
- `get_vaults.py` is a debug script — delete or move to `scratch/`. `.agents/`
  and `skills-lock.json` are agent-harness artifacts — keep out of this repo or
  add to `.gitignore`. `.Trash-0/` is junk.
- **Verify:** `git status` clean apart from intended edits.

### 0.3 Unify the SOL fallback constant
- **Files:** `decision/trenches.py:756-762` (`_sol_usd` hardcodes `150.0`),
  `decision/engines/arb/*` (reads `ARB_SOL_USD_FALLBACK=140`).
- Introduce `SOL_USD_FALLBACK = _env_float("SOL_USD_FALLBACK", 140.0)` in
  `config.py`; make `_sol_usd()` and the arb path both read it. Update
  `.env.settings` to define it once; keep `ARB_SOL_USD_FALLBACK` as an override
  alias for one release, then remove.
- **Verify:** grep for `150.0` / `ARB_SOL_USD_FALLBACK`; one source of truth.

---

## Phase 1 — Mechanical correctness bugs

### 1.1 Remove the `sys.modules` self-replacement shims
- **Files:** `decision/arb.py`, `pumpfun.py`, `snipe_feed.py`, `snipe_desk.py`,
  `swarm.py`, `discover.py`, `local_amm.py` (each does
  `sys.modules[__name__] = _impl`).
- Already caused the `session.py:85-91` `try/except AttributeError` workaround.
- **Steps:** inventory every import site with grep; replace
  `from decision import arb` → `from decision.engines.arb import engine as arb`
  (or import specific symbols); delete each shim file; remove the
  `session.py` fallback. Do **one shim per commit**, full suite between each.
- **Risk:** medium (imports are load-bearing).
- **Verify:** `grep -rn "sys.modules\[__name__\]"` returns nothing; suite green.

### 1.2 Remove the duplicate `using_public_rpc`
- **File:** `config.py:87` and `config.py:105` (identical). Keep one, delete the other.

### 1.3 Fix the misleading `sniper_seen` metric
- **File:** `trenches.py:984` — `return added + len(_watches), opens, ...`
- Return a proper split: `creates_added`, `sniper_seen` (cumulative distinct
  mints this tick), `watches_remaining`. Update `TrenchState`, `status()`,
  and `scan_once` consumers.

### 1.4 Fix the `packet.py` slippage round-trip
- **File:** `packet.py:105` — `slip = price_impact_pct * 100.0  # pct → bps`
  (round-trips through `slippage_bps/100`, confusing).
- Pass `price_impact_pct` directly to `_est_round_trip_cost_pct`; drop the
  bps conversion.

### 1.5 Guard `_cur()` lazy book creation against the race
- **File:** `paper.py:227-233`. Wrap create-if-missing in `with _lock:`
  (`RLock`, safe and cheap).
- Add a concurrency smoke test.

---

## Phase 2 — Paper-book honesty (two decisions required)

### 2.1 Make `PAPER_MAX_LOSS_PCT` stop masking real rugs
- **Files:** `paper.py:61-63`, `paper.py:1700-1707` (`_resolve_stop_exit` mark
  cap), `fill_audit.py`.
- **Decision — recommended B:** only apply the cap when the gap is *unconfirmed
  by a second venue*. Reuse `fill_audit`'s cross-venue comparison: a mark that
  gaps > `ENTRY_CONFIRM_PCT` but has no second source confirmation is a ghost
  (cap applies); Dex + Gecko (or tape) agreement books the full mark.
  - A) raise cap to 100 (simpler but re-introduces the single-bad-print loss).
- **Verify:** new test `test_paper_stop_confirmed_rug.py`.

### 2.2 Make `exec_sim` a credible (if still coarse) model
- **Files:** `exec_sim.py`, `paper.py` (`_exec_resolver_loop`,
  `drain_pending_buys`/`_exits`), `server.py`.
- Gaps: the latency window usually holds only `signal` + one Dex poll
  (pricefeed ticks every 2s); no adverse-selection/sandwich term.
- **Steps:** (1) denser marks — resolver polls Dex for *pending buys* during the
  window, not just waiting exits; (2) size-vs-liquidity adverse adder
  (`EXEC_SIM_ADVERSE_BPS`) applied on top of the worst mid; (3) document that
  this is a submit→land lower bound, not MEV/JITO modeling.
- **Verify:** extend `test_exec_sim.py`.

---

## Phase 3 — Statistical validation harness (the real work)

Principle: **freeze parameters, then test on tape the parameters have never
seen.** This is where the "+$2.30/trade" claim gets supported or killed.

### 3.1 Freeze and hash lane parameters
- **File:** `labs.py`, new `experiments.py`.
- Add a SHA-256 `config_hash` of `asdict(lane)` (sorted) to lane results,
  `lane_board()`, and `pipeline_log` open/close. Commit current configs to a
  tracked `experiments.json`; the harness refuses to aggregate across hashes.

### 3.2 Build a walk-forward replay runner
- **Files:** new `decision/validation.py`; reuse `replay.py`, `store/` JSONL,
  `test_replay_continuous.py`.
- Replay archived tape in strict chronological windows; each frozen lane runs
  each window on its own $1000 book (`use_lane`). Output per-lane/per-window:
  `n`, `win_rate`, `expectancy_usd`, `stddev`, `95% CI`, `max_drawdown`,
  `config_hash`. Train/validation split is **by time, not shuffle**.

### 3.3 Replace "promote the best lane" with a statistical gate
- **Files:** `labs.py` (`primary_lane_id`), `readiness.py`, `scoreboard.py`.
- Add readiness bars: min `n` per lane, out-of-sample expectancy significance
  (p<0.05), max drawdown. `primary_lane_id` promotes only lanes that clear the
  gate on holdout windows — not "most open lots".

### 3.4 Re-measure all headline numbers
- Re-run the harness and update `labs.py:43-45`, `trenches.py:80-81`,
  `.env.settings` comments, and `README.md` to report **out-of-sample** numbers
  tagged with `config_hash`. Label or delete old in-sample claims.

---

## Phase 4 — Documentation alignment

### 4.1 Fix the README "Master Book" claim
- **File:** `README.md:22-23`.
- **Decision — recommended A:** rewrite to describe reality: parallel isolated
  books promoted only on out-of-sample significance. The auto-"inherits the most
  profitable parameters" framing is the overfitting mechanism; do not build it.

### 4.2 Document the frozen-parameter / validation protocol
- Extend README "Safety rules" or add `TESTING.md`: frozen params, config-hash
  requirement, forward-only validation protocol.

---

## Order of execution

1. Phase 0 (0.1 → 0.3).
2. Phase 1.2–1.5 (low-risk mechanical).
3. Phase 1.1 (shim removal) — one shim per commit, full suite between each.
4. Phase 2.1 (needs the fill_audit confirmation piece) and 2.2.
5. Phase 3 (3.1 → 3.4).
6. Phase 4.

## Risks

- **1.1 is the riskiest single change.** Import refactors across 7 shims can
  break boot; do it last in Phase 1, one shim at a time, with
  `python -c "import server"` smoke tests in addition to pytest.
- **2.1 depends on `fill_audit` reliability.** Require Dex *and* Gecko agreement
  within a tolerance before trusting "confirmed".
- **3.x changes scorecard output, not just internals.** Readiness numbers will
  get worse (honest) — that is the point; do not "fix" it by loosening gates.
