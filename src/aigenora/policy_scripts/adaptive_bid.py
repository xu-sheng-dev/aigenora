"""示例策略脚本：Weak Wins All 自适应出价。

根据对方上一轮出价调整本轮出价：
- 对方上一轮出价 > 我方上一轮 → 本轮出对方上一轮 - 1（弱赢）
- 对方上一轮出价 <= 我方上一轮 → 本轮出一个较小的值（保守）
- 无历史 → 出 params.default_bid（默认 1）

params:
  default_bid: int, 无历史时的默认出价（默认 1）
  conservative_bid: int, 保守出价（默认 1）
"""
import json
import sys


def main():
    inp = json.load(sys.stdin)
    ctx = inp.get("context", {})
    params = inp.get("params", {})
    default_bid = params.get("default_bid", 1)
    conservative_bid = params.get("conservative_bid", 1)

    prev = ctx.get("previous", {})
    opp_bid = prev.get("opponent", {}).get("bid")
    self_bid = prev.get("self", {}).get("bid")

    if opp_bid is None or self_bid is None:
        print(json.dumps({
            "decision": {"bid": default_bid},
            "reason": f"no history, default bid {default_bid}",
        }))
        return

    if opp_bid > self_bid:
        # 对方上轮出价更高，本轮出对方-1 弱赢
        bid = max(1, opp_bid - 1)
        print(json.dumps({
            "decision": {"bid": bid},
            "reason": f"counter: opp bid {opp_bid} > self {self_bid}, bid {bid} to weak-win",
        }))
    else:
        # 对方上轮出价不高，保守
        print(json.dumps({
            "decision": {"bid": conservative_bid},
            "reason": f"conservative: opp bid {opp_bid} <= self {self_bid}",
        }))


if __name__ == "__main__":
    main()
