"""示例策略脚本：下一轮出克制对方上一轮的（一次性）。

读 schema.beats 找克制关系；无 beats 或无上一轮对方动作时 fallback。
"""
import json
import sys


def main():
    inp = json.load(sys.stdin)
    ctx = inp.get("context", {})
    schema = inp.get("schema", {})
    beats = schema.get("beats", {})
    opp = ctx.get("previous", {}).get("opponent", {}).get("choice")

    if not opp:
        print(json.dumps({"ok": False, "reason": "no_context"}))
        return

    # beats 形如 {"rock": "scissors", ...}，找 beats[?] == opp 的 key
    counter = None
    for k, v in beats.items():
        if v == opp:
            counter = k
            break

    if not counter:
        print(json.dumps({"ok": False, "reason": "no_counter"}))
        return

    print(json.dumps({
        "decision": {ctx.get("value_field", "choice"): counter},
        "confidence": 1.0,
        "reason": f"counter {opp} -> {counter}",
    }))


if __name__ == "__main__":
    main()
