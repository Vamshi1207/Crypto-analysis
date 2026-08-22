"""Headless wrapper around the in-container Antigravity CLI (`agy`).

Gemini CLI is deprecated; Antigravity CLI is the terminal surface for the same
subscription / agent harness:
https://antigravity.google/product/antigravity-cli

Headless usage (print mode):
https://antigravity.google/docs/cli/headless/

  agy -p "…" --output-format json --print-timeout 2m

Auth is a one-time interactive `agy` session (Google Sign-In). In Docker we
spoof SSH so it prints a URL instead of opening a browser. Credentials land
under `~/.gemini/` on the mounted volume. Alternatively set `GEMINI_API_KEY`
with `modelProvider: gemini` in settings.json for key-based headless auth.

This is the slow, expensive tier. Escalation and review only — not the
per-tick decision loop.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from decision.config import SETTINGS

GEMINI_HOME = Path(os.getenv("GEMINI_HOME", "/root/.gemini"))
AGY_SETTINGS = GEMINI_HOME / "antigravity-cli" / "settings.json"
# Legacy Gemini CLI path; keep checking so an old login still counts as present.
LEGACY_OAUTH = GEMINI_HOME / "oauth_creds.json"
DEFAULT_TIMEOUT_S = 120.0

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class AgyUnavailable(RuntimeError):
    """The CLI cannot be used right now. Callers should degrade, not retry hard."""


# Back-compat alias for earlier imports.
GeminiUnavailable = AgyUnavailable


@dataclass(frozen=True)
class AgyStatus:
    installed: bool
    authenticated: bool
    binary: Optional[str]
    model: str
    detail: str

    @property
    def ready(self) -> bool:
        return self.installed and self.authenticated


GeminiStatus = AgyStatus


def _has_session() -> bool:
    """True when a Google Sign-In session or API-key auth is configured."""
    if os.getenv("GEMINI_API_KEY", "").strip():
        return True
    if LEGACY_OAUTH.exists():
        return True
    # Antigravity stores profiles under ~/.gemini/antigravity-cli/
    cli_dir = GEMINI_HOME / "antigravity-cli"
    if not cli_dir.is_dir():
        return False
    for path in cli_dir.rglob("*"):
        if path.is_file() and path.name != "settings.json":
            return True
        if path.name == "settings.json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("modelProvider") == "gemini" and os.getenv("GEMINI_API_KEY", "").strip():
                return True
    return False


def preflight() -> AgyStatus:
    """Cheap, side-effect-free readiness check. Never spawns the CLI."""
    binary = shutil.which(SETTINGS.agy_binary)
    model = SETTINGS.agy_model or "(default)"
    if binary is None:
        return AgyStatus(
            installed=False,
            authenticated=False,
            binary=None,
            model=model,
            detail=(
                f"{SETTINGS.agy_binary!r} not on PATH. Rebuild the image: "
                "docker compose build python_server"
            ),
        )

    if not _has_session():
        return AgyStatus(
            installed=True,
            authenticated=False,
            binary=binary,
            model=model,
            detail=(
                f"No Antigravity session under {GEMINI_HOME}. One-time login: "
                "docker compose exec python_server agy"
            ),
        )

    return AgyStatus(
        installed=True,
        authenticated=True,
        binary=binary,
        model=model,
        detail="ready",
    )


def _env() -> dict[str, str]:
    env = os.environ.copy()
    # Keep headless runs from trying to open a browser or hang on a TTY prompt.
    env.setdefault("SSH_CONNECTION", "172.17.0.1 22 172.17.0.2 22")
    env.setdefault("SSH_CLIENT", "172.17.0.1 22 22")
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    return env


def _format_timeout(timeout_s: float) -> str:
    # agy --print-timeout accepts Go-style durations (e.g. 2m, 90s).
    seconds = max(1, int(timeout_s))
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def ask(
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Send one prompt via `agy -p` and return the response text."""
    status = preflight()
    if not status.ready:
        raise AgyUnavailable(status.detail)

    chosen = model or SETTINGS.agy_model
    command = [
        status.binary or SETTINGS.agy_binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        _format_timeout(timeout_s),
    ]
    if chosen:
        command.extend(["--model", chosen])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s + 15,
            env=_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgyUnavailable(f"timed out after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise AgyUnavailable(f"could not start agy: {exc}") from exc

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if completed.returncode != 0:
        raise AgyUnavailable(
            f"exit {completed.returncode}: {(stderr or stdout)[:400] or 'no output'}"
        )

    if not stdout:
        raise AgyUnavailable(f"agy returned empty stdout. stderr: {stderr[:400]}")

    # Prefer the JSON envelope from --output-format json.
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout

    if isinstance(envelope, dict):
        if envelope.get("status") and str(envelope["status"]).upper() not in {
            "SUCCESS",
            "OK",
            "COMPLETED",
        }:
            raise AgyUnavailable(
                envelope.get("error")
                or f"agy status={envelope.get('status')!r}"
            )
        response = envelope.get("response")
        if isinstance(response, str) and response.strip():
            return response.strip()
        structured = envelope.get("structured_output")
        if structured is not None:
            return json.dumps(structured)

    return stdout


def ask_json(
    prompt: str,
    *,
    model: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    schema: Optional[dict[str, Any]] = None,
) -> Any:
    """Ask for JSON. Uses `--json-schema` when a schema is provided."""
    status = preflight()
    if not status.ready:
        raise AgyUnavailable(status.detail)

    if schema is not None:
        chosen = model or SETTINGS.agy_model
        command = [
            status.binary or SETTINGS.agy_binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--print-timeout",
            _format_timeout(timeout_s),
        ]
        if chosen:
            command.extend(["--model", chosen])

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s + 15,
                env=_env(),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyUnavailable(f"timed out after {timeout_s:.0f}s") from exc
        except OSError as exc:
            raise AgyUnavailable(f"could not start agy: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise AgyUnavailable(
                f"exit {completed.returncode}: {detail[:400] or 'no output'}"
            )
        try:
            envelope = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise AgyUnavailable(f"agy JSON envelope invalid: {exc}") from exc
        if isinstance(envelope, dict) and envelope.get("structured_output") is not None:
            return envelope["structured_output"]
        raw = envelope.get("response", "") if isinstance(envelope, dict) else ""
    else:
        instruction = (
            f"{prompt}\n\n"
            "Respond with a single valid JSON object and nothing else. "
            "No prose, no markdown fences, no trailing commentary."
        )
        raw = ask(instruction, model=model, timeout_s=timeout_s)

    for candidate in _json_candidates(str(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise AgyUnavailable(f"response was not valid JSON: {str(raw)[:300]}")


def _json_candidates(raw: str) -> list[str]:
    candidates = [raw]
    fenced = _JSON_FENCE.search(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    return candidates
