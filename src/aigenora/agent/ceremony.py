from __future__ import annotations

import json
import sys

from aigenora.proto.hidden_role import (
    PROFILE_ID,
    HiddenRoleError,
    verify_terminal_artifact_file,
)


NOTICE = (
    "EXPERIMENTAL LOCAL RESEARCH RC; NOT EXTERNALLY AUDITED; "
    "NOT FOR REAL-STAKE DECISIONS."
)


def run(args) -> int:
    print(NOTICE, file=sys.stderr)
    command = (args.ceremony_cmd, args.ceremony_subcmd)
    if command == ("hidden-role", "verify"):
        try:
            result = verify_terminal_artifact_file(args.artifact)
        except HiddenRoleError as exc:
            print(
                json.dumps(
                    {
                        "code": exc.code,
                        "error": exc.message,
                        "status": "rejected",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
        return 0
    if command == ("hidden-role", "profile"):
        value = {
            "externally_audited": False,
            "profile_id": PROFILE_ID,
            "production_ready": False,
            "security_model": "at-least-one-honest-mix-and-terminal-audit",
            "status": "local-research-rc",
        }
        if args.json_output:
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        else:
            for key, item in value.items():
                print(f"{key}: {item}")
        return 0
    raise RuntimeError(f"unsupported ceremony command: {command}")
