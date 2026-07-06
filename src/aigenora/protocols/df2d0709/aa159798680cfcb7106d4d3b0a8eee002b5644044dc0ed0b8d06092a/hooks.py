"""Hero Duel hooks for simultaneous_round engine (v015 declarative balance).

完整 MOBA 风格英雄对决：英雄数值（HP / 蓝量 / 回蓝 / 普攻 + 3 技能）声明在
``options.balance``（双方一致持有，v015 ADR-1），hooks 从 balance 读取裁决，
不再硬编码 HEROES（ADR-3）。每轮双方 commit-reveal 一个动作
（normal / skill_a / skill_b / ult），同时结算伤害；技能需 ``mana >= cost`` 且
``cd == 0`` 才生效，否则退化为普攻；HP 归零或达 ``max_rounds`` 判负。

裁决完全 deterministic（给定 balance + 双方状态 + 双方动作 → 唯一结果），
供 M2 Guest 影子裁决用同一份 balance 本地重算、逐字段 diff Host 的 round_result。
"""
from __future__ import annotations

import copy
import random
import time
from pathlib import Path
from typing import Any

from aigenora.proto.hooks import HookResult, ProtocolHooks
from aigenora.proto.sdk import StateStore


MOVES = ["normal", "skill_a", "skill_b", "ult"]
MOVE_LABEL = {"normal": "Normal", "skill_a": "SkillA", "skill_b": "SkillB", "ult": "Ult"}
SKILL_KEYS = ["skill_a", "skill_b", "ult"]

# 无 options.balance 时的内置默认数值表（与 index.json profile 的 balance 保持一致）。
# 与 best_of 等参数的默认值同理：让协议在无 options 时也能跑通（generic harness / protocol test）。
# 正式用法是通过邀约 options.balance 声明数值（双方一致持有 + M2 影子裁决）；此常量仅作兜底。
DEFAULT_BALANCE: dict[str, dict] = {
    "warrior": {"hp": 250, "mana": 80, "mana_regen": 15, "atk": 25,
        "skill_a": {"dmg": 50, "cost": 20, "cd": 2},
        "skill_b": {"dmg": 70, "cost": 35, "cd": 3},
        "ult": {"dmg": 120, "cost": 60, "cd": 5}},
    "mage": {"hp": 150, "mana": 120, "mana_regen": 20, "atk": 12,
        "skill_a": {"dmg": 60, "cost": 25, "cd": 2},
        "skill_b": {"dmg": 90, "cost": 45, "cd": 3},
        "ult": {"dmg": 180, "cost": 80, "cd": 5}},
    "assassin": {"hp": 180, "mana": 60, "mana_regen": 12, "atk": 35,
        "skill_a": {"dmg": 45, "cost": 15, "cd": 1},
        "skill_b": {"dmg": 65, "cost": 30, "cd": 2},
        "ult": {"dmg": 110, "cost": 50, "cd": 4}},
    "tank": {"hp": 350, "mana": 70, "mana_regen": 14, "atk": 18,
        "skill_a": {"dmg": 35, "cost": 18, "cd": 2},
        "skill_b": {"dmg": 55, "cost": 30, "cd": 3},
        "ult": {"dmg": 90, "cost": 55, "cd": 5}},
}


class Hooks(ProtocolHooks):
    def proto_init(self, options, role, args, state_dir: Path, decision_config: dict[str, Any] | None = None):
        super().proto_init(options, role, args, state_dir, decision_config)
        self.state = StateStore(state_dir)
        # balance 是声明式数值表（v015），双方一致持有；options 未声明时用内置 DEFAULT_BALANCE 兜底
        balance = options.get("balance")
        if isinstance(balance, dict) and balance:
            self.heroes = balance
        else:
            self.heroes = copy.deepcopy(DEFAULT_BALANCE)
        self.hero_names = sorted(self.heroes.keys())
        # 英雄由 options 指定，缺省取阵容第一个（deterministic，便于 M1/M2 验证）
        self.host_hero = self._pick_hero(options.get("host_hero"))
        self.guest_hero = self._pick_hero(options.get("guest_hero"))
        self.max_rounds = int(options.get("max_rounds") or 30)
        self.fallback_strategy: str = args[0] if args else "smart"
        self._init_state()
        self.snapshot.update(
            phase="handshake",
            heroes={"host": self.host_hero, "guest": self.guest_hero},
            hp={"host": self.host_hp, "guest": self.guest_hp},
            mana={"host": self.host_mana, "guest": self.guest_mana},
            round=1,
            max_rounds=self.max_rounds,
        )

    def _pick_hero(self, name: Any) -> str:
        if isinstance(name, str) and name in self.heroes:
            return name
        return self.hero_names[0]

    def _init_state(self) -> None:
        """按双方英雄从 balance 初始化 HP / mana / cd（cd 全 0 = 全技能就绪）。"""
        h = self.heroes[self.host_hero]
        g = self.heroes[self.guest_hero]
        self.host_hp = int(h["hp"])
        self.guest_hp = int(g["hp"])
        self.host_mana = int(h["mana"])
        self.guest_mana = int(g["mana"])
        self.host_cd = {"skill_a": 0, "skill_b": 0, "ult": 0}
        self.guest_cd = {"skill_a": 0, "skill_b": 0, "ult": 0}

    def proto_host_metadata(self):
        return (
            "Hero Duel",
            "game,hero-duel,moba",
            "supply",
            {"host_hero": self.host_hero, "max_rounds": self.max_rounds},
        )

    # ---- balance 读取辅助 ----
    def _hero(self, side: str) -> dict:
        return self.heroes[self.host_hero if side == "host" else self.guest_hero]

    def _mana(self, side: str) -> int:
        return self.host_mana if side == "host" else self.guest_mana

    def _cd(self, side: str) -> dict:
        return self.host_cd if side == "host" else self.guest_cd

    # ---- 裁决核心：纯计算（单一真相源，host 裁决与 guest 影子裁决共用）----
    @staticmethod
    def _resolve_move(hero: dict, mana: int, cd: dict, move: str) -> tuple[str, int, int, str | None]:
        """纯函数：结算一方动作，返回 (生效动作, 伤害, 蓝耗, 释放的技能键|None)。

        技能需 ``mana >= cost`` 且 ``cd == 0`` 才生效，否则退化为普攻（atk）。
        这是裁决的核心确定性来源：不依赖 self 战斗状态，Host 与 Guest 用同份 balance
        重算结果一致（v015-M2 影子裁决的前提）。
        """
        if move in SKILL_KEYS:
            sk = hero[move]
            if mana >= int(sk["cost"]) and cd[move] == 0:
                return move, int(sk["dmg"]), int(sk["cost"]), move
        return "normal", int(hero["atk"]), 0, None

    @staticmethod
    def _game_winner_of(over: bool, host_hp: int, guest_hp: int) -> str:
        """纯函数：判定整局胜负（不依赖 self 战斗状态）。"""
        if not over:
            return "none"
        host_dead = host_hp <= 0
        guest_dead = guest_hp <= 0
        if host_dead and guest_dead:
            return "draw"
        if host_dead:
            return "guest"
        if guest_dead:
            return "host"
        # 走到这说明 max_rounds 到：按剩余 HP 高者
        if host_hp > guest_hp:
            return "host"
        if guest_hp > host_hp:
            return "guest"
        return "draw"

    def _snapshot_state(self) -> dict:
        """抓取当前战斗状态快照（裁决前的输入状态）。"""
        return {
            "host_hp": self.host_hp,
            "guest_hp": self.guest_hp,
            "host_mana": self.host_mana,
            "guest_mana": self.guest_mana,
            "host_cd": dict(self.host_cd),
            "guest_cd": dict(self.guest_cd),
        }

    def _compute_round(self, round_index: int, host_move: str, guest_move: str, st: dict) -> dict:
        """纯计算本回合 round_result（v015-M2 单一真相源）。

        给定状态快照 ``st``（上一轮结束后的 hp/mana/cd）+ 双方动作，返回完整 round_result dict：
        同时结算双方伤害、扣蓝、技能冷却、回合末 cd tick + 回蓝、本轮胜负与 game_over/game_winner。

        **只读** ``self.heroes`` / ``self.host_hero`` / ``self.guest_hero`` / ``self.max_rounds``，
        不修改 ``st``、不碰 ``self`` 战斗状态、不写 details/snapshot。

        ``proto_round_judge``（Host 裁决：apply 到 self + record）与 ``proto_round_judge_pure``
        （Guest 影子重算：只读返回）共用本方法，保证双方裁决逻辑完全一致、无副作用污染。
        """
        h_hero = self.heroes[self.host_hero]
        g_hero = self.heroes[self.guest_hero]
        host_hp = int(st["host_hp"])
        guest_hp = int(st["guest_hp"])
        host_mana = int(st["host_mana"])
        guest_mana = int(st["guest_mana"])
        host_cd = {"skill_a": int(st["host_cd"]["skill_a"]),
                   "skill_b": int(st["host_cd"]["skill_b"]),
                   "ult": int(st["host_cd"]["ult"])}
        guest_cd = {"skill_a": int(st["guest_cd"]["skill_a"]),
                    "skill_b": int(st["guest_cd"]["skill_b"]),
                    "ult": int(st["guest_cd"]["ult"])}

        # 同时结算双方动作（simultaneous，不分先后）
        h_eff, h_dmg, h_cost, h_sk = self._resolve_move(h_hero, host_mana, host_cd, host_move)
        g_eff, g_dmg, g_cost, g_sk = self._resolve_move(g_hero, guest_mana, guest_cd, guest_move)
        # 双方互换伤害（同时扣血）
        guest_hp = max(0, guest_hp - h_dmg)
        host_hp = max(0, host_hp - g_dmg)
        # 扣蓝；释放的技能进入冷却
        host_mana -= h_cost
        guest_mana -= g_cost
        if h_sk:
            host_cd[h_sk] = int(h_hero[h_sk]["cd"])
        if g_sk:
            guest_cd[g_sk] = int(g_hero[g_sk]["cd"])
        # 回合末 cd tick（floor 0）+ 回蓝（上限为 mana cap）
        for cd in (host_cd, guest_cd):
            for k in cd:
                cd[k] = max(0, cd[k] - 1)
        host_mana = min(int(h_hero["mana"]), host_mana + int(h_hero["mana_regen"]))
        guest_mana = min(int(g_hero["mana"]), guest_mana + int(g_hero["mana_regen"]))
        # 本轮胜负（伤害高者；等伤为平）
        if h_dmg > g_dmg:
            winner = "host"
        elif g_dmg > h_dmg:
            winner = "guest"
        else:
            winner = "draw"
        over = host_hp <= 0 or guest_hp <= 0 or (round_index + 1) >= self.max_rounds
        game_winner = self._game_winner_of(over, host_hp, guest_hp)
        return {
            "action": "round_result",
            "round": round_index,
            "host_move": h_eff,
            "guest_move": g_eff,
            "host_hp": host_hp,
            "guest_hp": guest_hp,
            "host_mana": host_mana,
            "guest_mana": guest_mana,
            "host_damage_dealt": h_dmg,
            "guest_damage_dealt": g_dmg,
            "host_cd_a": host_cd["skill_a"],
            "host_cd_b": host_cd["skill_b"],
            "host_cd_ult": host_cd["ult"],
            "guest_cd_a": guest_cd["skill_a"],
            "guest_cd_b": guest_cd["skill_b"],
            "guest_cd_ult": guest_cd["ult"],
            "round_winner": winner,
            "game_over": over,
            "game_winner": game_winner,
        }

    # ---- 动作策略 ----
    def _pick_auto(self, round_index: int) -> str:
        strat = self.strategy.read()
        if strat:
            mode = strat.get("mode", "smart")
            if mode == "fixed":
                m = strat.get("move")
                if m in MOVES:
                    return m
            elif mode == "seq":
                seq = [x for x in strat.get("sequence", []) if x in MOVES]
                if seq:
                    return seq[round_index % len(seq)]
            elif mode == "random":
                return random.choice(MOVES)
        s = self.fallback_strategy
        if s in MOVES:
            return s
        if s.startswith("seq:"):
            seq = [x for x in s[4:].split(",") if x in MOVES]
            if seq:
                return seq[round_index % len(seq)]
        # smart：优先放就绪的高伤技能（ult > skill_b > skill_a），否则普攻
        side = "host" if self.role == "host" else "guest"
        for sk in ("ult", "skill_b", "skill_a"):
            hero = self._hero(side)
            if self._mana(side) >= int(hero[sk]["cost"]) and self._cd(side)[sk] == 0:
                return sk
        return "normal"

    def _pick(self, round_index: int) -> str:
        auto = self._pick_auto(round_index)
        if self.bus is None:
            return auto
        # hybrid（auto 模式，默认）：非阻塞读 decide，无则 auto
        if self.decision_mode != "manual":
            d = self._consume_hybrid("round", round_index)
            if d and d.get("move") in MOVES:
                return d["move"]
            return auto
        # --coach（manual）：阻塞逐手等待
        if not self.timing_enabled:
            return auto
        now = time.monotonic()
        min_think = float(self.options.get("min_think_seconds", self.timing["min_think_seconds"]))
        max_think = float(self.options.get("max_think_seconds", self.timing["max_think_seconds"]))
        fallback = {"round": round_index, "move": self._pick_auto(round_index)}
        self._update_timing_snapshot("round", round_index, now + min_think, now + max_think, "waiting")
        decision = self.bus.await_latest_decision(
            match_key="round", match_value=round_index,
            release_at=now + min_think, deadline_at=now + max_think, fallback_value=fallback,
        )
        self._clear_timing_snapshot()
        move = decision.get("move")
        return move if move in MOVES else fallback["move"]

    # ---- simultaneous_round hooks ----
    def proto_round_value(self, round_index: int, state: dict) -> str:
        return self._pick(round_index)

    def proto_round_judge(self, round_index: int, host_move: str, guest_move: str, state: dict) -> HookResult:
        # 纯计算本回合（单一真相源 _compute_round），再把结果 apply 到 self 战斗状态 + 记录。
        # apply 必须在重算之后：_compute_round 只读输入状态，写回其产出保证 self 与 resp 一致。
        resp = self._compute_round(round_index, host_move, guest_move, self._snapshot_state())
        self.host_hp = resp["host_hp"]
        self.guest_hp = resp["guest_hp"]
        self.host_mana = resp["host_mana"]
        self.guest_mana = resp["guest_mana"]
        self.host_cd = {"skill_a": resp["host_cd_a"], "skill_b": resp["host_cd_b"], "ult": resp["host_cd_ult"]}
        self.guest_cd = {"skill_a": resp["guest_cd_a"], "skill_b": resp["guest_cd_b"], "ult": resp["guest_cd_ult"]}
        self._record_round(round_index, resp["host_move"], resp["guest_move"],
                           resp["host_damage_dealt"], resp["guest_damage_dealt"],
                           resp["round_winner"], resp["game_over"], resp["game_winner"])
        return HookResult(resp, completed=resp["game_over"])

    def proto_round_judge_pure(self, round_index: int, host_move: str, guest_move: str, state: dict) -> dict:
        """影子裁决（v015-M2）：用当前 self 战斗状态（= 上一轮结束后状态）纯重算本回合 round_result。

        与 proto_round_judge 共用 _compute_round（裁决单一真相源），但**不修改** self 战斗状态、
        **不写** details/snapshot。供 Guest 收到 Host round_result 后逐字段 diff，验证 Host 是否诚实裁决。
        self.heroes 已在 proto_init 从 options.balance 读取（与 Host 同份 balance），故重算结果可信。
        """
        return self._compute_round(round_index, host_move, guest_move, self._snapshot_state())

    def _record_round(self, round_index: int, host_move: str, guest_move: str,
                      h_dmg: int, g_dmg: int, winner: str, over: bool, game_winner: str) -> None:
        rd = round_index + 1
        self.details.append(
            type="round_result",
            round=rd,
            host_move=host_move, guest_move=guest_move,
            host_hp=self.host_hp, guest_hp=self.guest_hp,
            host_mana=self.host_mana, guest_mana=self.guest_mana,
            host_damage=h_dmg, guest_damage=g_dmg,
            winner=winner, game_over=over, game_winner=game_winner,
            summary=(f"R{rd}: {MOVE_LABEL[host_move]}({h_dmg}) vs "
                     f"{MOVE_LABEL[guest_move]}({g_dmg}) -> H:{self.host_hp} G:{self.guest_hp}"),
        )
        next_round = rd + 1 if not over else rd
        phase = "playing" if not over else "game_over"
        summary = (
            f"R{rd}: {winner} lands more, H:{self.host_hp} G:{self.guest_hp}"
            if not over else
            f"Game over: {game_winner} (H:{self.host_hp} G:{self.guest_hp})"
        )
        self.snapshot.update(
            phase=phase,
            hp={"host": self.host_hp, "guest": self.guest_hp},
            mana={"host": self.host_mana, "guest": self.guest_mana},
            round=next_round,
            last_event={"summary": summary, "structured": {
                "round": rd, "winner": winner, "game_over": over, "game_winner": game_winner}},
        )

    # ---- 展示 ----
    def proto_display(self, msg, direction):
        if msg.get("action") != "round_result":
            return None
        rd = msg["round"] + 1
        lines = [
            f"--- Round {rd} ---",
            (f"Host {MOVE_LABEL[msg['host_move']]}({msg['host_damage_dealt']})  vs  "
             f"Guest {MOVE_LABEL[msg['guest_move']]}({msg['guest_damage_dealt']})"),
            f"HP:   Host {msg['host_hp']:<5}  Guest {msg['guest_hp']}",
            f"Mana: Host {msg['host_mana']:<5}  Guest {msg['guest_mana']}",
            f"Round winner: {msg['round_winner']}",
        ]
        if msg["game_over"]:
            gw = msg["game_winner"]
            ws = {"host": "Host wins!", "guest": "Guest wins!", "draw": "Draw!"}.get(gw, gw)
            lines.append("")
            lines.append(f"=== Game Over ===  {ws}")
        return "\n".join(lines)

    # ---- join / ready 握手 ----
    def proto_host_handle_join(self, msg):
        gh = msg.get("hero")
        if gh in self.heroes:
            self.guest_hero = gh
        self.max_rounds = int(msg.get("max_rounds", self.max_rounds))
        # join 后按双方英雄重新初始化战斗状态
        self._init_state()
        self.snapshot.update(
            phase="playing",
            heroes={"host": self.host_hero, "guest": self.guest_hero},
            hp={"host": self.host_hp, "guest": self.guest_hp},
            round=1, max_rounds=self.max_rounds,
        )
        return HookResult({
            "action": "ready",
            "host_hero": self.host_hero,
            "guest_hero": self.guest_hero,
            "max_rounds": self.max_rounds,
        })

    def proto_guest_join_message(self):
        return {"action": "join", "hero": self.guest_hero, "max_rounds": self.max_rounds}

    def proto_guest_handle_ready(self, msg):
        self.host_hero = msg.get("host_hero", self.host_hero)
        self.guest_hero = msg.get("guest_hero", self.guest_hero)
        self.max_rounds = int(msg.get("max_rounds", self.max_rounds))
        self._init_state()
        self.snapshot.update(
            phase="playing",
            heroes={"host": self.host_hero, "guest": self.guest_hero},
            hp={"host": self.host_hp, "guest": self.guest_hp},
            round=1, max_rounds=self.max_rounds,
        )

    def proto_guest_handle(self, msg):
        # Guest 侧从 round_result 同步状态（Guest 不裁决；M2 将在此本地重算并 diff）
        if msg.get("action") == "round_result":
            self.host_hp = int(msg["host_hp"])
            self.guest_hp = int(msg["guest_hp"])
            self.host_mana = int(msg["host_mana"])
            self.guest_mana = int(msg["guest_mana"])
            self.host_cd = {"skill_a": int(msg["host_cd_a"]), "skill_b": int(msg["host_cd_b"]), "ult": int(msg["host_cd_ult"])}
            self.guest_cd = {"skill_a": int(msg["guest_cd_a"]), "skill_b": int(msg["guest_cd_b"]), "ult": int(msg["guest_cd_ult"])}
            over = bool(msg["game_over"])
            game_winner = msg.get("game_winner", "none")
            self._record_round(
                int(msg["round"]), msg["host_move"], msg["guest_move"],
                int(msg["host_damage_dealt"]), int(msg["guest_damage_dealt"]),
                msg["round_winner"], over, game_winner,
            )
        return HookResult()
