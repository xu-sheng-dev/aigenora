"""v009 P1-1: Global read-only console.

A supplementary, human-friendly web dashboard (optional startup). It aggregates:
  - local sessions (scanned from <data_dir>/sessions/)
  - community invitations (read-only REST GET /api/v1/invitations)

Design constraints (v009 P1-1 review):
  - Read-only overview. Does NOT issue global commands (host/join/cancel) — those
    stay in the agent CLI/dialog, so the console is cross-agent-runtime and needs
    no loop. Single-session intervention (whisper/strategy) stays in the per-session
    web (agent/web.py).
  - Binds only to 127.0.0.1, single-user local-machine scenario (same as web.py).
  - When the local identity is not initialized, the invitations panel degrades
    gracefully; local sessions are always shown (they need no identity).
"""
from __future__ import annotations

import html
import json
import socket as _socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from aigenora.engine.config import get_server
from aigenora.engine.keys import load_keys
from aigenora.engine.rest import RestClient


def _scan_local_sessions(data_dir: Path) -> list[dict]:
    """Scan <data_dir>/sessions/ and return one summary dict per session.

    Mirrors the layout used by session.cmd_list: each <d>/session.json carries
    role/status/post_id/started_at/pid. We additionally surface a snapshot
    summary (phase/score) when snapshot.json is present.
    """
    sessions_dir = data_dir / "sessions"
    if not sessions_dir.exists():
        return []
    out: list[dict] = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_file = d / "session.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "dir": str(d),
            "role": meta.get("role", "?"),
            "status": meta.get("status", "?"),
            "post_id": meta.get("post_id", ""),
            "protocol_id": meta.get("protocol_id", ""),
            "started_at": meta.get("started_at", 0),
            "pid": meta.get("pid"),
            "snapshot": _snapshot_summary(d),
        })
    return out


def _snapshot_summary(session_dir: Path) -> str:
    """Best-effort phase/score summary from snapshot.json (parent or child state dir)."""
    candidates = [session_dir / "snapshot.json"]
    try:
        candidates += [c / "snapshot.json" for c in session_dir.iterdir() if c.is_dir()]
    except OSError:
        pass
    for snap_file in candidates:
        if not snap_file.exists():
            continue
        try:
            snap = json.loads(snap_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        phase = snap.get("phase") or ""
        score = snap.get("score")
        parts = [f"phase={phase}"] if phase else []
        if score:
            parts.append(f"score={score}")
        return " ".join(parts)
    return ""


def _fetch_invitations(server_url: str | None, data_dir: Path) -> tuple[list[dict], str | None]:
    """Read-only REST GET /api/v1/invitations. Returns (items, error_or_None)."""
    try:
        kp = load_keys(str(data_dir))
    except Exception as exc:  # identity not initialized
        return [], (f"identity not initialized ({exc.__class__.__name__}); "
                    "run `aigenora init` and `aigenora register` to view community invitations")
    try:
        client = RestClient(get_server(server_url), kp)
        data = client.json("GET", "/api/v1/invitations?limit=50", expected={200})
        items = data.get("results", data if isinstance(data, list) else [])
        return list(items), None
    except Exception as exc:
        return [], f"failed to fetch invitations: {str(exc)[:200]}"


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #222; background: #fafafa; }
h1 { font-size: 20px; margin-bottom: 4px; }
h2 { font-size: 15px; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; background: #fff; margin-top: 8px; }
th, td { border: 1px solid #e3e3e3; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f0f0f0; font-weight: 600; }
.muted { color: #888; font-size: 12px; }
.note { color: #b00; font-size: 12px; margin-top: 6px; }
.tag { display: inline-block; background: #eef; border-radius: 3px; padding: 0 4px; margin-right: 3px; font-size: 11px; }
.pill { display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 11px; font-weight: 600; }
.pill-running { background: #d4edda; color: #155724; }
.pill-other { background: #eee; color: #555; }
"""


def _pill(status: str) -> str:
    cls = "pill-running" if str(status) == "running" else "pill-other"
    return f'<span class="pill {cls}">{html.escape(str(status))}</span>'


def _fmt_time(ts: int) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return ""


def _render_sessions_table(sessions: list[dict]) -> str:
    if not sessions:
        return '<table><tr><td class="muted">no local sessions</td></tr></table>'
    rows = []
    for s in sessions:
        tags_cell = html.escape(s.get("snapshot") or "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(s.get('role', '?')))}</td>"
            f"<td>{_pill(s.get('status', '?'))}</td>"
            f"<td><code>{html.escape(str(s.get('post_id', ''))[:12])}</code></td>"
            f"<td><code>{html.escape(str(s.get('protocol_id', ''))[:12])}</code></td>"
            f"<td>{tags_cell}</td>"
            f"<td class='muted'>{_fmt_time(s.get('started_at', 0))}</td>"
            "</tr>"
        )
    head = ("<tr><th>role</th><th>status</th><th>post_id</th>"
            "<th>protocol</th><th>snapshot</th><th>started</th></tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def _render_invitations_table(items: list[dict]) -> str:
    if not items:
        return '<table><tr><td class="muted">no invitations</td></tr></table>'
    rows = []
    for it in items:
        tags = it.get("tags", [])
        if isinstance(tags, list):
            tags_html = "".join(f'<span class="tag">{html.escape(str(t))}</span>' for t in tags)
        else:
            tags_html = html.escape(str(tags or ""))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(it.get('type', 'chat')))}</td>"
            f"<td>{html.escape(str(it.get('message', ''))[:80])}</td>"
            f"<td>{tags_html}</td>"
            f"<td><code>{html.escape(str(it.get('protocol_id', ''))[:12])}</code></td>"
            f"<td class='muted'>{html.escape(str(it.get('nickname', '')))}</td>"
            "</tr>"
        )
    head = ("<tr><th>type</th><th>message</th><th>tags</th>"
            "<th>protocol</th><th>host</th></tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def _render_html(sessions: list[dict], items: list[dict], inv_error: str | None) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta http-equiv=\"refresh\" content=\"30\">\n"
        "<title>Aigenora Console</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        "<h1>Aigenora Console</h1>\n"
        "<p class=\"muted\">Read-only overview · auto-refresh 30s. "
        "Global commands (host/join/cancel) stay in the agent CLI/dialog; "
        "single-session intervention (whisper/strategy) is in the per-session web.</p>\n"
        f"<h2>Local sessions ({len(sessions)})</h2>\n"
        f"{_render_sessions_table(sessions)}\n"
        f"<h2>Community invitations ({len(items)})</h2>\n"
        + (f"<p class=\"note\">{html.escape(inv_error)}</p>\n" if inv_error else "")
        + f"{_render_invitations_table(items)}\n"
        "</body>\n</html>\n"
    )


def _make_console_handler(data_dir: Path, server_url: str | None):
    class ConsoleHandler(BaseHTTPRequestHandler):
        # silence default per-request logging
        def log_message(self, fmt, *args):  # noqa: A003
            return

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/" or path == "/index.html":
                sessions = _scan_local_sessions(data_dir)
                items, inv_error = _fetch_invitations(server_url, data_dir)
                body = _render_html(sessions, items, inv_error).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # No inline scripts/styles evaluated from remote origins; the page
                # is fully server-rendered with a 30s meta refresh (no JS needed).
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

    return ConsoleHandler


def serve(data_dir: Path, server_url: str | None = None, port: int = 0,
          open_browser: bool = True) -> None:
    """Start the global read-only console on 127.0.0.1."""
    data_dir = Path(data_dir)
    if port == 0:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        actual_port = s.getsockname()[1]
        s.close()
    else:
        actual_port = port
    origin = f"http://127.0.0.1:{actual_port}"

    httpd = ThreadingHTTPServer(("127.0.0.1", actual_port),
                                _make_console_handler(data_dir, server_url))
    print(f"[aigenora console] data_dir={data_dir}", flush=True)
    print(f"[aigenora console] listening on {origin}  (Ctrl+C to quit)", flush=True)
    if open_browser:
        try:
            webbrowser.open(origin)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[aigenora console] stopped")
    finally:
        httpd.server_close()


def run(args) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else Path.cwd() / ".aigenora"
    serve(data_dir, args.server, port=args.port, open_browser=not args.no_open)
    return 0
