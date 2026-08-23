from __future__ import annotations

import json
from pathlib import Path

from aigenora.gamekit import (
    SUPPORTED_PRESETS,
    compile_game,
    default_manifest,
    inspect_game_spec,
    materialize_protocol,
)


def _print_report(report: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for key in (
        "status",
        "name",
        "preset",
        "protocol_id",
        "materialization",
        "hooks",
        "ui",
        "elapsed_ms",
        "output",
    ):
        if key in report:
            print(f"{key}: {report[key]}")
    if report.get("reason"):
        print(f"reason: {report['reason']}")


def run(args) -> int:
    if args.game_cmd == "presets":
        report = {
            "schema": "aigenora-game-kit-presets/1",
            "presets": list(SUPPORTED_PRESETS),
        }
        _print_report(report, as_json=bool(args.json_output))
        if not args.json_output:
            for preset in SUPPORTED_PRESETS:
                print(preset)
        return 0
    if args.game_cmd == "new":
        output = Path(args.output).resolve()
        if output.exists() and not args.force:
            raise FileExistsError(
                f"blueprint already exists; pass --force to replace it: {output}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                default_manifest(args.preset),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[OK] blueprint created: {output}")
        return 0
    if args.game_cmd == "build":
        blueprint = json.loads(Path(args.blueprint).read_text(encoding="utf-8"))
        report = compile_game(args.output, blueprint, force=bool(args.force))
        _print_report(report, as_json=True)
        return 0
    if args.game_cmd == "inspect":
        report = inspect_game_spec(args.source)
        _print_report(report, as_json=bool(args.json_output))
        return 0 if report["status"] in {"ready", "not_game_kit"} else 2
    if args.game_cmd == "materialize":
        report = materialize_protocol(
            args.protocol_dir,
            include_ui=not bool(args.no_ui),
            force=bool(args.force),
            run_smoke=bool(args.smoke),
        )
        _print_report(report, as_json=True)
        return 0
    raise RuntimeError(f"unknown game command: {args.game_cmd}")
