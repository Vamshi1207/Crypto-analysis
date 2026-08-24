"""Network access to free Solana data sources.

Every function raises `SourceError` on failure rather than returning a
placeholder, so the caller can record the source as unavailable and degrade the
verdict instead of treating missing data as a pass.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Optional

import httpx

from decision import cache
from decision.config import (
    SETTINGS,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WRAPPED_SOL_MINT,
)

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"
DEXSCREENER_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"
GECKO_TRENDING_URL = f"{GECKO_BASE_URL}/networks/solana/trending_pools"

# Jupiter has migrated hosts more than once; try the current one first and fall
# back rather than hard-failing the sellability probe.
JUPITER_QUOTE_URLS = (
    "https://lite-api.jup.ag/swap/v1/quote",
    "https://api.jup.ag/swap/v1/quote",
)

_rpc_ids = itertools.count(1)


class SourceError(RuntimeError):
    """A data source could not answer. Distinct from 'answered, and it's bad'."""


class NoRouteError(RuntimeError):
    """Jupiter answered successfully and there is no route. This is a signal."""


_tls = threading.local()


def _http() -> httpx.Client:
    client = getattr(_tls, "client", None)
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(SETTINGS.http_timeout_s),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "crypto-platform/decision-engine"},
            follow_redirects=True,
        )
        _tls.client = client
    return client


# Kept deliberately small: this is in the hot path of a trading decision, and
# the public RPC refuses expensive methods outright rather than throttling
# transiently, so extra attempts buy latency and nothing else.
RPC_RETRIES = 2
RPC_BACKOFF_S = 0.25

# Helius reports index pressure in the JSON-RPC error body with HTTP 200, so
# these have to be matched on message text rather than status code.
_TRANSIENT_RPC_ERRORS = ("overloaded", "try again", "timed out", "timeout")

# `getTokenLargestAccounts` refuses to run at all past a few million holders.
# That is a structural answer about the token, not a failure to answer.
_TOO_MANY_ACCOUNTS = "too many accounts requested"


class TooManyAccountsError(RuntimeError):
    """The account set is too large to rank. Implies a very wide holder base."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _rpc(method: str, params: list[Any], timeout_s: Optional[float] = None) -> Any:
    """One JSON-RPC call, retrying only on throttling and transient server load.

    Retries are capped tightly: this sits in the hot path of a trading decision,
    and waiting is worse than reporting the source as unavailable and degrading
    the verdict.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": next(_rpc_ids),
        "method": method,
        "params": params,
    }
    hint = (
        " (public RPC is rate limited; set HELIUS_API_KEY)"
        if SETTINGS.using_public_rpc
        else ""
    )
    last_error = "unknown error"

    for attempt in range(RPC_RETRIES):
        try:
            response = _http().post(
                SETTINGS.rpc_endpoint,
                json=payload,
                timeout=timeout_s or SETTINGS.http_timeout_s,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"{method}: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"HTTP {response.status_code}{hint}"
            if attempt < RPC_RETRIES - 1:
                time.sleep(RPC_BACKOFF_S * (2**attempt))
                continue
            raise SourceError(f"{method} {last_error}")

        if response.status_code >= 400:
            raise SourceError(f"{method} HTTP {response.status_code}{hint}")

        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError(f"{method}: malformed JSON ({exc})") from exc

        if "error" in body:
            error = body["error"]
            message = str(error.get("message", error) if isinstance(error, dict) else error)
            lowered = message.lower()

            if _TOO_MANY_ACCOUNTS in lowered:
                raise TooManyAccountsError(message)

            if any(token in lowered for token in _TRANSIENT_RPC_ERRORS):
                last_error = message
                if attempt < RPC_RETRIES - 1:
                    time.sleep(RPC_BACKOFF_S * (2**attempt))
                    continue

            raise SourceError(f"{method}: {message}")

        return body.get("result")

    raise SourceError(f"{method}: {last_error}")


def fetch_mint_account(mint: str) -> dict[str, Any]:
    """Parsed SPL mint state: authorities, supply, decimals, Token-2022 extensions."""

    def produce() -> dict[str, Any]:
        result = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (result or {}).get("value")
        if not value:
            raise SourceError("mint account not found on chain")

        owner = value.get("owner")
        if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
            raise SourceError(f"address is not an SPL mint (owner {owner})")

        data = value.get("data") or {}
        parsed = data.get("parsed") or {}
        if parsed.get("type") != "mint":
            raise SourceError(f"account is a {parsed.get('type')!r}, not a mint")

        info = dict(parsed.get("info") or {})
        info["_program_owner"] = owner
        info["_is_token_2022"] = owner == TOKEN_2022_PROGRAM_ID
        return info

    return cache.get_or_set("mint_account", mint, produce)


def is_token_mint(address: str) -> bool:
    """True when `address` is an SPL mint. False for pools / other accounts."""
    try:
        fetch_mint_account(address)
        return True
    except SourceError:
        return False


def fetch_dexscreener_pair(pair_address: str) -> dict[str, Any]:
    """Look up one Solana pair by its pool address. Raises if DexScreener has none."""

    def produce() -> dict[str, Any]:
        try:
            response = _http().get(f"{DEXSCREENER_PAIR_URL}/{pair_address}")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"dexscreener pair: {exc}") from exc

        pair = body.get("pair")
        if not pair and body.get("pairs"):
            pair = body["pairs"][0]
        if not pair:
            raise SourceError("dexscreener has no pair for this address")
        return pair

    return cache.get_or_set("dexscreener_pair", pair_address, produce)


def _mint_from_pair(pair: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Pick the non-SOL side of a DexScreener pair as the traded mint."""
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    base_addr = base.get("address")
    quote_addr = quote.get("address")

    if base_addr and base_addr != WRAPPED_SOL_MINT:
        return base_addr, base.get("symbol")
    if quote_addr and quote_addr != WRAPPED_SOL_MINT:
        return quote_addr, quote.get("symbol")
    if base_addr:
        return base_addr, base.get("symbol")
    if quote_addr:
        return quote_addr, quote.get("symbol")
    raise SourceError("pair has no token addresses")


def resolve_to_mint(address: str) -> dict[str, Any]:
    """Map a corpus / Axiom address to the underlying SPL mint.

    The candle files are keyed by whatever Axiom put in the URL. On Solana that
    is often the liquidity pool, not the mint. Gate 0 and Jupiter need the mint,
    so this tries mint-first and falls back to DexScreener's pair lookup.

    Returns ``{"mint", "source", "pool", "symbol", "dex_id"}``.
    """
    if not address or not isinstance(address, str):
        raise SourceError("empty address")
    # Axiom sometimes surfaces EVM / Robinhood ids; those are not Solana mints.
    if address.startswith("0x") or address.startswith("0X"):
        raise SourceError("not a Solana address (EVM 0x… id)")

    def produce() -> dict[str, Any]:
        if is_token_mint(address):
            return {
                "mint": address,
                "source": "on_chain_mint",
                "pool": None,
                "symbol": None,
                "dex_id": None,
            }

        pair = fetch_dexscreener_pair(address)
        mint, symbol = _mint_from_pair(pair)
        return {
            "mint": mint,
            "source": "dexscreener_pair",
            "pool": address,
            "symbol": symbol,
            "dex_id": pair.get("dexId"),
        }

    return cache.get_or_set("resolve_mint", address, produce)


# The holder ranking is the slowest call and the least critical: an unavailable
# one only degrades the verdict, so it gets a tighter budget than the checks the
# verdict actually depends on. It runs concurrently with them, so this caps how
# long it can hold up the whole report.
HOLDERS_TIMEOUT_S = 4.0


def fetch_largest_accounts(mint: str) -> list[dict[str, Any]]:
    """Up to the 20 largest token accounts for the mint, largest first."""

    def produce() -> list[dict[str, Any]]:
        result = _rpc(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
            timeout_s=HOLDERS_TIMEOUT_S,
        )
        accounts = (result or {}).get("value")
        if accounts is None:
            raise SourceError("no largest-accounts data returned")
        return accounts

    return cache.get_or_set("holders", mint, produce)


def fetch_dexscreener_pairs(mint: str) -> list[dict[str, Any]]:
    """All known pools for the mint. Empty list is a valid, meaningful answer."""

    def produce() -> list[dict[str, Any]]:
        try:
            response = _http().get(f"{DEXSCREENER_TOKENS_URL}/{mint}")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"dexscreener: {exc}") from exc

        pairs = body.get("pairs") or []
        return [p for p in pairs if p.get("chainId") == "solana"]

    return cache.get_or_set("dexscreener", mint, produce)


def fetch_dexscreener_boosts(*, which: str = "latest") -> list[dict[str, Any]]:
    """DexScreener paid boosts. `which` is ``latest`` or ``top``."""

    url = DEXSCREENER_BOOSTS_TOP_URL if which == "top" else DEXSCREENER_BOOSTS_LATEST_URL

    def produce() -> list[dict[str, Any]]:
        try:
            response = _http().get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"dexscreener boosts ({which}): {exc}") from exc
        if not isinstance(body, list):
            raise SourceError(f"dexscreener boosts ({which}): unexpected payload")
        return [row for row in body if isinstance(row, dict) and row.get("chainId") == "solana"]

    return cache.get_or_set("dexscreener_boosts", which, produce)


def fetch_gecko_trending_pools(*, page: int = 1) -> list[dict[str, Any]]:
    """Solana trending pools from GeckoTerminal (keyless, ~30 rpm)."""

    def produce() -> list[dict[str, Any]]:
        try:
            response = _http().get(GECKO_TRENDING_URL, params={"page": page})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"gecko trending: {exc}") from exc
        rows = body.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    return cache.get_or_set("gecko_trending", f"p{page}", produce)


# GeckoTerminal pool listings other than trending. ``new_pools`` surfaces fresh
# launches (where most memecoin movement lives) and ``pools`` ranks by volume.
GECKO_POOL_KINDS = ("new_pools", "pools", "trending_pools")


def fetch_gecko_pool_list(*, kind: str = "new_pools", page: int = 1) -> list[dict[str, Any]]:
    """Solana pool listing by ``kind`` (``new_pools`` / ``pools`` / ``trending_pools``)."""
    if kind not in GECKO_POOL_KINDS:
        raise SourceError(f"unsupported gecko pool kind {kind!r}")

    def produce() -> list[dict[str, Any]]:
        url = f"{GECKO_BASE_URL}/networks/solana/{kind}"
        try:
            response = _http().get(url, params={"page": page})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"gecko {kind}: {exc}") from exc
        rows = body.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    ns = "gecko_new_pools" if kind == "new_pools" else "gecko_pool_list"
    return cache.get_or_set(ns, f"{kind}:p{page}", produce)


# DexScreener accepts up to 30 comma-separated mints per token lookup, which is
# what makes a self-built candle feed affordable: ~2 calls covers 60 tokens.
DEXSCREENER_BATCH_MAX = 30


def fetch_dexscreener_tokens_batch(mints: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Solana pairs for up to 30 mints in one call, keyed by mint.

    Uncached on purpose: callers use this to sample a live price series, so a
    cache hit would silently flatten the candles they are trying to build.
    """
    wanted = [m.strip() for m in mints if m and m.strip()][:DEXSCREENER_BATCH_MAX]
    if not wanted:
        return {}
    try:
        response = _http().get(f"{DEXSCREENER_TOKENS_URL}/{','.join(wanted)}")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"dexscreener batch: {exc}") from exc

    out: dict[str, list[dict[str, Any]]] = {m: [] for m in wanted}
    for pair in body.get("pairs") or []:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        for side in ("baseToken", "quoteToken"):
            addr = ((pair.get(side) or {}).get("address") or "").strip()
            if addr in out:
                out[addr].append(pair)
    return out


def fetch_gecko_ohlcv(
    pool_address: str,
    *,
    timeframe: str = "minute",
    aggregate: int = 1,
    limit: int = 300,
    currency: str = "usd",
) -> list[list[float]]:
    """Raw Gecko OHLCV rows: ``[ts_sec, open, high, low, close, volume]``.

    Newest-first from the API; caller should sort ascending for the live buffer.
    """

    key = f"{pool_address}:{timeframe}:{aggregate}:{limit}:{currency}"

    def produce() -> list[list[float]]:
        url = (
            f"{GECKO_BASE_URL}/networks/solana/pools/{pool_address}"
            f"/ohlcv/{timeframe}"
        )
        try:
            response = _http().get(
                url,
                params={
                    "aggregate": aggregate,
                    "limit": limit,
                    "currency": currency,
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"gecko ohlcv: {exc}") from exc

        data = body.get("data") or {}
        attrs = data.get("attributes") if isinstance(data, dict) else {}
        rows = (attrs or {}).get("ohlcv_list") or []
        out: list[list[float]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                out.append([float(row[i]) for i in range(6)] if len(row) >= 6 else [
                    float(row[0]), float(row[1]), float(row[2]),
                    float(row[3]), float(row[4]), 0.0,
                ])
            except (TypeError, ValueError):
                continue
        if not out:
            raise SourceError("gecko ohlcv: empty series")
        return out

    return cache.get_or_set("gecko_ohlcv", key, produce)


def fetch_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    *,
    slippage_bps: int = 300,
    restrict_intermediate: bool = True,
) -> dict[str, Any]:
    """Quote a Jupiter swap. Raises NoRouteError when routable-but-impossible."""

    def produce() -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "restrictIntermediateTokens": "true" if restrict_intermediate else "false",
        }
        last_error: Optional[str] = None

        for url in JUPITER_QUOTE_URLS:
            try:
                response = _http().get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{url}: {exc}"
                continue

            if response.status_code in (400, 404, 422):
                # Jupiter reports an unroutable pair as a client error.
                detail = _safe_error_text(response)
                if "route" in detail.lower() or response.status_code == 400:
                    raise NoRouteError(detail or "no route found")
                last_error = f"{url}: HTTP {response.status_code} {detail}"
                continue

            if response.status_code >= 500 or response.status_code == 429:
                last_error = f"{url}: HTTP {response.status_code}"
                continue

            try:
                body = response.json()
            except ValueError as exc:
                last_error = f"{url}: malformed JSON ({exc})"
                continue

            if not isinstance(body, dict):
                last_error = f"{url}: unexpected quote payload"
                continue
            if not body.get("outAmount"):
                raise NoRouteError("quote returned no output amount")
            return body

        raise SourceError(last_error or "jupiter unreachable")

    cache_key = (
        f"{input_mint}->{output_mint}:{amount_raw}:{slippage_bps}:"
        f"ri={int(restrict_intermediate)}"
    )
    return cache.get_or_set("jupiter_quote", cache_key, produce)


def fetch_sell_quote(mint: str, amount_raw: int) -> dict[str, Any]:
    """Quote selling `amount_raw` base units of `mint` into wrapped SOL.

    Raises NoRouteError when Jupiter is reachable but cannot route the sell,
    which is the strongest cheap signal that a token cannot be exited.
    """
    return fetch_jupiter_quote(mint, WRAPPED_SOL_MINT, amount_raw)


def _safe_error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or body)[:200]
    return str(body)[:200]


def fetch_helius_transactions(address: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Parsed recent txs for an address via Helius enhanced API.

    Used by the paper cluster/launch channel. Requires ``HELIUS_API_KEY``.
    """
    key = SETTINGS.helius_api_key
    if not key:
        raise SourceError("HELIUS_API_KEY unset")
    addr = (address or "").strip()
    if not addr:
        raise SourceError("empty address")

    def produce() -> list[dict[str, Any]]:
        url = f"https://api.helius.xyz/v0/addresses/{addr}/transactions"
        try:
            response = _http().get(url, params={"api-key": key, "limit": str(limit)})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"helius txs: {exc}") from exc
        if not isinstance(body, list):
            raise SourceError("helius txs: unexpected body")
        return [row for row in body if isinstance(row, dict)]

    return cache.get_or_set("helius_txs", f"{addr}:{limit}", produce)


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
