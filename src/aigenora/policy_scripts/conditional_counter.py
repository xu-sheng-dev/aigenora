"""示例策略脚本：对方连续两次出同一招时出克制它的，否则随机。

条件分支策略示例：读 history 末尾两条判断。
"""
import json
import random
import sys


def main():
    inp = json.load(sys.stdin)
    ctx = inp.get("context", {})
    schema = inp.get("schema", {})
    beats = schema.get("beats", {})
    history = ctx.get("history", [])
    legal = ctx.get("legal_values", [])

    if len(history) >= 2:
        last_two = history[-2:]
        opp1 = last_two[0].get("opponent", {}).get("choice")
        opp2 = last_two[1].get("opponent", {}).get("choice")
        if opp1 and opp1 == opp2:
            # 连续两次同招，出克制
            counter = None
            for k, v in beats.items():
                if v == opp1:
                    counter = k
                    break
            if counter:
                print(json.dumps({
                    "decision": {ctx.get("value_field", "choice"): counter},
                    "confidence": 0.9,
                    "reason": f"conditional counter: opponent repeated {opp1}",
                }))
                return

    # 否则随机
    if legal:
        choice = random.choice(legal)
        print(json.dumps({
            "decision": {ctx.get("value_field", "choice"): choice},
            "confidence": 0.3,
            "reason": "random fallback (no repeat detected)",
        }))
    else:
        print(json.dumps({"ok": False, "reason": "no_legal_values"}))


if __name__ == "__main__":
    main()
