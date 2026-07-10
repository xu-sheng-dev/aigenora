"""aigenora 内置策略脚本示例包。

这些脚本随 Python 包安装分发，安装后 agent 可通过 script_id 直接引用，
无需用户自己放文件。用户也可以在 <state_dir>/policy_scripts/ 放自己的脚本，
引擎查找时用户本地脚本优先于包内置示例。

每个脚本遵守同一合约：
- JSON stdin 输入：{schema, context, strategy, params}
- JSON stdout 输出：{decision: {value_field: value}, confidence?, reason?} 或 {ok: false, reason: ...}
- 不 import hooks、不写 P2P 消息、不访问 channel/secret
- 硬 timeout（引擎沙箱控制，脚本自身不需要管）

内置示例：
- weighted_mirror: 按权重概率模仿对方上一轮（params.mirror_weight 控制）
- counter_once: 一次性克制对方上一轮（读 schema.beats）
- conditional_counter: 条件分支（对方连续两次同招才克制）
"""
