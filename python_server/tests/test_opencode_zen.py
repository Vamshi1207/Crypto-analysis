"""OpenCode Zen — jev-1.13-free via /systemone (typed decisions, not chat).

Jev is NOT a chat model: no /chat/completions, no /responses, no CLI agent
model. Native protocol (same as TypeSafe SystemOne):

    POST https://opencode.ai/zen/v1/systemone
    {"model": "jev-1.13-free", "state": "...", "questions": {"q": {"type": "noul", ...}}}

Verified 2026-10-09: free → 200 with typed answers; paid jev-1.13 → 402
(no funds). No CLI subprocess needed.

Live:

    pytest tests/test_opencode_zen.py -o addopts="" -m live -s

Key is never printed — only presence + reply size.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

BASE_URL = "https://opencode.ai/zen/v1"
MODEL = "jev-1.13-free"

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"


def _load_dotenv_minimal(path: Path = DOTENV_PATH) -> None:
    """Minimal .env loader — avoids a python-dotenv dependency."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv_minimal()


def _smoke_payload() -> dict:
    return {
        "model": MODEL,
        "state": "Smoke test. Nothing is urgent.",
        "questions": {
            "urgent": {
                "type": "noul",
                "instructions": "Is this urgent?",
                "criteria": {"true": "urgent", "false": "not urgent"},
            }
        },
    }


def test_payload_shape_offline():
    """Always runs — no network, no key needed."""
    p = _smoke_payload()
    assert p["model"] == "jev-1.13-free"
    assert p["questions"]["urgent"]["type"] == "noul"
    assert BASE_URL == "https://opencode.ai/zen/v1"


def test_parse_systemone_unit():
    data = {"model": "jev-1.13-free", "answers": {"urgent": {"type": "noul", "noul": 0.03}}}
    assert data["answers"]["urgent"]["noul"] == pytest.approx(0.03)


def test_jev_chair_accepts_opencode_key(monkeypatch):
    """Paper desk key wiring: OPENCODE_GO_API_KEY alone makes chair ready."""
    from decision import jev as jev_mod

    monkeypatch.setattr(jev_mod, "ENABLED", True)
    monkeypatch.setattr(jev_mod, "ENDPOINT", "https://opencode.ai/zen/v1/systemone")
    monkeypatch.setattr(jev_mod, "MODEL", "jev-1.13-free")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")
    for k in ("TYPESAFE_API_KEY", "JEV_API_KEY", "JEVX_API_KEY", "OPENCODE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert jev_mod._api_key() == "test-key"
    assert jev_mod.preflight().ready is True


@pytest.mark.live
def test_live_jev_free_systemone():
    """Direct HTTPS to /systemone. Skipped when key is missing."""
    import httpx

    api_key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if not api_key:
        pytest.skip("OPENCODE_GO_API_KEY not set in env/.env")

    session_id = os.environ.get("OPENCODE_SESSION_ID", "pytest-session")
    resp = httpx.post(
        f"{BASE_URL}/systemone",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-opencode-session": session_id,
        },
        json=_smoke_payload(),
        timeout=30.0,
    )
    if resp.status_code in (429, 402, 403):
        pytest.skip(f"zen free tier unavailable: {resp.text[:150]}")
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    assert data.get("model") == MODEL
    assert "urgent" in data.get("answers", {}), f"unexpected: {str(data)[:300]}"
    print(f"\n{MODEL} live OK: {str(data['answers'])[:200]}")
