from __future__ import annotations

from pathlib import Path
from typing import Any

from aigenora.proto.hooks import ProtocolHooks


_MAX_MESSAGES_RETAINED = 200


class Hooks(ProtocolHooks):
    """Human Chat business hooks.

    Security semantics: the Agent does not interpret message content, it only forwards and displays it.
    Observation channel: each message is written to both SnapshotBus.messages (webui render source) and
    DetailLog.append (persistent audit). The CLI side continues to echo [Peer] text to remain compatible
    with the stdin user experience.
    """

    def proto_init(self, options, role, args, state_dir: Path,
                    decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self._last_recv_seq: int = 0
        peer_role = "guest" if role == "host" else "host"
        self.snapshot.update(
            phase="chatting",
            messages=[],
            peer_role=peer_role,
        )

    def proto_host_metadata(self):
        return ("Human Chat", "chat", "chat", {})

    def _append_snapshot_message(self, *, source: str, seq: int, text: str) -> None:
        snap = self.snapshot.read() or {}
        msgs = list(snap.get("messages") or [])
        msgs.append({"seq": seq, "from": source, "text": text})
        if len(msgs) > _MAX_MESSAGES_RETAINED:
            msgs = msgs[-_MAX_MESSAGES_RETAINED:]
        summary = f"{source}: {text[:60]}"
        self.snapshot.update(
            messages=msgs,
            last_event={
                "summary": summary,
                "structured": {"from": source, "seq": seq, "text": text},
            },
        )

    def proto_on_message(self, msg):
        """Receive a peer message: CLI echo + write snapshot/detail."""
        if msg.get("action") != "chat":
            return
        seq = msg.get("seq")
        if not isinstance(seq, int) or seq <= self._last_recv_seq:
            return
        self._last_recv_seq = seq
        text = msg.get("text", "")
        # preserve the original CLI user experience
        print(f"\r[Peer] {text}\n> ", end="", flush=True)
        self._append_snapshot_message(source="peer", seq=seq, text=text)
        self.details.append(direction="recv", seq=seq, text=text)

    def proto_on_send(self, msg):
        """Send a local message: write snapshot/detail (webui render depends on it).

        The engine layer calls this back after the sender coroutine sends; it does not break stdin behavior.
        """
        if msg.get("action") != "chat":
            return
        seq = msg.get("seq")
        text = msg.get("text", "")
        if not isinstance(seq, int):
            return
        self._append_snapshot_message(source="self", seq=seq, text=text)
        self.details.append(direction="sent", seq=seq, text=text)

    def proto_on_end(self):
        print("\r[Call ended]")
        self.snapshot.update(
            phase="ended",
            last_event={"summary": "Chat ended", "structured": {}},
        )
