from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from aigenora.agent.skeleton import (
    _skeleton,
    assert_hooks_implemented,
    write_sidecar,
)
from aigenora.engine.config import get_server, protocols_root, templates_root, data_protocols_root
from aigenora.engine.crypto import protocol_hash, protocol_hash_from_obj
from aigenora.engine.keys import load_keys
from aigenora.engine.p2p import memory_duplex, run_in_threads
from aigenora.engine.rest import RestClient
from aigenora.proto.engine import run_guest, run_host
from aigenora.proto.spec_version import check_spec_version


def _hash_dir(base: Path, proto_id: str) -> Path:
    return base / proto_id[:8] / proto_id[8:]


def _is_protocol_id(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _cache_root(data_dir: str | None = None) -> Path:
    from aigenora.engine.config import data_dir as _data_dir
    return Path(data_dir or _data_dir()) / "protocols"


def cache_path_for(proto_id: str, data_dir: str | None = None) -> Path:
    return _hash_dir(_cache_root(data_dir), proto_id)


def path_for(proto_id: str, data_dir: str | None = None) -> Path:
    # Runtime discovery reads only the user library (data_dir/protocols). Built-in
    # samples are seeded there by `init`; fetch/create also land here. The explicit
    # `--protocol-dir` override is handled by callers, not here.
    lib = data_protocols_root(data_dir)
    if _is_protocol_id(proto_id):
        direct = _hash_dir(lib, proto_id)
        if (direct / "spec.json").exists():
            return direct
    index = lib / "index.json"
    if index.exists():
        data = json.loads(index.read_text(encoding="utf-8"))
        protocols = data.get("protocols", data if isinstance(data, list) else [])
        for item in protocols:
            aliases = [item.get("id"), item.get("alias"), item.get("protocol_id")]
            if proto_id in aliases:
                # Prefer the index's recorded `path` to locate the dir directly.
                # This survives a stale protocol_id (dir name != spec hash) without
                # recursing into a lookup that may fail to resolve.
                rel = item.get("path")
                if rel:
                    cand = lib / rel
                    if (cand / "spec.json").exists():
                        return cand
                pid = item.get("protocol_id") or item.get("hash")
                if pid and pid != proto_id:
                    return path_for(pid, data_dir)
    raise FileNotFoundError(f"protocol not found: {proto_id}")


def _message_summary(spec: dict[str, Any], direction: str) -> str:
    # Legacy implementation kept for compatibility (external tests still reference it);
    # the new skeleton lives in aigenora.agent.skeleton.
    messages = spec.get("messages", [])
    lines: list[str] = []
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_dir = msg.get("direction")
            if msg_dir not in {direction, "both"}:
                continue
            fields = msg.get("fields", {})
            action = ""
            if isinstance(fields, dict):
                action_schema = fields.get("action", {})
                values = action_schema.get("values", []) if isinstance(action_schema, dict) else []
                if values:
                    action = f" action={values[0]!r}"
            lines.append(f"# - {msg.get('name', '<unnamed>')} ({msg_dir}){action}")
    return "\n".join(lines) if lines else "# - No messages declared for this direction."


def _extract_spec(data: Any) -> dict[str, Any]:
    spec = data.get("spec_json", data) if isinstance(data, dict) else data
    if isinstance(spec, str):
        spec = json.loads(spec)
    if not isinstance(spec, dict):
        raise RuntimeError("protocol response did not contain a JSON spec object")
    return spec


def fetch_protocol(client: RestClient, protocol_id: str, data_dir: str | None = None,
                   *, accept_ui: bool = False) -> tuple[Path, bool]:
    """Fetch a protocol: from v006 P4 onwards prefers GET /bundle; older servers fall back to GET /protocols/{id} on 404.

    Returns (proto_dir, created_hooks).

    accept_ui: whether to accept the UI bundle distributed by the protocol author. Remote UI is
    third-party web code with trojan/malware risk, so the secure default is False (only spec.json
    is written, no UI). Pass True when the user explicitly trusts the author (CLI --accept-ui, or
    PERSONAL.md accept_remote_ui=always). UI files (if any) are written to <out>/ui/ with a
    .aigenora-ui.json sidecar. UI verification failure does not block spec writing, but raises RuntimeError.
    """
    if not _is_protocol_id(protocol_id):
        raise ValueError("protocol_id must be a 64-character lowercase SHA256 hex string")

    from aigenora.agent.protocol_ui import (
        compute_manifest_hash,
        fetch_ui_bundle,
        materialize_bundle,
        write_ui_sidecar,
    )

    # Prefer the bundle endpoint
    bundle_resp = client.request("GET", f"/api/v1/protocols/{protocol_id}/bundle", None)
    if bundle_resp.status_code == 404:
        # Older server or protocol does not exist; fall back to the spec endpoint
        # (may itself 404, handled by the caller)
        data = client.json("GET", f"/api/v1/protocols/{protocol_id}", expected={200})
        spec = _extract_spec(data)
        ui_manifest = None
        ui_files: list[dict[str, Any]] = []
        ui_manifest_hash = None
        source_server = client.server
    elif bundle_resp.status_code != 200:
        raise RuntimeError(f"GET /bundle failed: HTTP {bundle_resp.status_code}: {bundle_resp.text[:200]}")
    else:
        data = bundle_resp.json()
        spec = _extract_spec(data)
        ui_manifest = data.get("ui_manifest")
        ui_files = data.get("ui_files") or []
        ui_manifest_hash = data.get("ui_manifest_hash")
        source_server = client.server

    check_spec_version(spec, reject_unknown=True)
    actual = protocol_hash_from_obj(spec)
    if actual != protocol_id:
        raise RuntimeError(f"protocol hash mismatch: requested {protocol_id}, got {actual}")

    out = cache_path_for(protocol_id, data_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # UI writing: remote UI is third-party web code provided by the protocol author (trojan/malware risk);
    # the secure default is to refuse. Only written when accept_ui=True (--accept-ui, or
    # PERSONAL.md accept_remote_ui=always).
    if ui_manifest_hash and accept_ui:
        manifest_files = [
            {
                "path": f.get("path"),
                "content_hash": f.get("content_hash"),
                "size_bytes": f.get("size_bytes"),
            }
            for f in ui_files
        ]
        actual_manifest_hash = compute_manifest_hash(manifest_files)
        if actual_manifest_hash != ui_manifest_hash:
            raise RuntimeError(
                f"ui manifest hash mismatch: declared {ui_manifest_hash}, got {actual_manifest_hash}"
            )
        from aigenora.agent.protocol_ui import read_ui_sidecar
        existing = read_ui_sidecar(out)
        if not (existing and existing.get("ui_manifest_hash") == ui_manifest_hash):
            materialize_bundle(out, ui_files)
            write_ui_sidecar(
                out, protocol_id=protocol_id, manifest_hash=ui_manifest_hash,
                files=ui_files, source_server=source_server,
            )
    elif ui_manifest_hash:
        # Remote UI present but not accepted: spec.json was written, UI is not downloaded; notify the user
        import sys
        print(
            f"[fetch] Protocol {protocol_id[:16]}... author distributed a UI bundle (third-party web code, "
            f"security risk), not accepted. spec.json written, UI not downloaded. "
            f"If you trust the protocol author: add --accept-ui, or set accept_remote_ui: always in PERSONAL.md.",
            file=sys.stderr,
        )

    hooks = out / "hooks.py"
    created_hooks = False
    if not hooks.exists():
        # P3: do not overwrite an existing hooks.py; only write a skeleton + sidecar on first generation
        hooks_text = _skeleton(spec)
        hooks.write_text(hooks_text, encoding="utf-8")
        write_sidecar(out, protocol_id=protocol_id, spec=spec, hooks_text=hooks_text)
        created_hooks = True
    return out, created_hooks


def prepare_protocol(client: RestClient, protocol_id: str, data_dir: str | None = None,
                     *, accept_ui: bool = False) -> tuple[Path, bool]:
    try:
        proto_dir = path_for(protocol_id, data_dir)
    except FileNotFoundError:
        return fetch_protocol(client, protocol_id, data_dir, accept_ui=accept_ui)
    if not (proto_dir / "hooks.py").exists():
        raise RuntimeError(f"hooks.py not found in protocol dir: {proto_dir}")
    return proto_dir, False


def run(args) -> int:
    if args.protocol_cmd == "hash":
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        check_spec_version(spec, reject_unknown=False)
        print(protocol_hash(args.spec))
        return 0
    if args.protocol_cmd == "path":
        print(path_for(args.alias_or_protocol_id, args.data_dir))
        return 0
    if args.protocol_cmd == "register":
        with Path(args.spec).open("r", encoding="utf-8") as f:
            spec = json.load(f)
        check_spec_version(spec, reject_unknown=True)
        kp = load_keys(args.data_dir)
        # default preflight unless skipped
        if not getattr(args, "skip_preflight", False):
            from aigenora.agent.protocol_preflight import preflight as do_preflight
            result = do_preflight(spec, family=spec.get("family"), allow_new=True, reason=getattr(args, "reason", ""), data_dir=args.data_dir)
            if result["status"] == "blocked":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        else:
            if not getattr(args, "reason", ""):
                print("[warning] skipping preflight without a reason")
        proto_id = protocol_hash(args.spec)
        payload = {
            "protocol_id": proto_id,
            "name": spec.get("name") or proto_id,
            "description": spec.get("description") or "",
            "type": spec.get("type") or "protocol",
            "spec_json": spec,
        }
        client = RestClient(get_server(args.server), kp)
        data = client.json("POST", "/api/v1/protocols", payload, expected={200, 201, 409})
        print(data if data is not None else proto_id)

        # v006 P4: --with-ui <dir> uploads the UI bundle + finalize
        ui_dir_value = getattr(args, "with_ui", None)
        if ui_dir_value:
            from aigenora.agent.protocol_ui import build_manifest_from_dir
            ui_dir = Path(ui_dir_value)
            try:
                manifest_files, manifest_hash = build_manifest_from_dir(ui_dir)
            except Exception as exc:
                print(f"[ui] build manifest failed: {exc}")
                return 1
            body = {
                "manifest_hash": manifest_hash,
                "manifest": {"files": [
                    {"path": f["path"], "content_hash": f["content_hash"], "size_bytes": f["size_bytes"]}
                    for f in manifest_files
                ]},
                "files": manifest_files,
            }
            try:
                resp = client.json(
                    "POST", f"/api/v1/protocols/{proto_id}/ui-batch", body, expected={200},
                )
                print(f"[ui] staged {resp.get('staging_files')} files (manifest_hash={manifest_hash[:16]}...)")
            except Exception as exc:
                print(f"[ui] ui-batch failed: {exc}")
                return 1
            try:
                resp = client.json(
                    "POST", f"/api/v1/protocols/{proto_id}/ui-finalize",
                    {"manifest_hash": manifest_hash}, expected={200},
                )
                print(f"[ui] finalized status={resp.get('status')} idempotent={resp.get('idempotent')}")
            except Exception as exc:
                print(f"[ui] ui-finalize failed: {exc}")
                return 1
        return 0
    if args.protocol_cmd == "fetch":
        kp = load_keys(args.data_dir)
        out, created_hooks = fetch_protocol(RestClient(get_server(args.server), kp), args.protocol_id, args.data_dir, accept_ui=getattr(args, "accept_ui", False))
        print(args.protocol_id)
        if created_hooks:
            print(f"[fetch] generated local hooks skeleton: {out / 'hooks.py'}")
        return 0
    if args.protocol_cmd == "test":
        host_ch, guest_ch = memory_duplex()
        protocol_dir = Path(args.protocol_dir)
        spec = json.loads((protocol_dir / "spec.json").read_text(encoding="utf-8"))
        check_spec_version(spec, reject_unknown=True)
        assert_hooks_implemented(
            protocol_dir,
            allow_skeleton=getattr(args, "allow_skeleton_hooks", False),
        )
        from aigenora.proto.engine import parse_options
        opts = parse_options(getattr(args, "options", None))
        run_in_threads(
            lambda: run_host(protocol_dir, host_ch, options=opts, state_base=args.state_base),
            lambda: run_guest(protocol_dir, guest_ch, options=opts, state_base=args.state_base),
        )
        print("[OK] protocol test passed")
        if getattr(args, "adversarial", False):
            from aigenora.agent.protocol_adversarial import run_adversarial_suite
            summary = run_adversarial_suite(protocol_dir, opts)
            if summary["status"] == "skipped":
                print(f"[adversarial] skipped: {summary['reason']}")
            else:
                print(f"[adversarial] {summary['passed']}/{summary['total']} cases rejected malformed messages")
                for d in summary["details"]:
                    mark = "PASS" if d["rejected"] else "FAIL"
                    print(f"  [{mark}] {d['case']}")
                if summary["failed"]:
                    print(f"[adversarial] {summary['failed']} case(s) failed to reject malformed input")
                    return 1
        return 0
    if args.protocol_cmd == "preflight":
        return _cmd_preflight(args)
    if args.protocol_cmd == "governance":
        return _cmd_governance(args)
    if args.protocol_cmd == "stats":
        return _cmd_protocol_stats(args)
    if args.protocol_cmd == "create":
        template = templates_root(args.data_dir) / f"{args.template}.json"
        if not template.exists():
            raise FileNotFoundError(template)
        output_path = Path(args.output)
        # Create the parent dir so `--output ./draft/spec.json` works without a pre-existing
        # ./draft/ (shutil.copyfile does not create intermediate dirs).
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, output_path)
        print(f"[OK] created {args.output}")
        return 0
    if args.protocol_cmd == "discover":
        return _cmd_discover(args)
    if args.protocol_cmd == "search":
        return _cmd_search(args)
    if args.protocol_cmd == "select":
        return _cmd_select(args)
    if args.protocol_cmd == "preferences":
        return _cmd_preferences(args)
    if args.protocol_cmd == "profile":
        return _cmd_profile(args)
    raise RuntimeError(f"unknown protocol command: {args.protocol_cmd}")


def _cmd_preflight(args) -> int:
    from aigenora.agent.protocol_preflight import preflight

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    check_spec_version(spec, reject_unknown=True)
    result = preflight(
        spec,
        family=getattr(args, "family", None),
        include_remote=getattr(args, "include_remote", False),
        allow_new=getattr(args, "allow_new", False),
        reason=getattr(args, "reason", ""),
        data_dir=getattr(args, "data_dir", None),
    )
    if getattr(args, "json_output", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status: {result['status']}")
        print(f"classification: {result.get('classification', '?')}")
        print(f"recommendation: {result.get('recommendation', '?')}")
        if result.get("reason"):
            print(f"reason: {result['reason']}")
        if result.get("nearest"):
            for n in result["nearest"]:
                print(f"  nearest: {n['alias']} ({n['protocol_id'][:16]}...) {n['classification']}")
    return 0 if result["status"] == "allowed" else 1


def _cmd_governance(args) -> int:
    from aigenora.agent.session import governance_get, governance_set

    if args.governance_cmd == "get":
        return governance_get(args)
    elif args.governance_cmd == "set":
        return governance_set(args)
    raise RuntimeError(f"unknown governance command: {args.governance_cmd}")


def _cmd_protocol_stats(args) -> int:
    from aigenora.agent.session import protocol_stats

    return protocol_stats(args)


def _cmd_search(args) -> int:
    from aigenora.agent.protocol_search import search_protocols

    results = search_protocols(
        family=args.family,
        tags=args.tags,
        capabilities=args.capabilities,
        status=args.status,
        all_status=args.all_status,
        data_dir=args.data_dir,
    )
    if getattr(args, "json_output", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        if not results:
            print("no protocols found")
        for p in results:
            blocked = " [blocked]" if p.get("_blocked") else ""
            print(f"  {p.get('alias', '?')} ({p.get('protocol_id', '')[:16]}...) family={p.get('family', '?')} status={p.get('status', '?')}{blocked}")
    return 0


def _fetch_remote_page(client: RestClient, *, limit: int,
                       cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch one page of the remote protocol directory.

    The server lists protocols with created_at DESC LIMIT keyset pagination (index-ordered,
    no LIKE, no full table scan). Returns (results, next_cursor).
    """
    path = f"/api/v1/protocols?limit={limit}"
    if cursor:
        path += f"&cursor={cursor}"
    data = client.json("GET", path, None, expected={200})
    return data.get("results") or [], data.get("next_cursor")


def _cmd_discover(args) -> int:
    """Browse the remote protocol directory. Paginated: never pulls everything at once.

    - Browse (no -q): default 1 page (limit items); --cursor paginates forward.
    - Keyword (-q): pulls up to --max-pages pages (default 5) and filters client-side on
      name+description. The DB is never asked to do a LIKE; matching is in-memory.
    - --fetch: auto-downloads only when -q yields exactly one match.
    """
    kp = load_keys(args.data_dir)
    client = RestClient(get_server(args.server), kp)

    limit = min(getattr(args, "limit", None) or 20, 100)
    query = getattr(args, "query", None)
    cursor = getattr(args, "cursor", None)
    # Browse defaults to 1 page; keyword defaults to scanning up to 5 pages (==100 rows).
    max_pages = getattr(args, "max_pages", None) or (5 if query else 1)

    collected: list[dict[str, Any]] = []
    next_cursor: str | None = cursor
    pages = 0
    while pages < max_pages:
        page, next_cursor = _fetch_remote_page(client, limit=limit, cursor=next_cursor)
        collected.extend(page)
        pages += 1
        if not next_cursor:
            break
    exhausted = not next_cursor  # next_cursor None => reached the end

    # Client-side keyword filter (no DB query)
    if query:
        ql = query.lower()
        results = [p for p in collected
                   if ql in (p.get("name") or "").lower()
                   or ql in (p.get("description") or "").lower()]
    else:
        results = collected

    if getattr(args, "json_output", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print(f"no protocols {'matching ' + repr(query) if query else 'on this page'}")
    else:
        for p in results:
            print(f"  {(p.get('name') or '?')[:20]:<20} {p.get('protocol_id', '')[:16]}...  {(p.get('description') or '')[:50]}")
        print(f"({len(results)} protocol{'s' if len(results) != 1 else ''}" + (f" matching '{query}'" if query else "") + ")")

    # Range hint so the user knows whether they have seen everything
    if query and not exhausted:
        print(f"(searched {pages} page{'s' if pages != 1 else ''} = {pages * limit} most recent; older not searched. use --max-pages N to extend)")
    elif not query and next_cursor:
        print(f"(more available: aigenora protocol discover --cursor {next_cursor} ...)")

    # --fetch: only auto-download on a unique keyword match
    if getattr(args, "fetch", False):
        if not query:
            print("[--fetch] needs -q to pin a unique protocol; skipping")
        elif len(results) == 1:
            pid = results[0].get("protocol_id")
            print(f"[1 match] fetching {pid[:16]}...")
            out, created = fetch_protocol(client, pid, args.data_dir,
                                          accept_ui=getattr(args, "accept_ui", False))
            print(f"[fetch] {'hooks skeleton generated' if created else 'already present'}: {out / 'hooks.py'}")
        else:
            print(f"[{len(results)} matches] not auto-fetching; narrow -q or run: aigenora protocol fetch <id>")
    return 0


def _cmd_select(args) -> int:
    from aigenora.agent.protocol_search import select_protocol

    options = json.loads(args.options) if args.options else None
    result = select_protocol(
        protocol_id=args.protocol_id,
        alias=args.alias,
        family=args.family,
        profile=args.profile,
        options=options,
        non_interactive=args.non_interactive,
        save_preference=args.save_preference,
        data_dir=args.data_dir,
    )
    if getattr(args, "json_output", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("status") == "ambiguous":
            print("ambiguous: multiple candidates match family")
            for c in result.get("candidates", []):
                print(f"  {c.get('alias', '?')} ({c.get('protocol_id', '')[:16]}...) status={c.get('status', '?')}")
            print("next actions:")
            for a in result.get("next_actions", []):
                print(f"  {a}")
        else:
            print(f"protocol_id: {result['protocol_id']}")
            print(f"path: {result['path']}")
            if result.get("options"):
                print(f"options: {json.dumps(result['options'])}")
            print(f"source: {result['source']}")
    return 0


def _cmd_preferences(args) -> int:
    from aigenora.proto.prefs import (
        list_preferences, get_preference, set_preference,
        clear_preference, block_protocol, unblock_protocol,
    )

    if args.prefs_cmd == "list":
        prefs = list_preferences(args.data_dir)
        if getattr(args, "json_output", False):
            print(json.dumps(prefs, ensure_ascii=False, indent=2))
        else:
            families = prefs.get("families", {})
            if not families:
                print("no preferences set")
            for fam, entry in families.items():
                print(f"  {fam}: {entry.get('preferred_protocol_id', '')[:16]}... profile={entry.get('preferred_profile', '-')}")
            blocked = prefs.get("blocked_protocols", [])
            if blocked:
                print("blocked:")
                for b in blocked:
                    print(f"  {b['protocol_id'][:16]}... reason={b.get('reason', '')}")
    elif args.prefs_cmd == "get":
        entry = get_preference(args.data_dir, args.family)
        if getattr(args, "json_output", False):
            print(json.dumps(entry, ensure_ascii=False, indent=2))
        else:
            if entry:
                print(f"preferred: {entry.get('preferred_protocol_id', '')[:16]}... profile={entry.get('preferred_profile', '-')}")
            else:
                print(f"no preference for family: {args.family}")
    elif args.prefs_cmd == "set":
        entry = set_preference(args.data_dir, args.family, args.protocol_id, args.profile, args.reason)
        print(f"[OK] preference set for {args.family}: {args.protocol_id[:16]}...")
    elif args.prefs_cmd == "clear":
        if clear_preference(args.data_dir, args.family):
            print(f"[OK] preference cleared for {args.family}")
        else:
            print(f"no preference to clear for {args.family}")
    elif args.prefs_cmd == "block":
        block_protocol(args.data_dir, args.protocol_id, args.reason)
        print(f"[OK] blocked {args.protocol_id[:16]}...")
    elif args.prefs_cmd == "unblock":
        if unblock_protocol(args.data_dir, args.protocol_id):
            print(f"[OK] unblocked {args.protocol_id[:16]}...")
        else:
            print(f"not blocked: {args.protocol_id[:16]}...")
    return 0


def _cmd_profile(args) -> int:
    from aigenora.proto.prefs import list_profiles, set_profile, delete_profile

    if args.profile_cmd == "list":
        profiles = list_profiles(args.data_dir, args.family)
        if getattr(args, "json_output", False):
            print(json.dumps(profiles, ensure_ascii=False, indent=2))
        else:
            families = profiles.get("families", {})
            if not families:
                print("no profiles defined")
            for fam, profs in families.items():
                for name, entry in profs.items():
                    print(f"  {fam}/{name}: {entry.get('protocol_id', '')[:16]}... options={json.dumps(entry.get('options', {}))}")
    elif args.profile_cmd == "set":
        options = json.loads(args.options)
        entry = set_profile(args.data_dir, args.family, args.name, args.protocol_id, options, args.description)
        print(f"[OK] profile {args.family}/{args.name} saved")
    elif args.profile_cmd == "delete":
        if delete_profile(args.data_dir, args.family, args.name):
            print(f"[OK] profile {args.family}/{args.name} deleted")
        else:
            print(f"profile not found: {args.family}/{args.name}")
    return 0
