"""Single-session relay web: observe state_dir + a constrained local intervention channel.

Design constraints:
- Binds only to 127.0.0.1, no auth (single-user local-machine scenario).
- All reads/writes reuse SnapshotBus / EventBus / DetailLog / StrategyStore / DecisionBus,
  fully equivalent to the CLI, with zero intrusion into the engine layer.
- Operator notes are written only to strategy.operator_hint and never enter the P2P protocol
  message body; whether they take effect depends on the protocol's hooks.py implementation.
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aigenora.proto.decide_gateway import submit_decision
from aigenora.agent.coach import CoachWorker
from aigenora.proto.sdk import DetailLog, EventBus, SnapshotBus, StrategyStore, WhisperLog
from aigenora.control import declared_control_modes


class _BadRequest(Exception):
    """Request body could not be parsed as valid utf-8 / json. Handler catches it and returns 400."""


# ---------------------------------------------------------------------------
# state_dir resolution (kept consistent with session.py)

def resolve_state_dir(state_dir: str | Path) -> Path:
    """In daemon mode, the parent/ and child subdirectory form a two-layer structure; this locates
    the directory containing snapshot.json in a unified way.

    注意：本函数是高频查询（broadcaster 每 0.5s 调用），**不能阻塞**。
    v015 的 daemon 时序修复（等待子目录）在 session.py 的 _resolve_state_dir 里实现，
    只用于写入类操作（strategy/decide/whisper），不影响本函数。
    """
    p = Path(state_dir)
    if (p / "snapshot.json").exists() or (p / "details.jsonl").exists():
        return p
    if (p / "session.json").exists():
        children = sorted(
            [d for d in p.iterdir() if d.is_dir() and (d.name.startswith("host-") or d.name.startswith("guest-"))],
            key=lambda d: d.name,
        )
        if children:
            return children[-1]
    return p


def _current_round(state_dir: str | Path) -> int:
    """从 snapshot.json 读当前轮号（0-indexed，用于 decide match_value）。读不到返回 0。"""
    try:
        from aigenora.proto.sdk import SnapshotBus
        snap = SnapshotBus(str(resolve_state_dir(state_dir))).read() or {}
        # snapshot.round 是 1-indexed（hooks 写入 round=1 表示第一轮）；decide match 用 0-indexed
        r = snap.get("round")
        return max(0, int(r) - 1) if r is not None else 0
    except Exception:
        return 0


def resolve_protocol_dir(root_dir: str | Path) -> Path | None:
    """Reverse-lookup protocol_dir from the daemon parent / child session.json.

    - For host, the parent session.json writes protocol_dir right after _resolve_state_dir.
    - For guest, the parent session.json is written by _run_daemon; protocol_dir is carried back
      from the subprocess via the peer_joined event (see join._join emitting it and
      _run_daemon reading startup_data["protocol_dir"] into session_meta).
    - Also covers the case where a session.json exists in the child directory.
    - v019-M2: 当传入的是业务子目录（child state_dir）时，向上查找 parent session.json。

    Returns None if it cannot be resolved. The web side uses this to decide whether /api/ui-available is true.
    """
    root = Path(root_dir)
    candidates: list[Path] = [root / "session.json"]
    try:
        eff = resolve_state_dir(root)
    except Exception:
        eff = root
    if eff != root:
        candidates.append(eff / "session.json")
    # v019-M2: 向上查找 parent session.json（child state_dir 场景）
    parent = root.parent
    for _ in range(3):  # 最多向上 3 层
        if parent == root or parent == parent.parent:
            break
        candidates.append(parent / "session.json")
        parent = parent.parent
    for sj in candidates:
        if not sj.exists():
            continue
        try:
            meta = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            continue
        pd = meta.get("protocol_dir") if isinstance(meta, dict) else None
        if isinstance(pd, str) and pd:
            p = Path(pd)
            if p.exists():
                return p
    return None


def _read_session_meta(root_dir: str | Path) -> dict[str, Any]:
    """Read daemon session metadata from a parent or resolved child directory."""
    root = Path(root_dir)
    candidates = [root / "session.json"]
    try:
        effective = resolve_state_dir(root)
        candidates.extend([effective / "session.json", effective.parent / "session.json"])
    except Exception:
        pass
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


def session_runtime_info(root_dir: str | Path) -> dict[str, Any]:
    """Return stable local runtime metadata for the parent page and protocol UI."""
    root = Path(root_dir)
    cur = resolve_state_dir(root)
    meta = _read_session_meta(root)
    snapshot = SnapshotBus(cur).read() or {}
    mode = meta.get("local_control_mode") or snapshot.get("control_mode") or "hybrid"
    if mode not in ("autonomous", "hybrid", "human"):
        mode = "hybrid"
    role = meta.get("role") or snapshot.get("role") or ""
    peer_mode = meta.get("peer_control_mode") or ""
    supported: list[str] = []
    schema = None
    ui_artifact = meta.get("ui_artifact") if isinstance(meta.get("ui_artifact"), dict) else None
    protocol_dir = resolve_protocol_dir(root)
    if protocol_dir is not None:
        try:
            from aigenora.proto.loader import load_hooks
            hooks = load_hooks(protocol_dir)
            supported = list(declared_control_modes(hooks))
            schema = getattr(hooks, "DECISION_SCHEMA", None)
        except Exception:
            supported = []
            schema = None
        if ui_artifact is None and (protocol_dir / "ui" / "index.html").is_file():
            try:
                from aigenora.agent.protocol_ui import read_ui_sidecar
                sidecar = read_ui_sidecar(protocol_dir) or {}
                source_kind = sidecar.get("source_kind") or (
                    "platform" if sidecar.get("source_server") else "local"
                )
                ui_artifact = {
                    "status": (
                        "session_consent_required"
                        if source_kind == "host_p2p"
                        else "available"
                    ),
                    "source_kind": source_kind,
                }
                if sidecar.get("ui_manifest_hash"):
                    ui_artifact["manifest_hash"] = sidecar["ui_manifest_hash"]
                if sidecar.get("source_peer"):
                    ui_artifact["source_peer"] = sidecar["source_peer"]
            except Exception:
                ui_artifact = {"status": "available", "source_kind": "local"}
    return {
        "root_dir": str(root),
        "state_dir": str(cur),
        "role": role,
        "control_mode": mode,
        "peer_control_mode": peer_mode,
        "supported_control_modes": supported,
        "decision_schema": schema,
        "web_mode": meta.get("web_mode") or "",
        "ui_artifact": ui_artifact,
    }


def _ui_dir(root_dir: str | Path) -> Path | None:
    meta = _read_session_meta(root_dir)
    session_ui_dir = meta.get("ui_dir")
    if isinstance(session_ui_dir, str) and session_ui_dir:
        ui = Path(session_ui_dir)
        if (ui / "index.html").is_file():
            artifact = meta.get("ui_artifact")
            source_kind = artifact.get("source_kind") if isinstance(artifact, dict) else None
            # Remote HTML/JS must use the isolated-origin postMessage bridge.  The
            # same-origin compatibility path exists only for trusted local installs;
            # otherwise an old platform/P2P page would inherit the relay origin and
            # bypass the capability boundary described by the remote-UI consent.
            if source_kind in {"platform", "host_p2p"} and _detect_legacy_ui(ui):
                return None
            return ui
    pd = resolve_protocol_dir(root_dir)
    if pd is None:
        return None
    try:
        from aigenora.agent.protocol_ui import read_ui_sidecar
        sidecar = read_ui_sidecar(pd) or {}
        # P2P consent is session-scoped.  Old clients may have placed a Host
        # snapshot in the protocol cache; never auto-execute that in a new session.
        source_kind = sidecar.get("source_kind")
        if source_kind == "host_p2p":
            return None
        ui = pd / "ui"
        if source_kind == "platform" and _detect_legacy_ui(ui):
            return None
    except Exception:
        pass
    ui = pd / "ui"
    if (ui / "index.html").exists():
        return ui
    return None


# v005 protocol UI (same-origin fetch "/api/...") goes through a compatibility-period fallback;
# v006 P4 new UI uses a postMessage bridge and runs on an isolated origin sandbox.
# Detection: if the UI directory contains a `.legacy-ui` marker file, or none of its files contain
# the `aigenoraBroadcastBridge` literal, treat it as legacy_mode.
def _detect_legacy_ui(ui_dir: Path | None) -> bool:
    """v006 P4 fallback: old UI (same-origin /api/* fetch) runs in a legacy sandbox.

    Trigger conditions (any one):
    1. The ui directory contains a .legacy-ui marker file (author's explicit declaration)
    2. ui/index.html does not contain the postMessage bridge keyword
    """
    if ui_dir is None:
        return True
    if (ui_dir / ".legacy-ui").exists():
        return True
    idx = ui_dir / "index.html"
    if not idx.exists():
        return True
    try:
        content = idx.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True
    # Detect the bridge keyword (v006 P4 UI should use parent.postMessage)
    return "parent.postMessage" not in content


def _rejected_remote_legacy_source(root_dir: str | Path) -> str | None:
    """Return the rejected remote source when its UI lacks the bridge contract."""
    meta = _read_session_meta(root_dir)
    artifact = meta.get("ui_artifact")
    source_kind = artifact.get("source_kind") if isinstance(artifact, dict) else None
    session_ui_dir = meta.get("ui_dir")
    if source_kind in {"platform", "host_p2p"} and isinstance(session_ui_dir, str):
        ui = Path(session_ui_dir)
        if (ui / "index.html").is_file() and _detect_legacy_ui(ui):
            return source_kind

    protocol_dir = resolve_protocol_dir(root_dir)
    if protocol_dir is None:
        return None
    try:
        from aigenora.agent.protocol_ui import read_ui_sidecar
        sidecar = read_ui_sidecar(protocol_dir) or {}
    except Exception:
        return None
    source_kind = sidecar.get("source_kind")
    ui = protocol_dir / "ui"
    if source_kind in {"platform", "host_p2p"} and (ui / "index.html").is_file():
        if _detect_legacy_ui(ui):
            return source_kind
    return None


# ---------------------------------------------------------------------------
# HTML template (single file, no build dependency)

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>aigenora session</title>
<link rel="icon" href="data:,">
<style>
  :root { color-scheme: light dark; }
  html, body { height: 100%; margin: 0; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; display: flex; flex-direction: column; }
  /* Tab bar */
  .tabs { display: flex; gap: 0; padding: 0 12px; background: #8881;
          border-bottom: 1px solid #8884; align-items: stretch; }
  .tab { padding: 8px 16px; cursor: pointer; border: 0; background: transparent; color: inherit;
         font-size: 13px; border-bottom: 2px solid transparent; }
  .tab.active { border-bottom-color: #58f; font-weight: 600; }
  .tab:disabled { color: #888; cursor: not-allowed; }
  .tab-spacer { flex: 1; }
  .tab-meta { padding: 8px 12px; font-size: 11px; color: #888; align-self: center; }
  .mode-badge { align-self: center; display: inline-flex; align-items: center; gap: 7px;
                margin: 5px 2px; padding: 4px 10px; border-radius: 999px;
                border: 1px solid #8884; font-size: 11px; font-weight: 700;
                letter-spacing: .04em; text-transform: uppercase; }
  .mode-badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #f0a93b; }
  body[data-control-mode="human"] .mode-badge::before { background: #25b889; box-shadow: 0 0 0 3px #25b88922; }
  body[data-control-mode="autonomous"] .mode-badge::before { background: #6d7cff; box-shadow: 0 0 0 3px #6d7cff22; }
  body[data-control-mode="human"] .automation-control,
  body[data-control-mode="human"] .whisper-wrap { display: none !important; }
  body[data-control-mode="autonomous"] .decision-control { display: none !important; }
  /* Main view: two mutually exclusive views, filling the remaining height */
  .views { flex: 1; min-height: 0; position: relative; }
  .view { position: absolute; inset: 0; display: none; }
  .view.active { display: block; }
  #view-business { background: #0001; }
  #view-business iframe { width: 100%; height: 100%; border: 0; display: block; background: #fff0; }
  #view-business .no-ui { padding: 24px; color: #888; font-size: 13px; line-height: 1.6; }
  /* Raw/debug view: two columns (keep the original layout) */
  #view-debug { display: none; }
  #view-debug.active { display: grid; grid-template-columns: 1fr 1fr; }
  .pane { padding: 12px 16px; overflow: auto; border-right: 1px solid #8884; }
  .pane:last-child { border-right: 0; }
  h2 { margin: 4px 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: .04em;
       color: #888; border-bottom: 1px solid #8884; padding-bottom: 4px; }
  .block { margin-bottom: 18px; }
  pre, code { font-family: "Consolas", "Menlo", monospace; font-size: 12px; }
  pre { background: #8881; padding: 8px; border-radius: 4px; overflow: auto; margin: 4px 0; }
  textarea, input[type=text] { width: 100%; box-sizing: border-box; font-family: "Consolas", monospace;
                               font-size: 12px; padding: 6px; border: 1px solid #8884; border-radius: 4px;
                               background: transparent; color: inherit; }
  textarea { min-height: 64px; resize: vertical; }
  button { padding: 6px 14px; border: 1px solid #8884; background: #8881; color: inherit;
           border-radius: 4px; cursor: pointer; font-size: 12px; }
  button:hover { background: #8883; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .warn { color: #c80; font-size: 11px; margin: 4px 0; }
  .toast { position: fixed; top: 12px; right: 12px; padding: 8px 12px; background: #333; color: #fff;
           border-radius: 4px; font-size: 12px; opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 30; }
  .toast.show { opacity: .9; }
  /* Bottom-right whisper FAB + summary capsule (shared by both business and debug views) */
  .whisper-wrap { position: fixed; right: 20px; bottom: 20px; display: flex; align-items: center;
                  gap: 10px; z-index: 25; }
  .whisper-summary { max-width: 280px; padding: 6px 12px; background: var(--whisper-bg, #fff);
                     border: 1px solid #8884; border-radius: 16px; font-size: 12px; color: #666;
                     box-shadow: 0 2px 8px #0002; cursor: pointer;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .whisper-summary:hover { color: inherit; background: #8881; }
  .whisper-summary.empty { display: none; }
  @media (prefers-color-scheme: dark) {
    .whisper-summary { background: #2a2a2a; color: #aaa; }
  }
  .whisper-fab { width: 48px; height: 48px; border-radius: 50%; border: 0; cursor: pointer;
                 background: #58f; color: #fff; font-size: 22px; line-height: 1;
                 box-shadow: 0 4px 12px #0004; transition: transform .15s, box-shadow .15s;
                 position: relative; flex: none; }
  .whisper-fab:hover { transform: translateY(-1px); box-shadow: 0 6px 16px #0005; }
  .whisper-fab.has-whisper::after {
    content: ""; position: absolute; top: 6px; right: 6px; width: 10px; height: 10px;
    border-radius: 50%; background: #5b3; border: 2px solid #fff;
  }
  .whisper-popover { position: fixed; right: 20px; bottom: 80px; width: 380px; max-height: 70vh;
                     background: var(--popover-bg, #fff); color: inherit;
                     border: 1px solid #8884; border-radius: 8px;
                     box-shadow: 0 8px 28px #0004; z-index: 26; display: none;
                     flex-direction: column; }
  @media (prefers-color-scheme: dark) {
    .whisper-popover { background: #222; }
  }
  .whisper-popover.open { display: flex; }
  .whisper-popover header { padding: 10px 14px 6px; border-bottom: 1px solid #8883; }
  .whisper-popover header h3 { margin: 0 0 4px; font-size: 13px; }
  .whisper-popover header .warn { color: #888; font-size: 11px; margin: 0; line-height: 1.4; }
  .whisper-popover .chat { flex: 1; overflow-y: auto; padding: 10px 14px;
                           display: flex; flex-direction: column; gap: 8px; min-height: 120px; }
  .whisper-popover .chat .empty { color: #888; font-size: 12px; text-align: center; padding: 16px 0; }
  .whisper-popover .chat .msg { display: flex; flex-direction: column; max-width: 85%; }
  .whisper-popover .chat .msg.user { align-self: flex-end; align-items: flex-end; }
  .whisper-popover .chat .msg.agent { align-self: flex-start; align-items: flex-start; }
  .whisper-popover .chat .bubble { padding: 6px 10px; border-radius: 10px; font-size: 12px;
                                    line-height: 1.4; word-break: break-word; white-space: pre-wrap; }
  .whisper-popover .chat .msg.user .bubble { background: #58f; color: #fff;
                                              border-bottom-right-radius: 2px; }
  .whisper-popover .chat .msg.agent .bubble { background: #8882; color: inherit;
                                                border-bottom-left-radius: 2px; }
  .whisper-popover .chat .meta { font-size: 10px; color: #999; margin: 2px 4px 0;
                                  display: flex; gap: 4px; align-items: center; }
  .whisper-popover .input-row { padding: 8px 12px 12px; border-top: 1px solid #8883;
                                 display: flex; gap: 6px; align-items: flex-end; }
  .whisper-popover .input-row textarea { min-height: 36px; max-height: 120px; resize: none;
                                          flex: 1; padding: 8px; }
  .whisper-popover .input-row button { padding: 8px 14px; white-space: nowrap; }
  .event { padding: 2px 0; font-size: 12px; border-bottom: 1px dotted #8882; }
  .event .ts { color: #888; }
  .event .tag { display: inline-block; padding: 0 4px; margin-right: 4px; border-radius: 2px; font-size: 10px; }
  .tag.event { background: #57f4; }
  .tag.detail { background: #5b34; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; font-size: 12px; }
  .kv .k { color: #888; }
  fieldset { border: 1px solid #8884; border-radius: 4px; padding: 8px 12px; margin: 4px 0; }
  legend { font-size: 11px; color: #888; padding: 0 4px; }
  label { font-size: 12px; }
  /* Peer-disconnect banner (heartbeat timeout) */
  .peer-banner { position: fixed; top: 0; left: 0; right: 0;
                 padding: 10px 16px; background: #c0392b; color: #fff;
                 text-align: center; font-weight: 600; font-size: 13px;
                 z-index: 9999; display: none;
                 box-shadow: 0 2px 8px #0004; }
  .peer-banner.visible { display: block; }
  .peer-banner.resumed { background: #27ae60; }
  /* v014-M2: embedded coach panel (mirrors whisper popover, anchored bottom-left) */
  .coach-wrap { position: fixed; left: 20px; bottom: 20px; z-index: 1000; }
  .coach-fab { width: 48px; height: 48px; border-radius: 50%; border: 0; cursor: pointer;
               background: #6c5ce7; color: #fff; box-shadow: 0 4px 12px #0004;
               display: flex; align-items: center; justify-content: center;
               transition: transform .16s ease, box-shadow .16s ease, background .16s ease; }
  .coach-fab:hover { transform: translateY(-1px); box-shadow: 0 6px 16px #0005; }
  .coach-fab:focus-visible { outline: 3px solid #a89cff; outline-offset: 3px; }
  .coach-fab svg { width: 24px; height: 24px; fill: none; stroke: currentColor;
                   stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .coach-popover { position: fixed; left: 20px; bottom: 80px; width: 420px; max-height: 70vh;
                   display: none; flex-direction: column; background: var(--whisper-bg, #fff);
                   color: inherit; border-radius: 10px; box-shadow: 0 8px 28px #0006;
                   overflow: hidden; z-index: 1001; }
  @media (prefers-color-scheme: dark) { .coach-popover { background: #222; } }
  .coach-popover.open { display: flex; }
  .coach-popover header { padding: 10px 14px 6px; border-bottom: 1px solid #8883; }
  .coach-popover header h3 { margin: 0 0 4px; font-size: 13px; }
  .coach-popover header .warn { color: #888; font-size: 11px; margin: 0; line-height: 1.4; }
  .coach-popover .chat { flex: 1; overflow-y: auto; padding: 10px 14px;
                         display: flex; flex-direction: column; gap: 8px; }
  .coach-popover .chat .empty { color: #888; font-size: 12px; text-align: center; padding: 16px 0; }
  .coach-popover .chat .msg { display: flex; flex-direction: column; max-width: 85%; }
  .coach-popover .chat .msg.user { align-self: flex-end; align-items: flex-end; }
  .coach-popover .chat .msg.agent { align-self: flex-start; align-items: flex-start; }
  .coach-popover .chat .msg.system { align-self: center; align-items: center; max-width: 100%; }
  .coach-popover .chat .bubble { padding: 6px 10px; border-radius: 10px; font-size: 12px;
                                  white-space: pre-wrap; word-break: break-word; line-height: 1.45; }
  .coach-popover .chat .msg.user .bubble { background: #6c5ce7; color: #fff; }
  .coach-popover .chat .msg.agent .bubble { background: #8882; color: inherit; }
  .coach-popover .chat .msg.agent.err .bubble { background: #c0392b55; }
  .coach-popover .chat .msg.system .bubble { background: transparent; color: #888; font-size: 11px; }
  .coach-popover .chat .msg .adopt { margin-top: 4px; padding: 2px 8px; font-size: 10px;
                                      cursor: pointer; border: 1px solid #8885; background: transparent;
                                      border-radius: 4px; color: inherit; align-self: flex-start; }
  .coach-popover .chat .msg .meta { font-size: 10px; color: #999; margin: 2px 4px 0; }
  .coach-popover .input-row { padding: 8px 12px 12px; border-top: 1px solid #8883;
                              display: flex; gap: 6px; align-items: flex-end; }
  .coach-popover .input-row textarea { min-height: 36px; max-height: 120px; resize: none;
                                       flex: 1; padding: 8px; }
  .coach-popover .input-row button { padding: 8px 14px; white-space: nowrap; }
  @media (max-width: 720px) {
    .tab { padding-inline: 10px; }
    .tab-meta { display: none; }
    .mode-badge { margin-left: 6px; padding-inline: 8px; }
  }
</style>
</head>
<body>

<div id="peer-banner" class="peer-banner"></div>

<div class="tabs">
  <button class="tab" id="tab-business" data-view="business">Business</button>
  <button class="tab" id="tab-debug" data-view="debug">Raw / Debug</button>
  <div class="tab-spacer"></div>
  <div class="mode-badge" id="control-mode-badge" title="Local action source">hybrid</div>
  <div class="tab-meta" id="tab-meta"></div>
</div>

<div class="whisper-wrap">
  <div class="whisper-summary empty" id="whisper-summary" title="Click to open whispers"></div>
  <button class="whisper-fab" id="whisper-toggle"
          title="Whisper: talk to your local agent privately (not sent to peer)">💬</button>
</div>

<div class="whisper-popover" id="whisper-popover">
  <header>
    <h3>Whisper</h3>
    <p class="warn">Talk privately to your local agent. <b>Not sent to the peer.</b>
       Whether this affects decisions depends on the protocol hooks implementation.</p>
  </header>
  <div class="chat" id="whisper-chat">
    <div class="empty">No whispers yet</div>
  </div>
  <div class="input-row">
    <textarea id="whisper" placeholder="Type a message... (Enter to send, Shift+Enter for newline)" rows="1"></textarea>
    <button id="whisper-btn">Send</button>
  </div>
</div>

<div class="coach-wrap">
  <button class="coach-fab" id="coach-toggle" type="button"
          aria-label="Open tactical coach"
          title="Tactical coach: analyze the live game with your agent CLI">
    <svg class="coach-icon" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="4.25"></circle>
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3"></path>
      <path d="M8.75 8.75 6.6 6.6M15.25 8.75l2.15-2.15M8.75 15.25 6.6 17.4M15.25 15.25l2.15 2.15"></path>
      <circle cx="12" cy="12" r=".8" fill="currentColor" stroke="none"></circle>
    </svg>
  </button>
</div>
<div class="coach-popover" id="coach-popover">
  <header>
    <h3>Tactical Coach</h3>
    <p class="warn" id="coach-warn">Analyzes the live game via your own agent CLI. It advises only — it does not play for you.</p>
  </header>
  <div class="chat" id="coach-chat">
    <div class="empty">Ask the coach about the current situation.</div>
  </div>
  <div class="input-row">
    <textarea id="coach-input" placeholder="Ask the coach... (Enter to send, Shift+Enter for newline)" rows="1"></textarea>
    <button id="coach-send">Send</button>
    <button id="coach-reset" title="Clear the conversation and start fresh">Reset</button>
  </div>
</div>

<div class="views">
  <div class="view" id="view-business">
    <div class="no-ui" id="business-fallback" style="display:none">
      <p>No business UI provided by this protocol (<code>protocol_dir/ui/index.html</code> not found).</p>
      <p>Switch to "Raw / Debug" to submit decisions or edit strategy.
         The 💬 whisper panel is available in any view.</p>
      <p>Protocol authors: create <code>ui/index.html</code> under the protocol directory.
         v006 P4: UI iframe runs on an isolated origin (separate local port) and
         communicates with broadcast via <code>postMessage</code> bridge
         (see <code>docs/cn/SKILL.md</code> §UI postMessage protocol).</p>
    </div>
    <iframe id="business-frame" src="about:blank" style="display:none"
            sandbox="allow-scripts allow-same-origin allow-popups allow-modals"
            referrerpolicy="no-referrer"></iframe>
    <!-- v005 legacy fallback: same-origin sandbox for built-in protocol UIs
         not yet migrated to postMessage bridge. Detected at runtime. -->
    <iframe id="business-frame-legacy" src="about:blank" style="display:none"
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-modals"></iframe>
  </div>

  <div class="view" id="view-debug">
    <div class="pane">
      <div class="block">
        <h2>Snapshot</h2>
        <div id="snapshot" class="kv"></div>
      </div>

      <div class="block decision-control">
        <h2>Submit Decision</h2>
        <p class="warn">Spec-constrained structured decision, written to DecisionBus, consumed by hooks.</p>
        <div id="decision-form"></div>
        <details>
          <summary style="font-size:12px;cursor:pointer">Raw JSON (Advanced)</summary>
          <textarea id="decision-raw" placeholder='{"action":"commit","value":"rock"}'></textarea>
          <div class="row" style="margin-top:6px"><button id="decision-raw-btn">Submit</button></div>
        </details>
      </div>

      <div class="block automation-control">
        <h2>Strategy JSON</h2>
        <p class="warn">Written to strategy.json, read by hooks before each decision round. Must be a JSON object.</p>
        <textarea id="strategy" rows="6"></textarea>
        <div class="row" style="margin-top:6px">
          <button id="strategy-merge">Merge Save</button>
          <button id="strategy-set">Overwrite Save</button>
          <button id="strategy-reload">Reload from Disk</button>
        </div>
      </div>
    </div>

    <div class="pane">
      <h2>Event Stream</h2>
      <div id="events"></div>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
const toast = (msg, ok=true) => {
  const el = $("#toast"); el.textContent = msg;
  el.style.background = ok ? "#333" : "#933";
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1500);
};

let currentSnapshot = {};
let runtimeInfo = { control_mode: "hybrid" };

async function loadRuntimeInfo() {
  try {
    const r = await fetch("/api/info");
    if (r.ok) runtimeInfo = await r.json();
  } catch (_) {}
  const mode = runtimeInfo.control_mode || "hybrid";
  document.body.dataset.controlMode = mode;
  const badge = $("#control-mode-badge");
  badge.textContent = mode;
  badge.title = mode === "human"
    ? "Every local action requires an explicit human submission"
    : mode === "autonomous"
      ? "Local actions are generated automatically"
      : "Automatic play with human intervention enabled";
}

function renderSnapshot(snap) {
  currentSnapshot = snap || {};
  const fields = ["phase", "role", "round", "protocol_name", "protocol_id", "score", "updated_at"];
  const html = fields
    .filter(k => snap && snap[k] !== undefined)
    .map(k => {
      const v = typeof snap[k] === "object" ? JSON.stringify(snap[k]) : String(snap[k]);
      return `<div class="k">${k}</div><div>${escapeHtml(v)}</div>`;
    }).join("");
  $("#snapshot").innerHTML = html || '<div class="k">(Snapshot not yet available)</div>';
  renderDecisionForm(snap);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function renderDecisionForm(snap) {
  const phase = snap && snap.phase;
  const container = $("#decision-form");
  // Constrained built-in rendering: for common turn-based protocols with a known phase, provide radio
  // buttons; otherwise prompt the user to use the raw JSON section.
  if (phase === "commit" || phase === "reveal" || phase === "throw") {
    container.innerHTML = `
      <div class="row">
        <label><input type="radio" name="rps" value="rock"> Rock</label>
        <label><input type="radio" name="rps" value="paper"> Paper</label>
        <label><input type="radio" name="rps" value="scissors"> Scissors</label>
      </div>
      <div class="row" style="margin-top:6px">
        <button id="rps-submit">Submit (action=${escapeHtml(phase)})</button>
      </div>`;
    $("#rps-submit").onclick = () => {
      const v = document.querySelector('input[name=rps]:checked');
      if (!v) { toast("Select one first", false); return; }
      submitDecision({ action: phase, value: v.value });
    };
  } else if (phase === "guess") {
    container.innerHTML = `
      <div class="row">
        <input type="text" id="guess-input" placeholder="Enter your guess" style="max-width:160px">
        <button id="guess-submit">Submit</button>
      </div>`;
    $("#guess-submit").onclick = () => {
      const v = $("#guess-input").value.trim();
      if (!v) return;
      submitDecision({ action: "guess", value: Number(v) });
    };
  } else {
    container.innerHTML = `<p class="warn">Current phase=<code>${escapeHtml(phase || "?")}</code>,
      no built-in renderer. Use the Raw JSON section below.</p>`;
  }
}

async function submitDecision(decision) {
  const r = await fetch("/api/decide", { method: "POST", body: JSON.stringify(decision) });
  if (r.ok) toast("Decision submitted"); else toast("Submit failed " + r.status, false);
}

$("#decision-raw-btn").onclick = async () => {
  try {
    const obj = JSON.parse($("#decision-raw").value);
    await submitDecision(obj);
  } catch (e) { toast("JSON parse failed", false); }
};

$("#strategy-merge").onclick = async () => {
  try {
    const obj = JSON.parse($("#strategy").value);
    const r = await fetch("/api/strategy/merge", { method: "POST", body: JSON.stringify(obj) });
    if (r.ok) toast("Strategy merged"); else toast("Failed " + r.status, false);
  } catch (e) { toast("JSON parse failed", false); }
};
$("#strategy-set").onclick = async () => {
  try {
    const obj = JSON.parse($("#strategy").value);
    const r = await fetch("/api/strategy/set", { method: "POST", body: JSON.stringify(obj) });
    if (r.ok) toast("Strategy overwritten"); else toast("Failed " + r.status, false);
  } catch (e) { toast("JSON parse failed", false); }
};
$("#strategy-reload").onclick = async () => {
  const r = await fetch("/api/strategy"); const d = await r.json();
  $("#strategy").value = JSON.stringify(d, null, 2);
  toast("Reloaded");
};
// Whisper state
const whispersState = [];  // accumulates all entries, sorted by timestamp

function renderWhisperList() {
  const box = $("#whisper-chat");
  if (whispersState.length === 0) {
    box.innerHTML = '<div class="empty">No whispers yet</div>';
    return;
  }
  // Sort by ts (append order is usually already correct, but this is a safeguard when SSE full
  // and incremental updates are mixed)
  const sorted = [...whispersState].sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
  box.innerHTML = sorted.map(w => {
    const role = w.role === "agent" ? "agent" : "user";
    const who = role === "agent" ? "Agent" : "Me";
    const time = fmtTime(w.ts);
    return `<div class="msg ${role}">
      <div class="bubble">${escapeHtml(w.text || "")}</div>
      <div class="meta"><span>${who}</span><span>·</span><span>${time}</span></div>
    </div>`;
  }).join("");
  // Auto-scroll to bottom
  box.scrollTop = box.scrollHeight;
}

function fmtTime(ts) {
  if (!ts) return "";
  // ts: ISO-8601 UTC, rendered as local HH:MM:SS
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour12: false });
  } catch (_) {
    return ts.slice(11, 19);
  }
}

function applyWhispersFull(list) {
  whispersState.length = 0;
  if (Array.isArray(list)) whispersState.push(...list);
  renderWhisperList();
  updateWhisperFab();
}

function applyWhisperIncr(entry) {
  if (!entry || typeof entry !== "object") return;
  whispersState.push(entry);
  renderWhisperList();
  updateWhisperFab();
}

function updateWhisperFab() {
  const fab = $("#whisper-toggle");
  const summary = $("#whisper-summary");
  if (whispersState.length === 0) {
    fab.classList.remove("has-whisper");
    summary.classList.add("empty");
    summary.textContent = "";
    summary.title = "";
    return;
  }
  // Find the latest user input for the summary preview (agent replies are not surfaced on the capsule)
  const lastUser = [...whispersState].reverse().find(w => w.role !== "agent");
  fab.classList.add("has-whisper");
  if (lastUser && lastUser.text) {
    const t = lastUser.text;
    summary.classList.remove("empty");
    summary.textContent = t.length > 30 ? t.slice(0, 30) + "…" : t;
    summary.title = t;
  } else {
    summary.classList.add("empty");
    summary.textContent = "";
  }
}

async function sendWhisper() {
  const ta = $("#whisper");
  const text = ta.value.trim();
  if (!text) return;
  ta.disabled = true;
  $("#whisper-btn").disabled = true;
  try {
    const r = await fetch("/api/whisper", { method: "POST", body: JSON.stringify({ text }) });
    if (r.ok) {
      ta.value = "";  // clear after send
      // Do not push locally; wait for the SSE whisper increment to come back before rendering
    } else {
      toast("Send failed " + r.status, false);
    }
  } finally {
    ta.disabled = false;
    $("#whisper-btn").disabled = false;
    ta.focus();
  }
}

$("#whisper-btn").addEventListener("click", sendWhisper);
$("#whisper").addEventListener("keydown", (e) => {
  // Enter to send, Shift+Enter for newline
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendWhisper();
  }
});

// Bottom-right whisper popover toggle
function openWhisperPopover() {
  $("#whisper-popover").classList.add("open");
  $("#whisper").focus();
  // Scroll to bottom on open (see the latest)
  const chat = $("#whisper-chat");
  chat.scrollTop = chat.scrollHeight;
}
function closeWhisperPopover() {
  $("#whisper-popover").classList.remove("open");
}
$("#whisper-toggle").addEventListener("click", (e) => {
  e.stopPropagation();
  const pop = $("#whisper-popover");
  if (pop.classList.contains("open")) closeWhisperPopover(); else openWhisperPopover();
});
$("#whisper-summary").addEventListener("click", (e) => {
  e.stopPropagation();
  openWhisperPopover();
});
document.addEventListener("click", (e) => {
  const pop = $("#whisper-popover");
  if (!pop.classList.contains("open")) return;
  if (pop.contains(e.target)) return;
  if ($("#whisper-toggle").contains(e.target)) return;
  if ($("#whisper-summary").contains(e.target)) return;
  closeWhisperPopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeWhisperPopover();
});

// ---------- v014-M2: embedded coach ----------
const coachState = [];            // dialog entries: {role, text, ts, turn_id?, pending?}
const coachPending = {};          // turn_id -> accumulated chunk text
function escapeAttr(s) { return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function renderCoachList() {
  const box = $("#coach-chat");
  if (coachState.length === 0) {
    box.innerHTML = '<div class="empty">Ask the coach about the current situation.</div>';
    return;
  }
  box.innerHTML = coachState.map(c => {
    const cls = c.role === "user" ? "user" : (c.role === "error" ? "agent err" : (c.role === "system" ? "system" : "agent"));
    const who = c.role === "user" ? "Me" : (c.role === "error" ? "Error" : (c.role === "system" ? "" : "Coach"));
    const body = (c.pending && !c.text) ? "…" : (c.text || "");
    const adopt = (c.role === "coach" && c.text && !c.pending)
      ? `<button class="adopt" data-text="${escapeAttr(c.text)}">Adopt as hint</button>` : "";
    const meta = who ? `<div class="meta"><span>${who}</span><span>·</span><span>${fmtTime(c.ts)}</span></div>` : "";
    return `<div class="msg ${cls}"><div class="bubble">${escapeHtml(body)}</div>${meta}${adopt}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}
function coachLastBubble(tid) {
  for (let i = coachState.length - 1; i >= 0; i--)
    if (coachState[i].turn_id === tid && coachState[i].role !== "user") return coachState[i];
  return null;
}
function handleCoachEvent(event, payload) {
  const tid = payload && payload.turn_id;
  if (event === "coach_history") { coachState.push(payload); renderCoachList(); return; }
  if (event === "coach_reset") { coachState.length = 0; renderCoachList(); return; }
  if (event === "coach_context_reset") { coachState.push({role:"system", text:"— coach session reset (previous context was lost) —", ts:payload.ts}); renderCoachList(); return; }
  if (event === "coach_turn_start") { coachPending[tid] = ""; coachState.push({role:"coach", text:"", ts:payload.ts, turn_id:tid, pending:true}); renderCoachList(); return; }
  if (event === "coach_chunk") { if (tid in coachPending) coachPending[tid] += payload.text; const b = coachLastBubble(tid); if (b) { b.text = coachPending[tid] || ""; renderCoachList(); } return; }
  if (event === "coach_turn_done") { const b = coachLastBubble(tid); if (b) { b.text = payload.text; b.pending = false; b.ts = payload.ts; } delete coachPending[tid]; renderCoachList(); return; }
  if (event === "coach_turn_error") { const b = coachLastBubble(tid); if (b) { b.role = "error"; b.text = payload.error; b.pending = false; } else coachState.push({role:"error", text:payload.error, ts:payload.ts, turn_id:tid}); delete coachPending[tid]; renderCoachList(); return; }
}
const coachEs = new EventSource("/api/coach/stream");
["coach_history","coach_turn_start","coach_chunk","coach_turn_done","coach_turn_error","coach_reset","coach_context_reset"].forEach(ev =>
  coachEs.addEventListener(ev, e => { try { handleCoachEvent(ev, JSON.parse(e.data)); } catch(_){} }));
coachEs.onerror = () => { /* browser auto-reconnects */ };
async function sendCoach() {
  const ta = $("#coach-input"); const text = ta.value.trim(); if (!text) return;
  ta.disabled = true; $("#coach-send").disabled = true;
  try {
    const r = await fetch("/api/coach/send", { method: "POST", body: JSON.stringify({ text }) });
    const d = await r.json();
    if (r.ok && d.turn_id) {
      ta.value = "";
      coachState.push({ role: "user", text, ts: new Date().toISOString(), turn_id: d.turn_id });
      renderCoachList();
    } else { toast("Coach send failed " + r.status, false); }
  } finally { ta.disabled = false; $("#coach-send").disabled = false; ta.focus(); }
}
$("#coach-send").addEventListener("click", sendCoach);
$("#coach-input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendCoach(); } });
$("#coach-reset").addEventListener("click", async () => { if (!confirm("Reset the coach conversation? Clears all history.")) return; await fetch("/api/coach/reset", { method: "POST" }); });
$("#coach-chat").addEventListener("click", async e => {
  const btn = e.target.closest("button.adopt"); if (!btn) return;
  const text = btn.getAttribute("data-text");
  const r = await fetch("/api/whisper", { method: "POST", body: JSON.stringify({ text, role: "user" }) });
  toast(r.ok ? "Adopted as tactical hint" : "Adopt failed " + r.status, r.ok);
});
function openCoachPopover() { $("#coach-popover").classList.add("open"); $("#coach-input").focus(); const c = $("#coach-chat"); c.scrollTop = c.scrollHeight; }
function closeCoachPopover() { $("#coach-popover").classList.remove("open"); }
$("#coach-toggle").addEventListener("click", e => { e.stopPropagation(); const p = $("#coach-popover"); if (p.classList.contains("open")) closeCoachPopover(); else openCoachPopover(); });
document.addEventListener("click", e => { const p = $("#coach-popover"); if (!p.classList.contains("open")) return; if (p.contains(e.target)) return; if ($("#coach-toggle").contains(e.target)) return; closeCoachPopover(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeCoachPopover(); });
fetch("/api/coach/config").then(r => r.json()).then(c => { if (c && c.user_agent) $("#coach-warn").textContent = `Coach backend: ${c.user_agent}. Analyzes the live game; it advises only — it does not play for you.`; }).catch(() => {});

function appendEvent(kind, payload) {
  const el = document.createElement("div");
  el.className = "event";
  const ts = (payload.ts || "").slice(11, 19);
  const summary = payload.summary || payload.type || JSON.stringify(payload).slice(0, 200);
  el.innerHTML = `<span class="ts">${ts}</span> <span class="tag ${kind}">${kind}</span> ${escapeHtml(summary)}`;
  const box = $("#events");
  box.insertBefore(el, box.firstChild);
  while (box.childNodes.length > 500) box.removeChild(box.lastChild);
}

// SSE
const es = new EventSource("/sse/stream");
es.addEventListener("snapshot", e => renderSnapshot(JSON.parse(e.data)));
es.addEventListener("strategy", e => {
  const st = JSON.parse(e.data);
  $("#strategy").value = JSON.stringify(st, null, 2);
});
es.addEventListener("whispers", e => applyWhispersFull(JSON.parse(e.data)));
es.addEventListener("whisper", e => applyWhisperIncr(JSON.parse(e.data)));
es.addEventListener("event", e => {
  const ev = JSON.parse(e.data);
  handlePeerEvent(ev);
  appendEvent("event", ev);
});
es.addEventListener("detail", e => appendEvent("detail", JSON.parse(e.data)));
es.onerror = () => { /* browser auto-reconnects */ };

// Peer-disconnect banner + title blink
const peerBanner = $("#peer-banner");
let titleBlinker = null;
const originalTitle = document.title;
function setPeerBanner(text, kind) {
  peerBanner.textContent = text;
  peerBanner.classList.toggle("resumed", kind === "resumed");
  peerBanner.classList.add("visible");
  if (kind === "resumed") {
    if (titleBlinker) { clearInterval(titleBlinker); titleBlinker = null; }
    document.title = originalTitle;
    setTimeout(() => peerBanner.classList.remove("visible"), 3000);
    return;
  }
  if (titleBlinker) return;
  let on = true;
  titleBlinker = setInterval(() => {
    document.title = on ? "(!) " + originalTitle : originalTitle;
    on = !on;
  }, 1000);
}
function clearPeerBanner() {
  peerBanner.classList.remove("visible");
  if (titleBlinker) { clearInterval(titleBlinker); titleBlinker = null; }
  document.title = originalTitle;
}
function handlePeerEvent(ev) {
  if (!ev || !ev.type) return;
  if (ev.type === "peer_unresponsive") {
    const elapsed = (ev.data && ev.data.elapsed) || "?";
    setPeerBanner("Peer unresponsive for " + elapsed + "s, connection may be lost", "unresponsive");
  } else if (ev.type === "peer_resumed") {
    setPeerBanner("Peer reconnected", "resumed");
  } else if (ev.type === "session_ended" && ev.data && ev.data.reason === "aborted_by_agent") {
    if (titleBlinker) { clearInterval(titleBlinker); titleBlinker = null; }
    document.title = originalTitle;
    peerBanner.classList.remove("resumed");
    setPeerBanner("Session aborted by agent", "ended");
  }
}

// Tab switching: business (iframe) / raw (existing panes)
function switchTab(view) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + view));
  try { localStorage.setItem("aigenora.tab", view); } catch (_) {}
}
document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.view));
});

async function fetchUiAvailable() {
  try {
    const r = await fetch("/api/ui-available");
    if (r.ok) return await r.json();
  } catch (_) { /* ignore */ }
  return { available: false };
}

function applyUiAvailable(info) {
  const frame = $("#business-frame");
  const legacyFrame = $("#business-frame-legacy");
  const fallback = $("#business-fallback");
  if (info.available) {
    // v006 P4: ui_origin = isolated origin (random port); iframe communicates via postMessage bridge
    // legacy_mode = true → use same-origin sandbox for old UI (v005 protocols not yet migrated)
    const uiOrigin = info.ui_origin;        // e.g. "http://127.0.0.1:54321"
    const legacyMode = !!info.legacy_mode;
    if (legacyMode || !uiOrigin) {
      // Compatibility period: load /ui/index.html directly, same-origin
      legacyFrame.src = "/ui/index.html";
      legacyFrame.style.display = "block";
      frame.style.display = "none";
    } else {
      // P4 isolated mode: isolated origin + postMessage
      frame.src = `${uiOrigin}/index.html?parent=${encodeURIComponent(location.origin)}`;
      frame.style.display = "block";
      legacyFrame.style.display = "none";
    }
    fallback.style.display = "none";
    $("#tab-meta").textContent = info.protocol_name || "";
    $("#tab-business").disabled = false;
    document.body.dataset.hasBusinessUi = "true";
    $(".whisper-wrap").style.display = "none";
  } else {
    frame.style.display = "none";
    legacyFrame.style.display = "none";
    fallback.style.display = "block";
    $("#tab-business").disabled = true;
    $("#tab-meta").textContent = "No business UI";
  }
}

// Poll until /api/ui-available reports available (or give up). The protocol dir may
// not be resolved yet at first load — e.g. a Guest opening the page before peer_joined
// lands its protocol_dir into session.json. Re-polling lets the panel appear once it's
// ready, and hides the floating whisper box at that point (so both boxes never coexist).
let _uiApplied = false;
function pollUiAvailable() {
  if (_uiApplied) return;
  fetchUiAvailable().then(info => {
    if (_uiApplied) return;
    if (info.available) {
      _uiApplied = true;
      applyUiAvailable(info);
    }
  });
}

async function initUiTab() {
  const info = await fetchUiAvailable();
  applyUiAvailable(info);
  if (info.available) {
    _uiApplied = true;
  } else {
    // Not ready yet — retry every 2s for up to ~30s, then stop (page reload will recheck).
    let tries = 0;
    const iv = setInterval(() => {
      if (_uiApplied || ++tries > 15) { clearInterval(iv); return; }
      pollUiAvailable();
    }, 2000);
  }
  // Default tab: business if available, otherwise debug; localStorage takes precedence
  let preferred = null;
  try { preferred = localStorage.getItem("aigenora.tab"); } catch (_) {}
  if (preferred === "business" && !info.available) preferred = "debug";
  switchTab(preferred || (info.available ? "business" : "debug"));
}

// v006 P4: postMessage bridge — UI iframe accesses /api/* / /sse/stream through this channel.
// The parent page strictly validates event.origin === uiOrigin and only responds to the aigenora-ui source.
(function setupUiBridge() {
  let _uiOrigin = null;
  let _granted = new Set();

  // Take uiOrigin from query string / bootstrap (locked on first hello)
  window.addEventListener("message", async (ev) => {
    const data = ev.data || {};
    if (data.source !== "aigenora-ui") return;
    if (_uiOrigin === null) {
      _uiOrigin = ev.origin;  // lock the first source
    } else if (_uiOrigin !== ev.origin) {
      return;  // reject other origins
    }
    try {
      const result = await _handleUiRequest(data);
      ev.source.postMessage({
        source: "aigenora-broadcast",
        type: "response",
        id: data.id,
        ...result,
      }, _uiOrigin);
    } catch (err) {
      ev.source.postMessage({
        source: "aigenora-broadcast",
        type: "response",
        id: data.id,
        ok: false,
        error: String(err),
      }, _uiOrigin);
    }
  });

  async function _handleUiRequest(msg) {
    if (msg.type === "hello") {
      // Capability negotiation: the iframe declares capabilities, the parent returns granted
      const requested = new Set(msg.capabilities || []);
      const supported = ["info", "snapshot", "strategy", "orders", "whisper", "decide", "details", "events"];
      _granted = new Set([...requested].filter(c => supported.includes(c)));
      return { type: "hello-ack", ok: true, granted: [..._granted] };
    }
    if (msg.type !== "request") {
      return { ok: false, error: "unknown message type: " + msg.type };
    }
    if (!_granted.has(msg.method)) {
      return { ok: false, error: "method not granted: " + msg.method };
    }
    // Same-origin forward to broadcast /api/*
    const methodMap = {
      info:     { path: "/api/info", method: "GET" },
      snapshot: { path: "/api/snapshot", method: "GET" },
      strategy: { path: "/api/strategy", method: "GET" },
      orders:   { path: "/api/strategy/merge", method: "POST" },
      whisper:  { path: "/api/whisper", method: "POST" },
      details:  { path: "/api/details", method: "GET" },
      events:   { path: "/api/events", method: "GET" },
      decide:   { path: "/api/decide", method: "POST" },
    };
    const route = methodMap[msg.method];
    if (!route) return { ok: false, error: "unknown method: " + msg.method };
    const init = { method: route.method, headers: { "Content-Type": "application/json" } };
    if (route.method === "POST" && msg.body) init.body = JSON.stringify(msg.body);
    const r = await fetch(route.path, init);
    let data = null;
    try { data = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, data };
  }

  // Push SSE updates to the UI iframe: poll /api/snapshot periodically and push
  let _lastPush = "";
  let _lastPushAt = 0;
  setInterval(async () => {
    if (!_uiOrigin || _granted.size === 0) return;
    const frame = document.getElementById("business-frame");
    if (!frame || !frame.contentWindow) return;
    // Only push when granted contains snapshot
    if (!_granted.has("snapshot")) return;
    try {
      const r = await fetch("/api/snapshot");
      const snap = await r.json();
      const sig = JSON.stringify(snap);
      const now = Date.now();
      // postMessage is fire-and-forget. A changed frame can be sent while the
      // iframe is navigating or temporarily detached, so a signature-only
      // de-duplication can lose the terminal frame forever. Re-emit the latest
      // replacement snapshot once per second; changed real-time frames still
      // flow at the 100 ms polling cadence.
      if (sig === _lastPush && now - _lastPushAt < 1000) return;
      frame.contentWindow.postMessage({
        source: "aigenora-broadcast",
        type: "push",
        event: "snapshot",
        data: snap,
      }, _uiOrigin);
      _lastPush = sig;
      _lastPushAt = now;
    } catch (_) {}
  // Real-time protocol snapshots are overwritten rather than appended, so a 100ms
  // bridge poll stays bounded and matches the reference RTS 10 Hz state stream.
  }, 100);
})();

// Initial fetch
(async () => {
  await loadRuntimeInfo();
  const s = await fetch("/api/snapshot").then(r => r.json());
  renderSnapshot(s);
  const st = await fetch("/api/strategy").then(r => r.json());
  $("#strategy").value = JSON.stringify(st, null, 2);
  const wlist = await fetch("/api/whispers").then(r => r.json()).catch(() => []);
  applyWhispersFull(wlist);
  await initUiTab();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# SSE push: a background thread polls for file changes and broadcasts to all subscribers

class _Broadcaster:
    """N SSE clients + 1 background polling thread.

    Polling sources: snapshot.json mtime, strategy.json mtime, line-count deltas of events.jsonl/details.jsonl.

    Important: root_dir may be the daemon's parent directory (no child subdirectory yet at startup).
    Each loop iteration re-runs resolve_state_dir(root_dir); when the effective directory switches from
    parent to child, all counters are reset and a full snapshot/strategy is broadcast once, so the browser
    side receives the real business state.
    """

    POLL_INTERVAL = 0.5  # seconds

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        # The state_dir field keeps the current effective directory, for external observation / test assertions
        self.state_dir = resolve_state_dir(self.root_dir)
        self._subs: list[list[tuple[str, str]]] = []  # one pending-send queue per subscriber
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Dynamic resolve: the bus is built per read against the current effective directory (cheap)

    def _effective(self) -> Path:
        return resolve_state_dir(self.root_dir)

    def _read_snapshot(self) -> Any:
        return SnapshotBus(self._effective()).read()

    def _read_strategy(self) -> Any:
        return StrategyStore(self._effective()).read()

    def _read_whispers(self) -> Any:
        return WhisperLog(self._effective()).read_with_acks()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self) -> list[tuple[str, str]]:
        q: list[tuple[str, str]] = []
        with self._lock:
            self._subs.append(q)
        # New subscriber: immediately push a full snapshot (against the current effective directory)
        self._enqueue_one(q, "snapshot", self._read_snapshot())
        self._enqueue_one(q, "strategy", self._read_strategy())
        # Whisper history pushed in full once (the frontend renders by timestamp)
        self._enqueue_one(q, "whispers", self._read_whispers())
        # Replay of historical events / details: lets the business UI rebuild history after refresh.
        # Note: this is decoupled from the *_seen counters in _loop — subscribe is a full push for a new
        # subscriber; _loop maintains the broadcaster's own position, and a new subscriber must get
        # everything from 0.
        effective = self._effective()
        for e in EventBus(effective).read_events():
            self._enqueue_one(q, "event", e)
        for d in DetailLog(effective).read_all():
            self._enqueue_one(q, "detail", d)
        return q

    def unsubscribe(self, q: list) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _enqueue_one(self, q: list, event: str, payload: Any) -> None:
        q.append((event, json.dumps(payload, ensure_ascii=False)))

    def _broadcast(self, event: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            for q in self._subs:
                q.append((event, data))

    def _forward_parent_state(self, parent: Path, child: Path) -> None:
        """Carry strategy.json / whispers.jsonl from the daemon parent dir into the nested
        business child dir when the effective directory switches parent -> child.

        Why: in daemon mode the business subprocess runs in a nested <role>-<ts>/ under the
        parent. If a user (or test) injects strategy/whisper via the web API before the child
        exists, the write lands in parent/ and the subprocess (which only reads child/) never
        sees it -- an "orphan". This runs once at the switch moment so early injections survive.
        """
        if parent == child:
            return
        # strategy.json: copy parent's only if child has none (never overwrite runtime state).
        p_strat = parent / "strategy.json"
        c_strat = child / "strategy.json"
        if p_strat.exists() and not c_strat.exists():
            try:
                c_strat.write_bytes(p_strat.read_bytes())
            except Exception:
                pass
        # whispers.jsonl: append parent entries the child doesn't already have (dedup by id).
        p_w = parent / "whispers.jsonl"
        if not p_w.exists():
            return
        try:
            seen: set = set()
            c_w = child / "whispers.jsonl"
            if c_w.exists():
                for raw in c_w.read_text(encoding="utf-8").splitlines():
                    try:
                        seen.add(json.loads(raw).get("id"))
                    except Exception:
                        pass
            with open(c_w, "a", encoding="utf-8") as f:
                for raw in p_w.read_text(encoding="utf-8").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        wid = json.loads(raw).get("id")
                    except Exception:
                        wid = None
                    if wid and wid in seen:
                        continue
                    f.write(raw + "\n")
        except Exception:
            pass

    def _loop(self) -> None:
        # Each effective directory maintains its own counters; all are reset on switch
        effective = self._effective()
        self.state_dir = effective
        snap_mtime = 0.0
        strat_mtime = 0.0
        events_seen = 0
        details_seen = 0
        whispers_seen = 0
        acks_seen = 0
        while not self._stop.is_set():
            try:
                cur = self._effective()
                if cur != effective:
                    # parent -> child switch: forward any parent-phase strategy/whispers so they
                    # reach the subprocess living in the child dir, then reset counters.
                    old_effective = effective
                    effective = cur
                    self.state_dir = cur
                    self._forward_parent_state(old_effective, cur)
                    snap_mtime = 0.0
                    strat_mtime = 0.0
                    events_seen = 0
                    details_seen = 0
                    whispers_seen = 0
                    acks_seen = 0
                    self._broadcast("snapshot", self._read_snapshot())
                    self._broadcast("strategy", self._read_strategy())
                    self._broadcast("whispers", self._read_whispers())

                snap_path = effective / "snapshot.json"
                strat_path = effective / "strategy.json"
                if snap_path.exists():
                    m = snap_path.stat().st_mtime
                    if m != snap_mtime:
                        snap_mtime = m
                        self._broadcast("snapshot", SnapshotBus(effective).read())
                if strat_path.exists():
                    m = strat_path.stat().st_mtime
                    if m != strat_mtime:
                        strat_mtime = m
                        self._broadcast("strategy", StrategyStore(effective).read())
                all_events = EventBus(effective).read_events()
                if len(all_events) > events_seen:
                    for e in all_events[events_seen:]:
                        self._broadcast("event", e)
                    events_seen = len(all_events)
                all_details = DetailLog(effective).read_all()
                if len(all_details) > details_seen:
                    for d in all_details[details_seen:]:
                        self._broadcast("detail", d)
                    details_seen = len(all_details)
                # Whisper deltas: broadcast each new entry as a whisper event
                all_whispers = WhisperLog(effective).read_all()
                if len(all_whispers) > whispers_seen:
                    for w in all_whispers[whispers_seen:]:
                        self._broadcast("whisper", w)
                    whispers_seen = len(all_whispers)
                # ack deltas: broadcast each new ack as a whisper_ack event
                wlog = WhisperLog(effective)
                all_acks = wlog.read_acks()
                if len(all_acks) > acks_seen:
                    for a in all_acks[acks_seen:]:
                        self._broadcast("whisper_ack", a)
                    acks_seen = len(all_acks)
            except Exception:
                # Ignore file races and similar; retry next round
                pass
            self._stop.wait(self.POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP Handler

def _make_handler(root_dir: Path, bc: _Broadcaster, coach: "CoachWorker | None" = None, ui_origin: str | None = None):
    """Build the HTTP handler. root_dir is the original directory passed by the user/CLI (may be a daemon parent).
    Each request re-runs resolve_state_dir to avoid being frozen on the parent before a child is generated.

    v006 P4: ui_origin is the origin of the isolated UI server (e.g. "http://127.0.0.1:54321"), used to
    (1) inject the iframe URL; (2) add it to the parent page CSP frame-src allowlist.
    None means UI is not started or legacy fallback is in use.
    """
    root_dir = Path(root_dir)

    def effective() -> Path:
        return resolve_state_dir(root_dir)

    # Closure context for _handle_ui_available to access ui_origin
    class _Ctx:
        pass
    handler_ctx = _Ctx()
    handler_ctx.ui_origin = ui_origin

    class Handler(BaseHTTPRequestHandler):
        # Silence access logs (avoid SSE long-connection spam)
        def log_message(self, format, *args):  # noqa: A002
            return

        # v006 P4: parent page CSP — frame-src is strict to ui_origin
        def _send_html_with_csp(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"
            if ui_origin:
                csp += f"; frame-src {ui_origin}"
            self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(data)

        # -------- helpers
        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                return None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _BadRequest(f"request body must be valid utf-8: {exc}") from exc
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise _BadRequest(f"invalid json: {exc}") from exc

        # -------- helpers for _meta extraction
        @staticmethod
        def _extract_meta(body: dict) -> dict:
            """Extract audit fields from the request body and assemble _meta (does not mutate the original body)."""
            from datetime import datetime as _dt, timezone as _tz
            meta: dict[str, Any] = {
                "request_ts": _dt.now(_tz.utc).isoformat(),
            }
            if "agent_id" in body:
                meta["agent_id"] = body["agent_id"]
            if "idempotency_key" in body:
                meta["idempotency_key"] = body["idempotency_key"]
            if "caused_by_whisper_id" in body:
                meta["caused_by_whisper_id"] = body["caused_by_whisper_id"]
            return meta

        # -------- routing
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/" or path == "/index.html":
                    return self._send_html_with_csp(_INDEX_HTML)
                if path == "/api/snapshot":
                    return self._send_json(200, SnapshotBus(effective()).read())
                if path == "/api/strategy":
                    return self._send_json(200, StrategyStore(effective()).read())
                if path == "/api/whispers":
                    return self._send_json(200, WhisperLog(effective()).read_with_acks())
                if path == "/api/events":
                    return self._send_json(200, EventBus(effective()).read_events())
                if path == "/api/details":
                    return self._send_json(200, DetailLog(effective()).read_all())
                if path == "/api/info":
                    return self._send_json(200, session_runtime_info(root_dir))
                if path == "/api/ui-available":
                    return self._handle_ui_available()
                if path.startswith("/ui/"):
                    return self._serve_ui_static(path[len("/ui/"):])
                if path.startswith("/assets/"):
                    return self._serve_assets(path[len("/assets/"):])
                if path == "/sse/stream":
                    return self._serve_sse()
                if path == "/api/coach/config":
                    return self._send_json(200, coach.config_view() if coach else {"enabled": False})
                if path == "/api/coach/dialog":
                    return self._send_json(200, coach.dialog_history() if coach else [])
                if path == "/api/coach/stream":
                    return self._serve_coach_sse()
                return self._send_text(404, "not found")
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._read_json_body()
                cur = effective()
                if path == "/api/decide":
                    if not isinstance(body, dict):
                        return self._send_json(400, {"error": "decision must be a JSON object"})
                    if session_runtime_info(root_dir).get("control_mode") == "autonomous":
                        return self._send_json(409, {
                            "ok": False,
                            "reason": "control_mode_autonomous",
                            "error": "direct decisions are disabled in autonomous mode",
                        })
                    result = submit_decision(
                        str(cur), body,
                        origin="web",
                        require_match_key=True,
                        agent_id=body.get("agent_id"),
                        idempotency_key=body.get("idempotency_key"),
                        caused_by_whisper_id=body.get("caused_by_whisper_id"),
                    )
                    if result["ok"]:
                        return self._send_json(200, result)
                    reason = result.get("reason")
                    if reason == "decision_finalized":
                        return self._send_json(409, result)
                    return self._send_json(400, result)
                if path == "/api/strategy/merge":
                    if not isinstance(body, dict):
                        return self._send_json(400, {"error": "patch must be a JSON object"})
                    meta = self._extract_meta(body)
                    new = StrategyStore(cur).merge(body, _meta=meta)
                    return self._send_json(200, new)
                if path == "/api/strategy/set":
                    if not isinstance(body, dict):
                        return self._send_json(400, {"error": "strategy must be a JSON object"})
                    meta = self._extract_meta(body)
                    StrategyStore(cur).write(body, _meta=meta)
                    return self._send_json(200, body)
                if path == "/api/operator-hint":
                    # Legacy-compatible interface: synchronously write strategy.operator_hint and whispers.jsonl (user role)
                    if not isinstance(body, dict) or "text" not in body:
                        return self._send_json(400, {"error": "expected {\"text\": str}"})
                    text = str(body.get("text", "")).strip()
                    new = StrategyStore(cur).merge({"operator_hint": text})
                    if text:
                        WhisperLog(cur).append("user", text)
                    return self._send_json(200, new)
                if path == "/api/whisper":
                    # v005a: supports origin/agent_id; the server pins origin="web"
                    if not isinstance(body, dict) or "text" not in body:
                        return self._send_json(400, {"error": "expected {\"text\": str}"})
                    text = str(body.get("text", "")).strip()
                    role = str(body.get("role", "user"))
                    if role not in WhisperLog.VALID_ROLES:
                        return self._send_json(400, {"error": f"role must be one of {WhisperLog.VALID_ROLES}"})
                    if not text:
                        return self._send_json(400, {"error": "text must not be empty"})
                    if len(text) > 2000:
                        return self._send_json(400, {"error": "text exceeds 2000 chars"})
                    entry = WhisperLog(cur).append(
                        role, text,
                        origin="web",
                        agent_id=body.get("agent_id"),
                    )
                    ack_status = None
                    applied = None
                    if role == "user":
                        StrategyStore(cur).merge({"operator_hint": text})
                        # v019-M2: 用 parse_whisper_to_intent + materialize_intent 替代旧 parse_whisper_to_command
                        # 修复 round:null、setdefault 覆盖、Coin ack=unparsed 等问题
                        try:
                            from aigenora.proto.whisper_bridge import parse_whisper_to_intent, materialize_intent
                            # 读协议 DECISION_SCHEMA（而非只读 CHOICE_KEYWORDS）
                            schema = None
                            protocol_whisper_parser = None
                            cur_match_key = "round"
                            cur_match_value = _current_round(cur)
                            snapshot_context = SnapshotBus(cur).read() or {}
                            try:
                                pd = resolve_protocol_dir(cur)
                                if pd:
                                    from aigenora.proto.loader import load_hooks
                                    h = load_hooks(pd)
                                    schema = getattr(h, "DECISION_SCHEMA", None)
                                    protocol_whisper_parser = getattr(h, "proto_parse_whisper_intent", None)
                                    if schema and schema.get("match_key"):
                                        cur_match_key = schema["match_key"]
                            except Exception:
                                pass
                            if cur_match_key == "tick":
                                world = snapshot_context.get("world") if isinstance(snapshot_context, dict) else None
                                tick_value = snapshot_context.get("tick") if isinstance(snapshot_context, dict) else None
                                if tick_value is None and isinstance(world, dict):
                                    tick_value = world.get("tick")
                                if tick_value is not None:
                                    cur_match_value = int(tick_value)
                            # 从 decision/state.json 读当前窗口
                            try:
                                state_fp = Path(cur) / "decision" / "state.json"
                                if state_fp.exists():
                                    st = json.loads(state_fp.read_text(encoding="utf-8"))
                                    if st.get("match_key"):
                                        cur_match_key = st["match_key"]
                                    if st.get("match_value") is not None:
                                        cur_match_value = st["match_value"]
                            except Exception:
                                pass
                            ck = schema.get("choices") if schema else None
                            intent = None
                            if callable(protocol_whisper_parser):
                                try:
                                    intent = protocol_whisper_parser(text, snapshot_context)
                                except Exception:
                                    intent = None
                            if intent is None:
                                intent = parse_whisper_to_intent(text, schema, choice_keywords=ck, match_key=cur_match_key, match_value=cur_match_value)
                            if intent is not None:
                                result = materialize_intent(
                                    intent, state_dir=str(cur), origin="whisper_bridge",
                                    agent_id=body.get("agent_id"), whisper_id=entry.get("id"),
                                    current_match_key=cur_match_key, current_match_value=cur_match_value,
                                    schema=schema,
                                )
                                ack_status = result.get("ack_status", "unparsed")
                                applied = result.get("applied")
                            else:
                                ack_status = "unparsed"
                        except Exception:
                            ack_status = "unparsed"
                        # 写一条 ack 记录，让前端知道 whisper 是否被解析成战术
                        if ack_status:
                            try:
                                WhisperLog(cur).append_ack(
                                    entry.get("id"), status=ack_status,
                                    action=applied.get("type") if applied else None,
                                    detail=json.dumps(applied, ensure_ascii=False) if applied else None,
                                )
                            except Exception:
                                pass
                    resp = dict(entry)
                    if applied:
                        resp["applied"] = applied
                    if ack_status:
                        resp["ack_status"] = ack_status
                    return self._send_json(200, resp)
                if path == "/api/whisper/ack":
                    if not isinstance(body, dict) or "id" not in body or "status" not in body:
                        return self._send_json(400, {"error": "expected {\"id\": str, \"status\": str}"})
                    wid = str(body["id"])
                    status = str(body["status"])
                    valid_statuses = ("executed", "dismissed", "unparsed", "error")
                    if status not in valid_statuses:
                        return self._send_json(400, {"error": f"status must be one of {valid_statuses}"})
                    wlog = WhisperLog(cur)
                    known_ids = {w.get("id") for w in wlog.read_all()}
                    if wid not in known_ids:
                        return self._send_json(404, {"error": "unknown whisper id"})
                    ack = wlog.append_ack(
                        wid,
                        status=status,
                        agent_id=body.get("agent_id"),
                        idempotency_key=body.get("idempotency_key"),
                        action=body.get("action"),
                        reason_code=body.get("reason_code"),
                        detail=body.get("detail"),
                        ts_client=body.get("ts_client"),
                    )
                    bc._broadcast("whisper_ack", ack)
                    return self._send_json(200, {"ok": True, "ack": ack})
                if path == "/api/chat/send":
                    # v006 P5: message entry for free-mode protocols (e.g. human-chat)
                    # Writes to <state_dir>/inbox.jsonl, consumed by the sender coroutine in _run_free
                    if not isinstance(body, dict) or "text" not in body:
                        return self._send_json(400, {"error": "expected {\"text\": str}"})
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._send_json(400, {"error": "text must not be empty"})
                    if len(text) > 2000:
                        return self._send_json(400, {"error": "text exceeds 2000 chars"})
                    inbox = effective() / "inbox.jsonl"
                    entry = {
                        "text": text,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "agent_id": body.get("agent_id"),
                    }
                    inbox.parent.mkdir(parents=True, exist_ok=True)
                    with open(inbox, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
                    return self._send_json(200, {"ok": True})
                if path == "/api/coach/send":
                    if coach is None:
                        return self._send_json(503, {"error": "coach not available"})
                    if not isinstance(body, dict) or "text" not in body:
                        return self._send_json(400, {"error": "expected {\"text\": str}"})
                    ctext = str(body.get("text", "")).strip()
                    if not ctext:
                        return self._send_json(400, {"error": "text must not be empty"})
                    if len(ctext) > 4000:
                        return self._send_json(400, {"error": "text exceeds 4000 chars"})
                    turn_id = coach.send(ctext)
                    return self._send_json(200, {"ok": True, "turn_id": turn_id})
                if path == "/api/coach/reset":
                    if coach is None:
                        return self._send_json(503, {"error": "coach not available"})
                    coach.reset()
                    return self._send_json(200, {"ok": True})
                return self._send_text(404, "not found")
            except _BadRequest as exc:
                return self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})

        # -------- Business UI (iframe)
        def _handle_ui_available(self):
            ui = _ui_dir(root_dir)
            if ui is None:
                runtime = session_runtime_info(root_dir)
                rejected_source = _rejected_remote_legacy_source(root_dir)
                payload = {
                    "available": False,
                    "ui_artifact": runtime.get("ui_artifact"),
                }
                if rejected_source:
                    payload.update({
                        "reason": "remote_ui_bridge_required",
                        "rejected_source_kind": rejected_source,
                    })
                return self._send_json(200, payload)
            # Simply return the protocol name (if available in the snapshot) for tab display
            snap = {}
            try:
                snap = SnapshotBus(effective()).read() or {}
            except Exception:
                pass
            # v006 P4: ui_origin comes from the _make_handler closure injection (None means legacy mode)
            ui_origin = getattr(handler_ctx, "ui_origin", None)
            legacy_mode = _detect_legacy_ui(ui)
            return self._send_json(200, {
                "available": True,
                "ui_dir": str(ui),
                "ui_origin": ui_origin,
                "legacy_mode": legacy_mode,
                "protocol_name": snap.get("protocol_name") or snap.get("protocol_id") or "",
                "ui_artifact": session_runtime_info(root_dir).get("ui_artifact"),
            })

        def _serve_ui_static(self, rel: str):
            ui = _ui_dir(root_dir)
            if ui is None:
                return self._send_text(404, "no ui")
            # Default entry
            if not rel or rel.endswith("/"):
                rel = (rel + "index.html") if rel else "index.html"
            # Path traversal defense: only allow inside the ui directory
            try:
                target = (ui / rel).resolve()
                ui_resolved = ui.resolve()
            except Exception:
                return self._send_text(400, "bad path")
            try:
                target.relative_to(ui_resolved)
            except ValueError:
                return self._send_text(403, "forbidden")
            if not target.exists() or not target.is_file():
                return self._send_text(404, "not found")
            # MIME inference (good enough; avoid mimetypes complexity)
            ext = target.suffix.lower()
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".htm": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".mjs": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".ico": "image/x-icon",
                ".woff": "font/woff",
                ".woff2": "font/woff2",
                ".txt": "text/plain; charset=utf-8",
                ".map": "application/json; charset=utf-8",
            }.get(ext, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # v006 P4: nosniff prevents MIME sniffing from bypassing the allowlist
            self.send_header("X-Content-Type-Options", "nosniff")
            # frame-ancestors 'self' prevents external pages from embedding the UI (only the broadcast parent may embed)
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _serve_assets(self, rel: str):
            """v005b: /assets/<file> — serve shared static from aigenora/agent/assets/."""
            if not rel or ".." in rel.split("/"):
                return self._send_text(404, "not found")
            ext = Path(rel).suffix.lower()
            allowed_ext = {".js", ".css", ".mjs"}
            if ext not in allowed_ext:
                return self._send_text(404, "not found")
            assets_dir = Path(__file__).parent / "assets"
            try:
                target = (assets_dir / rel).resolve()
                assets_resolved = assets_dir.resolve()
                target.relative_to(assets_resolved)
            except (ValueError, OSError):
                return self._send_text(403, "forbidden")
            if not target.exists() or not target.is_file():
                return self._send_text(404, "not found")
            ctype = {
                ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(ext, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # -------- SSE
        def _serve_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = bc.subscribe()
            heartbeat_at = time.monotonic() + 15.0
            try:
                while True:
                    if q:
                        event, data = q.pop(0)
                        try:
                            self.wfile.write(f"event: {event}\n".encode("utf-8"))
                            # data may contain \n, which would require a "data:" prefix per line; here the payload
                            # is the output of json.dumps with ensure_ascii=False, so it has no real newlines and
                            # a single line is fine.
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    else:
                        now = time.monotonic()
                        if now >= heartbeat_at:
                            try:
                                self.wfile.write(b": keep-alive\n\n")
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                            heartbeat_at = now + 15.0
                        time.sleep(0.1)
            finally:
                bc.unsubscribe(q)

        def _serve_coach_sse(self):
            # v014-M2: independent SSE for coach events (NOT merged into /sse/stream).
            if coach is None:
                return self._send_json(503, {"error": "coach not available"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = coach.streamer.subscribe()
            heartbeat_at = time.monotonic() + 15.0
            try:
                while True:
                    if q:
                        event, data = q.pop(0)
                        try:
                            self.wfile.write(f"event: {event}\n".encode("utf-8"))
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    else:
                        now = time.monotonic()
                        if now >= heartbeat_at:
                            try:
                                self.wfile.write(b": keep-alive\n\n")
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                break
                            heartbeat_at = now + 15.0
                        time.sleep(0.1)
            finally:
                coach.streamer.unsubscribe(q)

    return Handler


# ---------------------------------------------------------------------------
# v006 P4: UI isolated-origin server

class _UiStaticHandler(BaseHTTPRequestHandler):
    """Isolated UI static server, bound to a random port to form an isolated origin.

    Decoupled from the main broadcast server:
    - Serves only files under <protocol_dir>/ui/
    - frame-ancestors is strictly limited to the main broadcast server origin
    - Does not expose any /api/* or /sse/* endpoints
    """

    # Injected via class attribute (independent per server instance)
    ui_dir: Path = None  # type: ignore[assignment]
    main_origin: str = ""   # main broadcast server origin (for frame-ancestors)

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send(self, status: int, body: bytes, ctype: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # frame-ancestors restricted to the main broadcast server origin
        csp = f"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors {self.main_origin}; form-action 'none'; base-uri 'self'"
        self.send_header("Content-Security-Policy", csp)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, unquote
        path = urlparse(self.path).path
        if path == "/" or path == "":
            path = "/index.html"
        rel = unquote(path).lstrip("/")
        # Path traversal defense: only allow inside the ui directory
        try:
            target = (self.ui_dir / rel).resolve()
            ui_resolved = self.ui_dir.resolve()
        except Exception:
            return self._send(400, b"bad path", "text/plain; charset=utf-8")
        try:
            target.relative_to(ui_resolved)
        except ValueError:
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        if not target.exists() or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".htm": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".txt": "text/plain; charset=utf-8",
        }.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self._send(200, data, ctype)


def _spawn_ui_server(ui_dir: Path, main_origin: str) -> tuple[ThreadingHTTPServer, str]:
    """Start the isolated UI server. Returns (server, ui_origin)."""
    handler_cls = type("_BoundUiHandler", (_UiStaticHandler,), {
        "ui_dir": ui_dir.resolve(),
        "main_origin": main_origin,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    ui_port = httpd.server_address[1]
    ui_origin = f"http://127.0.0.1:{ui_port}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, ui_origin


# ---------------------------------------------------------------------------
# Service entry

def serve(root_dir: Path, port: int = 0, open_browser: bool = True) -> None:
    """Start the relay service. root_dir may be a daemon parent or a direct state child.

    v006 P4: if the protocol contains ui/ and is not in legacy mode, an isolated UI server (isolated
    origin) is started automatically. Legacy mode (old v005 UI with same-origin /api/ fetch) goes through
    the fallback and does not start an isolated server.
    """
    root_dir = Path(root_dir)
    bc = _Broadcaster(root_dir)
    bc.start()

    # v014-M2: embedded coach lives in THIS web subprocess (same process as the HTTP handler, so
    # /api/coach/stream can push streamer chunks in real time). workspace = root_dir/coach_workspace
    # (daemon parent dir, stable across parent->child game switches). personal_md is auto-resolved
    # from ~/.aigenora/skill_targets.json inside CoachWorker.
    coach = CoachWorker(state_dir=root_dir, data_dir=root_dir)
    coach.start()

    # v006 P4: first bind a temporary socket to obtain main_port (without holding the listener),
    # then (if needed) start the ui server, and finally build the main listener with main_port + ui_origin.
    # This way the "listening on" line appears immediately and spawn_broadcast's 5s timeout does not trigger.
    import socket as _socket
    if port == 0:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        actual_port = s.getsockname()[1]
        s.close()
    else:
        actual_port = port
    main_origin = f"http://127.0.0.1:{actual_port}"

    ui_dir = _ui_dir(root_dir)
    ui_httpd = None
    ui_origin = None
    if ui_dir is not None and not _detect_legacy_ui(ui_dir):
        try:
            ui_httpd, ui_origin = _spawn_ui_server(ui_dir, main_origin)
        except Exception as exc:
            print(f"[aigenora session web] WARN: failed to start ui origin server: {exc}", flush=True)
            ui_origin = None
            ui_httpd = None

    httpd = ThreadingHTTPServer(("127.0.0.1", actual_port),
                                 _make_handler(root_dir, bc, coach=coach, ui_origin=ui_origin))

    print(f"[aigenora session web] root_dir={root_dir}", flush=True)
    print(f"[aigenora session web] effective state_dir={bc.state_dir}", flush=True)
    print(f"[aigenora session web] listening on {main_origin}  (Ctrl+C to quit)", flush=True)
    if ui_origin:
        print(f"[aigenora session web] ui origin: {ui_origin}  (isolated origin)", flush=True)
    elif ui_dir is not None:
        print("[aigenora session web] ui: legacy mode (same-origin sandbox)", flush=True)
    if open_browser:
        try:
            webbrowser.open(main_origin)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[aigenora session web] stopped")
    finally:
        coach.stop()
        coach.wait_stopped(timeout=2.0)
        bc.stop()
        httpd.server_close()
        if ui_httpd is not None:
            ui_httpd.shutdown()
            ui_httpd.server_close()


# ---------------------------------------------------------------------------
# CLI entry (called by cli.py)

def cmd_web(args) -> int:
    root_dir = Path(args.state_dir)
    if not root_dir.exists():
        print(f"error: state-dir does not exist: {root_dir}")
        return 2
    port = int(getattr(args, "port", 0) or 0)
    open_browser = not getattr(args, "no_open", False)
    serve(root_dir, port=port, open_browser=open_browser)
    return 0


# ---------------------------------------------------------------------------
# Called by the host/join daemon: spawns the relay service in a subprocess and auto-opens the browser.
#
# Returns (pid, url) or None (failed / not started). Failures do not raise; they only print to stderr
# so they do not affect the main flow.

def spawn_broadcast(state_dir: Path | str, *, open_browser: bool = True) -> dict | None:
    """Start `aigenora session web --state-dir <state_dir> --port 0` as an independent subprocess.

    The subprocess stdout is redirected to state_dir/broadcast.log, and the port is parsed from the
    listening line in that file; this avoids a PIPE being filled and then blocking subsequent prints
    from the web subprocess.
    """
    import subprocess
    import sys

    cmd = [sys.executable, "-u", "-m", "aigenora", "session", "web",
           "--state-dir", str(state_dir), "--port", "0"]
    if not open_browser:
        cmd.append("--no-open")

    log_path = Path(state_dir) / "broadcast.log"
    try:
        log_fh = open(log_path, "wb")
    except Exception as exc:
        print(f"[warn] failed to open broadcast log {log_path}: {exc}", file=sys.stderr)
        return None
    try:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except Exception as exc:
        log_fh.close()
        print(f"[warn] failed to spawn broadcast process: {exc}", file=sys.stderr)
        return None
    finally:
        # The subprocess has inherited the fd; the parent can close its local handle
        log_fh.close()

    # Poll the log file until a "listening on" line appears or the timeout elapses
    url = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
            except Exception:
                pass
            print(f"[warn] broadcast process exited early: {tail}", file=sys.stderr)
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            if "listening on" in line:
                for tok in line.split():
                    if tok.startswith("http://"):
                        url = tok
                        break
            if url:
                break
        if url:
            break
        time.sleep(0.1)
    if not url:
        print("[warn] broadcast process did not report a URL in 5s", file=sys.stderr)
        return None
    # v005a: persist web_url to the parent session.json
    session_file = Path(state_dir) / "session.json"
    try:
        if session_file.exists():
            meta = json.loads(session_file.read_text(encoding="utf-8"))
            meta["web_url"] = url
            meta["web_pid"] = proc.pid
            meta["web_started_at"] = time.time()
            session_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[warn] failed to persist web_url to session.json: {exc}", file=sys.stderr)
    return {"pid": proc.pid, "url": url, "log": str(log_path)}
