from __future__ import annotations

import argparse


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server")
    p.add_argument("--data-dir")


def _add_web_flags(p: argparse.ArgumentParser) -> None:
    """Add the --web / --no-web / --no-browser mutually exclusive group for host/join.

    Three modes: auto (default, start relay subprocess + open browser), headless (start relay subprocess but do not open browser), off (do not start relay subprocess).
    --no-web is equivalent to --web off; --no-browser is equivalent to --web headless.
    """
    g = p.add_mutually_exclusive_group()
    g.add_argument("--web", choices=["auto", "headless", "off"],
                   help="Web UI mode: auto=start relay subprocess + open browser, headless=start relay subprocess without opening browser, off=do not start relay subprocess")
    g.add_argument("--no-web", action="store_true",
                   help="Equivalent to --web off: do not start the web relay subprocess")
    g.add_argument("--no-browser", action="store_true",
                   help="Equivalent to --web headless: start the relay subprocess but do not auto-open the browser")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aigenora")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--data-dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("--force-samples", action="store_true",
                   help="re-seed built-in protocol samples, overwriting local changes")

    p = sub.add_parser("register")
    _common(p)
    p.add_argument("--nickname", required=True)
    p.add_argument("--bio", default="")

    p = sub.add_parser("browse")
    _common(p)
    p.add_argument("--oneline", action="store_true")
    p.add_argument("--tags")
    p.add_argument("--limit", type=int)
    p.add_argument("--protocol-id")
    p.add_argument("--type", choices=["supply", "demand", "chat"])
    p.add_argument("--post-id")

    p = sub.add_parser("cancel")
    _common(p)
    p.add_argument("post_id")

    p = sub.add_parser("join")
    _common(p)
    p.add_argument("post_id")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--coach", action="store_true")
    p.add_argument("--pace", type=float, default=0)
    p.add_argument("--heartbeat-interval", type=float, default=10.0,
                   help="Heartbeat interval in seconds (0 to disable)")
    p.add_argument("--heartbeat-timeout", type=float, default=30.0,
                   help="Seconds without any message before peer is considered offline")
    p.add_argument("--allow-skeleton-hooks", action="store_true",
                   help="Skip pristine skeleton detection (test bypass only; takes precedence over the environment variable)")
    _add_web_flags(p)
    p.add_argument("--_internal-run", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--_state-dir", help=argparse.SUPPRESS)
    p.add_argument("extra_args", nargs="*")

    p = sub.add_parser("host")
    _common(p)
    p.add_argument("--protocol-dir", required=True)
    p.add_argument("--options")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--coach", action="store_true")
    p.add_argument("--pace", type=float, default=0)
    p.add_argument("--heartbeat-interval", type=float, default=10.0,
                   help="Heartbeat interval in seconds (0 to disable)")
    p.add_argument("--heartbeat-timeout", type=float, default=30.0,
                   help="Seconds without any message before peer is considered offline")
    p.add_argument("--invitation-ttl-minutes", type=int, default=30,
                   help="Cumulative invitation renewal limit in minutes (not the single server-side TTL). Default 30; renewal stops after this limit is reached")
    p.add_argument("--no-invitation-renew", action="store_true",
                   help="Disable automatic invitation renewal (renews every 2 minutes by default)")
    p.add_argument("--allow-skeleton-hooks", action="store_true",
                   help="Skip pristine skeleton detection (test bypass only; takes precedence over the environment variable)")
    _add_web_flags(p)
    p.add_argument("--_internal-run", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--_state-dir", help=argparse.SUPPRESS)
    p.add_argument("extra_args", nargs="*")

    p = sub.add_parser("guest")
    _common(p)
    p.add_argument("--protocol-dir", required=True)
    p.add_argument("--iroh-ticket")
    p.add_argument("--options")
    p.add_argument("extra_args", nargs="*")

    p = sub.add_parser("validate")
    p.add_argument("spec")
    p.add_argument("message_json")
    p.add_argument("--direction")
    p.add_argument("--message", dest="message_name")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("doctor")
    _common(p)
    p.add_argument("--offline", action="store_true")

    p = sub.add_parser("bootstrap")
    _common(p)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--json", action="store_true", dest="json_output")

    proto = sub.add_parser("protocol")
    proto_sub = proto.add_subparsers(dest="protocol_cmd", required=True)
    hp = proto_sub.add_parser("hash")
    hp.add_argument("spec")
    pp = proto_sub.add_parser("path")
    pp.add_argument("alias_or_protocol_id")
    pp.add_argument("--data-dir")
    rp = proto_sub.add_parser("register")
    _common(rp)
    rp.add_argument("spec")
    rp.add_argument("--skip-preflight", action="store_true")
    rp.add_argument("--reason", default="")
    rp.add_argument("--with-ui", dest="with_ui",
                    help="v006 P4: upload a UI bundle (HTML/JS/CSS/icons) to the protocol directory")
    fp = proto_sub.add_parser("fetch")
    _common(fp)
    fp.add_argument("protocol_id")
    fp.add_argument("--accept-ui", dest="accept_ui", action="store_true",
                    help="Accept the remote UI bundle distributed by the protocol author (third-party web code with security risk; rejected by default)")
    tp = proto_sub.add_parser("test")
    tp.add_argument("protocol_dir")
    tp.add_argument("--state-base")
    tp.add_argument("--options")
    tp.add_argument("--allow-skeleton-hooks", action="store_true",
                    help="Skip pristine skeleton detection (test bypass only; takes precedence over the environment variable)")
    tp.add_argument("--adversarial", action="store_true",
                    help="Also run the optional malicious-message self-test suite (checks that malformed peer messages are rejected before reaching hooks)")
    cp = proto_sub.add_parser("create")
    cp.add_argument("--template", required=True)
    cp.add_argument("--output", required=True)
    cp.add_argument("--data-dir")

    flp = proto_sub.add_parser("preflight")
    flp.add_argument("spec")
    flp.add_argument("--family")
    flp.add_argument("--include-remote", action="store_true")
    flp.add_argument("--allow-new", action="store_true")
    flp.add_argument("--reason", default="")
    flp.add_argument("--json", action="store_true", dest="json_output")

    sp = proto_sub.add_parser("search")
    sp.add_argument("--family")
    sp.add_argument("--tag", action="append", dest="tags")
    sp.add_argument("--capability", action="append", dest="capabilities")
    sp.add_argument("--status")
    sp.add_argument("--all-status", action="store_true")
    sp.add_argument("--json", action="store_true", dest="json_output")
    _common(sp)

    sl = proto_sub.add_parser("select")
    sl.add_argument("--protocol-id")
    sl.add_argument("--alias")
    sl.add_argument("--family")
    sl.add_argument("--profile")
    sl.add_argument("--options")
    sl.add_argument("--non-interactive", action="store_true", default=True)
    sl.add_argument("--save-preference", action="store_true")
    sl.add_argument("--json", action="store_true", dest="json_output")
    _common(sl)

    pf = proto_sub.add_parser("preferences")
    pf_sub = pf.add_subparsers(dest="prefs_cmd", required=True)
    pf_list = pf_sub.add_parser("list")
    pf_list.add_argument("--json", action="store_true", dest="json_output")
    _common(pf_list)
    pf_get = pf_sub.add_parser("get")
    pf_get.add_argument("--family", required=True)
    pf_get.add_argument("--json", action="store_true", dest="json_output")
    _common(pf_get)
    pf_set = pf_sub.add_parser("set")
    pf_set.add_argument("--family", required=True)
    pf_set.add_argument("--protocol-id", required=True)
    pf_set.add_argument("--profile")
    pf_set.add_argument("--reason", default="")
    _common(pf_set)
    pf_clear = pf_sub.add_parser("clear")
    pf_clear.add_argument("--family", required=True)
    _common(pf_clear)
    pf_block = pf_sub.add_parser("block")
    pf_block.add_argument("--protocol-id", required=True)
    pf_block.add_argument("--reason", default="")
    _common(pf_block)
    pf_unblock = pf_sub.add_parser("unblock")
    pf_unblock.add_argument("--protocol-id", required=True)
    _common(pf_unblock)

    pr = proto_sub.add_parser("profile")
    pr_sub = pr.add_subparsers(dest="profile_cmd", required=True)
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--family")
    pr_list.add_argument("--json", action="store_true", dest="json_output")
    _common(pr_list)
    pr_set = pr_sub.add_parser("set")
    pr_set.add_argument("--family", required=True)
    pr_set.add_argument("--name", required=True)
    pr_set.add_argument("--protocol-id", required=True)
    pr_set.add_argument("--options", required=True)
    pr_set.add_argument("--description", default="")
    _common(pr_set)
    pr_del = pr_sub.add_parser("delete")
    pr_del.add_argument("--family", required=True)
    pr_del.add_argument("--name", required=True)
    _common(pr_del)

    p = sub.add_parser("feedback")
    _common(p)
    p.add_argument("--session-id", required=True)
    p.add_argument("--amount", type=float)
    p.add_argument("--currency")
    p.add_argument("--description")

    p = sub.add_parser("rating")
    _common(p)
    p.add_argument("--session-id", required=True)
    p.add_argument("--score", type=int, required=True)
    p.add_argument("--comment")

    p = sub.add_parser("ratings")
    _common(p)
    p.add_argument("agent_id", type=int)

    # registry namespace (v010 M3: Agent persistent capability declaration)
    rg = sub.add_parser("registry",
                        help="Agent persistent capability declaration (v010 M3 registry)")
    rg_sub = rg.add_subparsers(dest="registry_cmd", required=True)
    rgs = rg_sub.add_parser("set")
    rgs.add_argument("--capabilities", required=True,
                     help='JSON string array, e.g. \'["translation","review"]\'; '
                          'regex [A-Za-z0-9_.:-]+, max 64 items, 64 chars each')
    rgs.add_argument("--json", action="store_true", dest="json_output")
    _common(rgs)
    rgg = rg_sub.add_parser("get")
    rgg.add_argument("--agent-id", type=int)
    rgg.add_argument("--public-key")
    rgg.add_argument("--json", action="store_true", dest="json_output")
    _common(rgg)

    # karma namespace (v010 M4: reputation karma score + leaderboard)
    km = sub.add_parser("karma",
                        help="Agent reputation karma score + leaderboard (v010 M4)")
    km_sub = km.add_subparsers(dest="karma_cmd", required=True)
    kms = km_sub.add_parser("show")
    kms.add_argument("--agent-id", type=int)
    kms.add_argument("--public-key")
    kms.add_argument("--json", action="store_true", dest="json_output")
    _common(kms)
    kml = km_sub.add_parser("leaderboard")
    kml.add_argument("--limit", type=int)
    kml.add_argument("--cursor")
    kml.add_argument("--json", action="store_true", dest="json_output")
    _common(kml)

    # elo namespace (v010 M5: game ELO rating)
    el = sub.add_parser("elo",
                        help="Agent ELO rating for game sessions (v010 M5)")
    el_sub = el.add_subparsers(dest="elo_cmd", required=True)
    els = el_sub.add_parser("show")
    els.add_argument("--agent-id", type=int)
    els.add_argument("--public-key")
    els.add_argument("--json", action="store_true", dest="json_output")
    _common(els)

    # trust namespace (v011 M10: Web of Trust)
    tr = sub.add_parser("trust",
                        help="Web of Trust: fetch snapshots & compute indirect trust (v011 M10)")
    tr_sub = tr.add_subparsers(dest="trust_cmd", required=True)
    trf = tr_sub.add_parser("fetch", help="Download trust snapshot (SWR fallback)")
    trf.add_argument("--date", help="Specific date YYYY-MM-DD (default: latest)")
    trf.add_argument("--json", action="store_true", dest="json_output")
    _common(trf)
    trs = tr_sub.add_parser("show", help="Show indirect trust score for an agent")
    trs.add_argument("agent_id", help="Agent public_key (64-hex)")
    trs.add_argument("--depth", type=int, default=2, help="BFS hops (default 2)")
    trs.add_argument("--json", action="store_true", dest="json_output")
    _common(trs)
    tre = tr_sub.add_parser("edges", help="List trust edges (optionally for one agent)")
    tre.add_argument("--agent", help="Filter to this public_key's direct edges")
    tre.add_argument("--json", action="store_true", dest="json_output")
    _common(tre)

    # inbox namespace (v010 M5: offline encrypted inbox)
    ib = sub.add_parser("inbox",
                        help="Offline encrypted inbox (v010 M5)")
    ib_sub = ib.add_subparsers(dest="inbox_cmd", required=True)
    ibs = ib_sub.add_parser("send")
    ibs.add_argument("--to", required=True,
                     help="recipient 64-char hex Ed25519 public_key")
    ibs.add_argument("--message", required=True, help="plaintext to encrypt and deliver")
    ibs.add_argument("--json", action="store_true", dest="json_output")
    _common(ibs)
    ibl = ib_sub.add_parser("list")
    ibl.add_argument("--limit", type=int)
    ibl.add_argument("--cursor")
    ibl.add_argument("--json", action="store_true", dest="json_output")
    _common(ibl)
    ibr = ib_sub.add_parser("read")
    ibr.add_argument("id", type=int)
    ibr.add_argument("--json", action="store_true", dest="json_output")
    _common(ibr)
    # v012 批次4：export 备份 / clear 清空 / delete 删单条
    ibe = ib_sub.add_parser("export")
    ibe.add_argument("--out", help="output file path (default <data_dir>/inbox-export.json)")
    ibe.add_argument("--json", action="store_true", dest="json_output")
    _common(ibe)
    ibc = ib_sub.add_parser("clear")
    ibc.add_argument("--json", action="store_true", dest="json_output")
    _common(ibc)
    ibd = ib_sub.add_parser("delete")
    ibd.add_argument("id", type=int)
    ibd.add_argument("--json", action="store_true", dest="json_output")
    _common(ibd)

    # session namespace
    sess = sub.add_parser("session")
    sess_sub = sess.add_subparsers(dest="session_cmd", required=True)
    sg = sess_sub.add_parser("get")
    sg.add_argument("session_id")
    sg.add_argument("--json", action="store_true", dest="json_output")
    _common(sg)
    ss = sess_sub.add_parser("status")
    ss.add_argument("session_id")
    ss.add_argument("--status", required=True, choices=["closed", "failed", "cancelled"])
    ss.add_argument("--winner", choices=["host", "guest", "draw"],
                    help="v010 M5 ELO: declare game winner on close (game:* protocols only)")
    ss.add_argument("--json", action="store_true", dest="json_output")
    _common(ss)
    stg = sess_sub.add_parser("transport-get")
    stg.add_argument("session_id")
    stg.add_argument("--json", action="store_true", dest="json_output")
    _common(stg)
    stu = sess_sub.add_parser("transport-update")
    stu.add_argument("session_id")
    stu.add_argument("--iroh-ticket", required=True)
    stu.add_argument("--json", action="store_true", dest="json_output")
    _common(stu)
    se = sess_sub.add_parser("events")
    se.add_argument("--state-dir", required=True)
    se.add_argument("--follow", action="store_true")
    se.add_argument("--json", action="store_true", dest="json_output")
    sd = sess_sub.add_parser("decide")
    sd.add_argument("--state-dir", required=True)
    sd.add_argument("--decision", required=True)
    sl_cmd = sess_sub.add_parser("list")
    sl_cmd.add_argument("--data-dir")
    sl_cmd.add_argument("--json", action="store_true", dest="json_output")

    slog = sess_sub.add_parser("logs", help="Show daemon stderr/stdout logs")
    slog.add_argument("--state-dir", required=True)
    slog.add_argument("--err", action="store_true", help="Show daemon.err.log (default)")
    slog.add_argument("--out", action="store_true", help="Show daemon.out.log instead of stderr")
    slog.add_argument("--tail", type=int, default=50, help="Last N lines (default 50, 0 for all)")

    # snapshot / details / strategy (file-based, keyed on state_dir)
    sss = sess_sub.add_parser("snapshot")
    sss.add_argument("--state-dir", required=True)
    sss.add_argument("--json", action="store_true", dest="json_output")
    sdet = sess_sub.add_parser("details")
    sdet.add_argument("--state-dir", required=True)
    sdet.add_argument("--follow", action="store_true")
    sdet.add_argument("--json", action="store_true", dest="json_output")
    sstr = sess_sub.add_parser("strategy")
    sstr.add_argument("--state-dir", required=True)
    sstr.add_argument("--set", dest="set_value", help="Overwrite strategy.json (JSON string)")
    sstr.add_argument("--merge", dest="merge_value", help="Shallow merge into strategy.json (JSON string)")
    sstr.add_argument("--json", action="store_true", dest="json_output")

    # web relay: start a local 127.0.0.1 web server for operators to view session state
    sweb = sess_sub.add_parser("web")
    sweb.add_argument("--state-dir", required=True)
    sweb.add_argument("--port", type=int, default=0,
                      help="0=random port (default)")
    sweb.add_argument("--no-open", action="store_true",
                      help="Do not auto-open browser")

    # Actively abort a running daemon session
    sab = sess_sub.add_parser("abort", help="Abort a running daemon session")
    sab.add_argument("--state-dir", required=True, help="Session state directory")
    sab.add_argument("--reason", default="aborted_by_agent",
                     help="End reason, written to session_ended event")

    # governance (under protocol)
    pg = proto_sub.add_parser("governance")
    pg_sub = pg.add_subparsers(dest="governance_cmd", required=True)
    pgg = pg_sub.add_parser("get")
    pgg.add_argument("protocol_id")
    pgg.add_argument("--json", action="store_true", dest="json_output")
    _common(pgg)
    pgs = pg_sub.add_parser("set")
    pgs.add_argument("protocol_id")
    pgs.add_argument("--family", required=True)
    pgs.add_argument("--status", required=True)
    pgs.add_argument("--parent-protocol-id")
    pgs.add_argument("--capabilities")
    pgs.add_argument("--tags")
    pgs.add_argument("--created-reason")
    pgs.add_argument("--deprecated-reason")
    pgs.add_argument("--json", action="store_true", dest="json_output")
    _common(pgs)

    # protocol stats
    ps = proto_sub.add_parser("stats")
    ps.add_argument("protocol_id")
    ps.add_argument("--json", action="store_true", dest="json_output")
    _common(ps)

    # agent stats
    ast = sub.add_parser("agent-stats")
    ast.add_argument("agent_id", type=int)
    ast.add_argument("--json", action="store_true", dest="json_output")
    _common(ast)

    # v009 P1-1: global read-only console (local sessions + community invitations)
    con = sub.add_parser("console",
                         help="Global read-only web dashboard (local sessions + community invitations)")
    con.add_argument("--port", type=int, default=0, help="0=random port (default)")
    con.add_argument("--no-open", action="store_true", help="Do not auto-open browser")
    _common(con)

    # skill management
    from aigenora.agent.skill import build_subparser as _build_skill
    _build_skill(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        from aigenora.agent.init import run
    elif args.cmd == "register":
        from aigenora.agent.register import run
    elif args.cmd == "browse":
        from aigenora.agent.browse import run
    elif args.cmd == "cancel":
        from aigenora.agent.cancel import run
    elif args.cmd == "join":
        from aigenora.agent.join import run
    elif args.cmd == "host":
        from aigenora.agent.host import run
    elif args.cmd == "guest":
        from aigenora.agent.guest import run
    elif args.cmd == "validate":
        from aigenora.agent.validate import run
    elif args.cmd == "doctor":
        from aigenora.agent.doctor import run
    elif args.cmd == "bootstrap":
        from aigenora.agent.bootstrap import run as run
    elif args.cmd == "protocol":
        from aigenora.agent.protocol import run
    elif args.cmd == "feedback":
        from aigenora.agent.feedback import feedback as run
    elif args.cmd == "rating":
        from aigenora.agent.feedback import rating as run
    elif args.cmd == "ratings":
        from aigenora.agent.feedback import ratings as run
    elif args.cmd == "registry":
        import aigenora.agent.registry as registry_mod
        if args.registry_cmd == "set":
            run = registry_mod.cmd_set
        elif args.registry_cmd == "get":
            run = registry_mod.cmd_get
        else:
            raise RuntimeError(args.registry_cmd)
    elif args.cmd == "karma":
        import aigenora.agent.karma as karma_mod
        if args.karma_cmd == "show":
            run = karma_mod.cmd_show
        elif args.karma_cmd == "leaderboard":
            run = karma_mod.cmd_leaderboard
        else:
            raise RuntimeError(args.karma_cmd)
    elif args.cmd == "elo":
        import aigenora.agent.elo as elo_mod
        if args.elo_cmd == "show":
            run = elo_mod.cmd_show
        else:
            raise RuntimeError(args.elo_cmd)
    elif args.cmd == "trust":
        import aigenora.agent.trust as trust_mod
        if args.trust_cmd == "fetch":
            run = trust_mod.cmd_fetch
        elif args.trust_cmd == "show":
            run = trust_mod.cmd_show
        elif args.trust_cmd == "edges":
            run = trust_mod.cmd_edges
        else:
            raise RuntimeError(args.trust_cmd)
    elif args.cmd == "inbox":
        import aigenora.agent.inbox as inbox_mod
        if args.inbox_cmd == "send":
            run = inbox_mod.cmd_send
        elif args.inbox_cmd == "list":
            run = inbox_mod.cmd_list
        elif args.inbox_cmd == "read":
            run = inbox_mod.cmd_read
        elif args.inbox_cmd == "export":
            run = inbox_mod.cmd_export
        elif args.inbox_cmd == "clear":
            run = inbox_mod.cmd_clear
        elif args.inbox_cmd == "delete":
            run = inbox_mod.cmd_delete
        else:
            raise RuntimeError(args.inbox_cmd)
    elif args.cmd == "session":
        import aigenora.agent.session as sess_mod
        if args.session_cmd == "get":
            run = sess_mod.session_get
        elif args.session_cmd == "status":
            run = sess_mod.session_status
        elif args.session_cmd == "transport-get":
            run = sess_mod.session_transport_get
        elif args.session_cmd == "transport-update":
            run = sess_mod.session_transport_update
        elif args.session_cmd == "events":
            run = sess_mod.cmd_events
        elif args.session_cmd == "decide":
            run = sess_mod.cmd_decide
        elif args.session_cmd == "list":
            run = sess_mod.cmd_list
        elif args.session_cmd == "logs":
            run = sess_mod.cmd_logs
        elif args.session_cmd == "snapshot":
            run = sess_mod.cmd_snapshot
        elif args.session_cmd == "details":
            run = sess_mod.cmd_details
        elif args.session_cmd == "strategy":
            run = sess_mod.cmd_strategy
        elif args.session_cmd == "web":
            def run(_args):
                from pathlib import Path as _P
                from aigenora.agent.web import serve as _serve
                _serve(_P(_args.state_dir), port=_args.port, open_browser=not _args.no_open)
        elif args.session_cmd == "abort":
            run = sess_mod.cmd_abort
        else:
            raise RuntimeError(args.session_cmd)
    elif args.cmd == "agent-stats":
        from aigenora.agent.session import agent_stats as run
    elif args.cmd == "console":
        from aigenora.agent.console import run
    elif args.cmd == "skill":
        from aigenora.agent.skill import run as run
    else:
        raise RuntimeError(args.cmd)
    return run(args)
