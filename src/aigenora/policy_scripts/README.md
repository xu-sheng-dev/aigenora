# 策略脚本编写规范（Policy Script Contract）

策略脚本是用户/agent 自己写的 `.py` 文件，引擎沙箱在每轮游戏窗口打开时执行它，
根据当前局势算出本轮决策。引擎不内置任何游戏规则——所有战术逻辑由脚本实现。

## 放置位置

1. **用户本地**（优先）：`<state_dir>/policy_scripts/<script_id>.py`
2. **包内置示例**（回退）：随 `aigenora` 包安装，`script_id` 直接可用

用户本地同名脚本优先于包内置示例。

## 激活方式

通过 `/api/strategy` 或 `/api/whisper` 设置：

```json
{"mode": "script", "script_id": "my_strategy", "params": {"threshold": 0.7}, "timeout_ms": 1000}
```

- `script_id`：脚本文件名（不含 `.py`），只允许字母/数字/下划线/连字符
- `params`：任意参数，传给脚本（如概率权重、阈值）
- `timeout_ms`：硬超时，默认 1000ms，超时游戏自动 fallback

## 输入合约（JSON stdin）

引擎通过 stdin 传给脚本一个 JSON：

```json
{
  "schema": {
    "match_key": "round",
    "value_field": "choice",
    "choices": {"rock": ["rock","石头"], ...},
    "beats": {"rock": "scissors", ...},
    "policy_family": "rps"
  },
  "context": {
    "supported": true,
    "match_key": "round",
    "match_value": 3,
    "value_field": "choice",
    "legal_values": ["rock", "paper", "scissors"],
    "previous": {
      "self": {"choice": "paper"},
      "opponent": {"choice": "scissors"},
      "result": "loss"
    },
    "history": [
      {"round": 1, "self": {"choice": "rock"}, "opponent": {"choice": "paper"}, "result": "loss"}
    ]
  },
  "strategy": {"mode": "script", "script_id": "my_strategy", "params": {...}},
  "params": {"threshold": 0.7}
}
```

`context` 由协议的 `build_decision_context()` 提供，字段因协议而异。
`schema` 由协议的 `DECISION_SCHEMA` 声明。无动态策略支持的协议 `context.supported=false`。

## 输出合约（JSON stdout）

脚本必须输出一行 JSON 到 stdout：

**成功**：
```json
{"decision": {"choice": "rock"}, "confidence": 0.9, "reason": "counter previous scissors"}
```

- `decision`：必须包含 `value_field` 指定的字段（如 `choice`/`bid`/`number`）
- `decision` 中不必带 `match_key/match_value`，引擎会补齐当前窗口
- `confidence`/`reason`：可选，用于审计

**主动 fallback**（无上下文/不支持）：
```json
{"ok": false, "reason": "no_context"}
```

## 脚本模板

```python
import json, sys

def main():
    inp = json.load(sys.stdin)
    ctx = inp.get("context", {})
    params = inp.get("params", {})
    schema = inp.get("schema", {})

    # 读局势
    opp = ctx.get("previous", {}).get("opponent", {}).get("choice")
    legal = ctx.get("legal_values", [])
    if not opp or opp not in legal:
        print(json.dumps({"ok": False, "reason": "no_context"}))
        return

    # 算决策（你的战术逻辑）
    beats = schema.get("beats", {})
    counter = next((k for k, v in beats.items() if v == opp), None)

    # 输出
    value_field = ctx.get("value_field", "choice")
    if counter:
        print(json.dumps({"decision": {value_field: counter}, "reason": f"counter {opp}"}))
    else:
        print(json.dumps({"ok": False, "reason": "no_counter"}))

if __name__ == "__main__":
    main()
```

## 约束

- 不允许 `import aigenora.proto.hooks` 或直接访问 hooks 对象
- 不允许写 P2P 消息、访问 channel/secret
- 只能通过 JSON stdin 读输入、JSON stdout 写输出
- 硬 timeout：超时游戏自动 fallback，脚本不需要自己管超时
- 不能阻塞（如 `time.sleep` 过久），否则会被 timeout 杀掉
