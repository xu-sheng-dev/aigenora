from __future__ import annotations

import json

from aigenora.proto.spec_version import check_spec_version
from aigenora.proto.validate import load_spec, validate_message_obj


def run(args) -> int:
    spec = load_spec(args.spec)
    check_spec_version(spec, reject_unknown=True)
    msg = json.loads(args.message_json)
    validate_message_obj(spec, msg, direction=args.direction, message_name=args.message_name)
    if not args.quiet:
        print("OK")
    return 0

