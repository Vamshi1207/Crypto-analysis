import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify
from werkzeug.serving import make_server


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_run_dir(run_dir):
    run_dir = Path(run_dir)
    tokens_dir = run_dir / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def reset_run_dir(run_dir):
    run_dir = ensure_run_dir(run_dir)

    for path in run_dir.glob("*.json"):
        path.unlink(missing_ok=True)
    for path in (run_dir / "tokens").glob("*.json"):
        path.unlink(missing_ok=True)

    return run_dir


def _atomic_write_json(path, payload):
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def write_overview(run_dir, payload):
    run_dir = ensure_run_dir(run_dir)
    _atomic_write_json(run_dir / "overview.json", payload)


def update_token_status(run_dir, token_id, payload):
    run_dir = ensure_run_dir(run_dir)
    safe_token_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in token_id)
    _atomic_write_json((run_dir / "tokens" / f"{safe_token_id}.json"), payload)


def read_state(run_dir):
    run_dir = ensure_run_dir(run_dir)
    overview_path = run_dir / "overview.json"
    overview = {}
    if overview_path.exists():
        overview = json.loads(overview_path.read_text(encoding="utf-8"))

    tokens = []
    for path in sorted((run_dir / "tokens").glob("*.json")):
        try:
            tokens.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue

    status_order = {"running": 0, "queued": 1, "done": 2, "error": 3, "empty": 4}
    tokens.sort(key=lambda item: (status_order.get(item.get("status"), 99), item.get("file_number", 10**9)))
    return {"overview": overview, "tokens": tokens}


def find_open_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _dashboard_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dataset Prep Progress</title>
  <style>
    :root {
      --bg: #f5f1e8;
      --panel: #fffaf0;
      --ink: #1d1a16;
      --muted: #6f665c;
      --line: #d8cbb8;
      --accent: #0e7a66;
      --warn: #b3561d;
      --err: #a32638;
      --done: #2d6a4f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(14, 122, 102, 0.12), transparent 30%),
        linear-gradient(180deg, #f8f5ee 0%, var(--bg) 100%);
    }
    .wrap {
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px;
    }
    .hero {
      padding: 20px 0 12px;
    }
    .hero h1 {
      margin: 0;
      font-size: 34px;
      font-weight: 600;
    }
    .hero p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 16px;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin: 18px 0 24px;
    }
    .card, .token {
      background: rgba(255, 250, 240, 0.9);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(60, 40, 20, 0.06);
    }
    .card {
      padding: 16px;
    }
    .label {
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .value {
      font-size: 30px;
      margin-top: 8px;
    }
    .token-list {
      display: grid;
      gap: 14px;
    }
    .token {
      padding: 16px;
    }
    .token-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      flex-wrap: wrap;
    }
    .token-title {
      font-size: 20px;
      font-weight: 600;
    }
    .token-subtitle {
      color: var(--muted);
      font-size: 14px;
      margin-top: 4px;
    }
    .status {
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      border: 1px solid currentColor;
    }
    .status-running { color: var(--accent); }
    .status-done { color: var(--done); }
    .status-error { color: var(--err); }
    .status-empty { color: var(--warn); }
    .status-queued { color: var(--muted); }
    .bar {
      height: 12px;
      background: #eadfce;
      border-radius: 999px;
      overflow: hidden;
      margin: 14px 0 10px;
    }
    .bar > div {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #0e7a66, #5fb1a0);
      transition: width 0.4s ease;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
    }
    .empty {
      padding: 22px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 16px;
    }
    code {
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>Dataset Prep Progress</h1>
      <p id="headline">Waiting for updates…</p>
    </div>
    <div class="summary" id="summary"></div>
    <div class="token-list" id="tokens"></div>
  </div>
  <script>
    function esc(value) {
      return String(value ?? "").replace(/[&<>"]/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;"
      }[ch]));
    }

    function renderSummary(overview, tokens) {
      const total = overview.total_tokens ?? tokens.length;
      const done = tokens.filter(t => t.status === "done").length;
      const running = tokens.filter(t => t.status === "running").length;
      const errors = tokens.filter(t => t.status === "error").length;
      const rows = tokens.reduce((sum, t) => sum + (t.rows_written || 0), 0);

      const cards = [
        ["Tokens", total],
        ["Running", running],
        ["Done", done],
        ["Errors", errors],
        ["Rows Written", rows.toLocaleString()],
      ];

      document.getElementById("summary").innerHTML = cards.map(([label, value]) => `
        <div class="card">
          <div class="label">${esc(label)}</div>
          <div class="value">${esc(value)}</div>
        </div>
      `).join("");

      const status = overview.status || (running > 0 ? "running" : "idle");
      document.getElementById("headline").textContent =
        `${status.toUpperCase()} • ${done}/${total} tokens complete`;
    }

    function renderTokens(tokens) {
      const root = document.getElementById("tokens");
      if (!tokens.length) {
        root.innerHTML = '<div class="empty">No worker state yet. The dashboard will populate as jobs start.</div>';
        return;
      }

      root.innerHTML = tokens.map((token) => {
        const total = token.total_steps || 0;
        const processed = token.processed_steps || 0;
        const pct = total > 0 ? Math.min(100, (processed / total) * 100) : (token.status === "done" ? 100 : 0);
        const message = token.message || "";
        return `
          <div class="token">
            <div class="token-top">
              <div>
                <div class="token-title">#${esc(token.file_number)} ${esc(token.token_name || token.token_id || "Unknown token")}</div>
                <div class="token-subtitle"><code>${esc(token.token_id || "")}</code></div>
              </div>
              <div class="status status-${esc(token.status || "queued")}">${esc(token.status || "queued")}</div>
            </div>
            <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
            <div class="metrics">
              <div>Progress: ${esc(processed.toLocaleString())}/${esc(total.toLocaleString())}</div>
              <div>Rows: ${esc((token.rows_written || 0).toLocaleString())}</div>
              <div>Memory: ${esc(token.memory_gb != null ? token.memory_gb.toFixed(2) + " GB" : "n/a")}</div>
              <div>Updated: ${esc(token.updated_at || "n/a")}</div>
              <div>Stage: ${esc(message || "working")}</div>
            </div>
          </div>
        `;
      }).join("");
    }

    async function refresh() {
      const response = await fetch("/api/state", { cache: "no-store" });
      const payload = await response.json();
      renderSummary(payload.overview || {}, payload.tokens || []);
      renderTokens(payload.tokens || []);
    }

    refresh().catch(console.error);
    setInterval(() => refresh().catch(console.error), 1000);
  </script>
</body>
</html>"""


def create_dashboard_app(run_dir):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return _dashboard_html()

    @app.get("/api/state")
    def api_state():
        return jsonify(read_state(run_dir))

    return app


class DashboardServer:
    def __init__(self, run_dir, host, port):
        self.host = host
        self.port = port
        self.app = create_dashboard_app(run_dir)
        self.server = make_server(host, port, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
