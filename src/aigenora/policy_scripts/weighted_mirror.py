"""示例策略脚本：60% 概率模仿对方上一轮，剩下 40% 另外两个均分。

比例由 params.mirror_weight 控制（默认 0.6）。
引擎沙箱加载，JSON stdin 输入，JSON stdout 输出。不含游戏规则，只读 context。
"""
import json
import random
import sys


def main():
    inp = json.load(sys.stdin)
    ctx = inp.get("context", {})
    params = inp.get("params", {})
    w = params.get("mirror_weight", 0.6)

    opp_last = ctx.get("previous", {}).get("opponent", {}).get("choice")
    legal = ctx.get("legal_values", [])

    if not opp_last or opp_last not in legal:
        print(json.dumps({"ok": False, "reason": "no_context"}))
        return

    others = [v for v in legal if v != opp_last]
    if random.random() < w:
        choice = opp_last
    else:
        choice = random.choice(others) if others else opp_last

    print(json.dumps({
        "decision": {ctx.get("value_field", "choice"): choice},
        "confidence": w,
        "reason": f"weighted mirror w={w}, chose {choice}",
    }))


if __name__ == "__main__":
    main()
