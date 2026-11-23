# -*- coding: utf-8 -*-
"""
明日方舟「雪山降临1101」小游戏「喀兰贸易技术研发部」
核心精确策略求解引擎 (Exact Optimal Strategy Solver)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import time

# 前5关与后5关资源定义
RESOURCE_SETS = {
    "early": ["gear", "crystal", "wood"],     # 第1~5关: 齿轮, 水晶, 木头
    "late": ["crystal", "flame", "steel"],    # 第6~10关: 水晶, 火苗, 工字钢
}

RESOURCE_LABELS = {
    "gear": "齿轮",
    "crystal": "水晶",
    "wood": "木头",
    "flame": "火苗",
    "steel": "工字钢",
}

# 10个关卡静态元数据（均经模拟器实采验证）
STAGES: Dict[int, dict] = {
    1: {
        "name": "地基重建",
        "res_set": "early",
        "max_turns": 6,
        "target_pts": 4,
        "card_range": (1, 5),
        "init_base": 1,
        "lap_bonus": 0,
        "hand_scale": False,
        "has_x2": True,
        "has_lock": False,
        "has_chest": False,
        "desc": "每2回合，随机一片区域的基础资源产出×2倍",
    },
    2: {
        "name": "物资支援",
        "res_set": "early",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 5),
        "init_base": 2,
        "lap_bonus": 0,
        "hand_scale": False,
        "has_x2": True,
        "has_lock": False,
        "has_chest": False,
        "desc": "每2回合，随机一片区域的基础资源产出×2倍",
    },
    3: {
        "name": "额外收获",
        "res_set": "early",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 5),
        "init_base": 2,
        "lap_bonus": 0,
        "hand_scale": False,
        "has_x2": False,
        "has_lock": False,
        "has_chest": False,
        "desc": "每2回合，所有区域的资源种类重新随机",
    },
    4: {
        "name": "灵活配给",
        "res_set": "early",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 5),
        "init_base": 2,
        "lock_base": 4,
        "lap_bonus": 0,
        "hand_scale": False,
        "has_x2": False,
        "has_lock": True,
        "has_chest": False,
        "desc": "每2回合，随机锁定4个区域无法收集，未封锁区域基础产出提升(2→4)",
    },
    5: {
        "name": "熟能生巧",
        "res_set": "early",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 3),
        "init_base": 2,
        "lap_bonus": 1,
        "hand_scale": False,
        "has_x2": False,
        "has_lock": False,
        "has_chest": False,
        "desc": "初始基础产出降低，每完成一圈所有区域基础资源产出+1",
    },
    6: {
        "name": "雪地奇珍",
        "res_set": "late",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 5),
        "init_base": 3,
        "lap_bonus": 0,
        "hand_scale": False,
        "has_x2": False,
        "has_lock": False,
        "has_chest": True,
        "chest_amount": 8,
        "desc": "每2回合随机区域出现宝箱(持续2回合)，经过可得随机资源+8",
    },
    7: {
        "name": "细致修缮",
        "res_set": "late",
        "max_turns": 8,
        "target_pts": 10,
        "card_range": (1, 5),
        "init_base": 3,
        "lap_bonus": 0,
        "hand_scale": True,
        "has_x2": False,
        "has_lock": False,
        "has_chest": False,
        "desc": "初始手牌减少(4张)，每获得2重建值手牌+1(最多+4张)",
    },
    8: {
        "name": "联合作业",
        "res_set": "late",
        "max_turns": 10,
        "target_pts": 12,
        "card_range": (1, 5),
        "init_base": 5,
        "lap_bonus": 0,
        "hand_scale": True,
        "has_x2": False,
        "has_lock": False,
        "has_chest": False,
        "desc": "初始手牌减少，每2重建值手牌+1(最多+4)；每2回合区域资源重随机",
    },
    9: {
        "name": "竣工检查",
        "res_set": "late",
        "max_turns": 10,
        "target_pts": 12,
        "card_range": (1, 5),
        "init_base": 5,
        "lock_base": 7,
        "lap_bonus": 0,
        "hand_scale": True,
        "has_x2": False,
        "has_lock": True,
        "has_chest": False,
        "desc": "初始手牌减少，每2重建值手牌+1(最多+4)；每2回合封锁4区且未封锁产出提升(5→7)",
    },
    10: {
        "name": "重建家园",
        "res_set": "late",
        "max_turns": 10,
        "target_pts": 12,
        "card_range": (1, 3),
        "init_base": 3,
        "lap_bonus": 2,
        "hand_scale": False,
        "has_x2": False,
        "has_lock": False,
        "has_chest": True,
        "chest_amount": 15,
        "desc": "初始基础产出降低，每完成一圈所有区域产出+2；每2回合随机刷宝箱(+15)",
    },
}

COMBO_BONUS = 2           # 实机验证: 每次触发 COMBO+ 全种类资源固定 +2
DEFAULT_CHEST_AMOUNT = 15 # 第6关为8，第10关为15


@dataclass
class TileState:
    idx: int
    res: str          # 'gear'|'crystal'|'wood'|'flame'|'steel'
    mult: int = 1     # 1 or 2 (优质收获区 ×2)
    locked: bool = False
    chest: bool = False
    cx: int = 0       # 屏幕像素坐标(可视化用)
    cy: int = 0


@dataclass
class QuestItem:
    res: str
    cur: int
    target: int


@dataclass
class QuestState:
    reward: int       # 4 or 1
    items: List[QuestItem]


@dataclass
class GameState:
    stage_id: int
    res_types: List[str]          # 长度3，如 ['crystal', 'flame', 'steel']
    tiles: List[TileState]        # 长度10，顺时针0..9，0为火车站
    pos: int                      # 0..9
    base_yield: int               # 当前全图未封锁格子的基础产出
    hand: List[int]               # 当前手牌点数列表，例如 [1, 2, 3, 3, 5]
    quests: List[QuestState]      # 当前3条重建计划
    cur_turn: int = 1
    max_turns: int = 8
    cur_pts: int = 0
    target_pts: int = 10
    can_collect: bool = False     # 是否刚完成前进，可直接首次收集
    last_collect: Optional[int] = None  # 若处于连续收集COMBO+状态，上一张收集牌点数
    lap_bonus: int = 0            # 每完成一圈全图基础产出+N (第5关+1, 第10关+2)
    hand_scale: bool = False      # 是否每2重建值手牌+1 (第7/8/9关)
    chest_amount: int = 15        # 每个宝箱随机给予任意1种资源的数量 (第6关=8, 第10关=15)


@dataclass
class StepDetail:
    step_no: int
    action: str          # 'advance' | 'collect' | 'combo'
    card: int
    from_pos: int
    to_pos: int
    tile_res: str
    gained: Dict[str, int]
    chests_hit: List[int]
    laps_crossed: int
    new_base_yield: int
    completed_quests: List[int]   # 本步完成的计划编号(0,1,2)
    pts_gained: int
    needs_resync: bool            # 本步完成后触发了新计划或宝箱，建议重算
    note: str


@dataclass
class PlanResult:
    rank: int
    score: float
    expected_pts: float           # 期望立刻获得的重建值
    guaranteed_pts: int           # 确定立刻获得的重建值（不含宝箱随机）
    progress_value: float         # 对未完成计划的等效推进价值
    lap_value: float              # 过圈成长折现价值
    total_gained: Dict[str, int]  # 确定采集总资源量
    chests_count: int
    laps_count: int
    end_pos: int
    end_base_yield: int
    final_quests: List[dict]
    steps: List[StepDetail]
    first_resync_step: Optional[int]


def _evaluate_terminal(
    res_types: List[str],
    quests_raw: List[Tuple[int, List[Tuple[int, int, int]]]],
    # quests_raw: [(reward, [(res_idx, cur, target), ...])]
    det_gains: Tuple[int, int, int],
    slot_overflow: Tuple[int, int, int],  # 各槽位在各自完成之后获得的独立后续资源量
    completed_mask_det: int,
    earliest_comp_rem_cards: int,
    chests_count: int,
    laps_count: int,
    end_pos: int,
    end_base_y: int,
    rem_turns: int,
    cur_pts: int,
    stage_id: int,
    lap_bonus: int,
    hand_scale: bool,
    res_set_is_late: bool,
    tiles_info: List[Tuple[int, int, bool]],  # (res_idx, mult, locked)
    chest_amount: int = DEFAULT_CHEST_AMOUNT,
    next_turn_resets_field: bool = False,
    is_early_end: bool = False,
) -> Tuple[float, float, int, float, float]:
    """
    精确计算终态得分（含宝箱全枚举 3^m 精确期望、倒数第2回合可达性估值、单边清零激励、
    按槽位独立溢出及末回合新单斩杀期望、全10关奇偶回合场地刷新节奏）。
    返回: (total_score, expected_pts, guaranteed_pts, progress_val, lap_val)
    """
    s_meta = STAGES.get(stage_id, STAGES[1])
    c_max = s_meta["card_range"][1]
    c_avg = (1 + c_max) / 2.0

    # 按关卡体量估算新刷出计划的典型总需求量
    if stage_id == 1:
        avg_req_4, avg_req_1 = 30.0, 8.0
    elif not res_set_is_late:
        avg_req_4, avg_req_1 = 60.0, 16.0
    elif stage_id in (6, 7):
        avg_req_4, avg_req_1 = 120.0, 30.0
    else:
        avg_req_4, avg_req_1 = 140.0, 36.0

    # 预估下一回合期望产能（用于 rem_turns == 1 倒数第2回合可达性判断）
    def est_single_turn_cap(pts_after: int) -> float:
        h_next = (4 + min(4, pts_after // 2)) if hand_scale else 5
        # 约 (h_next - 1) 张牌可有效采集，含 COMBO 与 ×2 平均增益因子 ~1.25
        eff_base = end_base_y
        if stage_id in (1, 2):
            eff_base *= 1.35
        return max(12.0, (h_next - 1) * c_avg * eff_base * 1.25 + 6.0)

    def score_one_outcome(extra: Tuple[int, int, int]) -> Tuple[float, float, float, float]:
        # 返回 (pts, prog_val + overflow_val + scale_bonus, pts, last_turn_new_quest_exp)
        tot = (det_gains[0] + extra[0], det_gains[1] + extra[1], det_gains[2] + extra[2])
        pts = 0.0
        prog_val = 0.0
        overflow_val = 0.0
        last_turn_extra_pts = 0.0
        comp_count_pts = 0

        # 第一遍先算出本结局确定拿到的重建值，便于精确判断 hand_scale 和倒数第2回合下回合手牌数
        q_evals = []
        for qi, (reward, items) in enumerate(quests_raw):
            if not items:
                q_evals.append((qi, reward, items, False, 0, 0, 0, 0))
                continue
            sum_t = 0
            sum_c = 0
            rem_gap = 0
            done_sides = 0
            done = True
            for r_idx, cur, target in items:
                nv = cur + tot[r_idx]
                if nv < target:
                    done = False
                    rem_gap += target - nv
                else:
                    done_sides += 1
                sum_t += target
                sum_c += nv if nv < target else target
            if done:
                pts += reward
                comp_count_pts += reward
            q_evals.append((qi, reward, items, done, sum_c, sum_t, rem_gap, done_sides))

        next_cap = est_single_turn_cap(cur_pts + comp_count_pts) if rem_turns == 1 else 1e9

        for qi, reward, items, done, sum_c, sum_t, rem_gap, done_sides in q_evals:
            if not items:
                continue
            if done:
                # 仅使用该槽位【自身完成后】产生的独立后续溢出量 slot_overflow[qi]
                if completed_mask_det & (1 << qi):
                    ov_q = slot_overflow[qi] if qi < 3 else 0
                    if ov_q > 0:
                        avg_req = avg_req_4 if reward >= 4 else avg_req_1
                        if rem_turns > 0:
                            # ×4 大单早交后剩余手牌可重同步导向新大单，适度提升大单溢出权重，小单保持克制避免破坏大单连击
                            ov_k = 0.60 if reward >= 4 else 0.50
                            overflow_val += min(reward * 0.72, (ov_q * 0.54 / avg_req) * reward * ov_k)
                        else:
                            # 末回合提前完成旧单后，剩余手牌打出的溢出量有概率当回合再斩一条新刷出的单
                            kill_prob = min(0.55, (ov_q / avg_req) * 0.52)
                            last_turn_extra_pts += kill_prob * reward * 0.35
            else:
                if rem_turns > 0 and sum_t > 0:
                    p = sum_c / sum_t
                    # 凸进度价值 + 双资源计划单边清零激励（减少下回合换格移动成本）
                    single_side_bonus = 0.045 if (len(items) == 2 and done_sides == 1) else 0.0
                    raw_prog = reward * min(0.95, 0.78 * p + 0.14 * p * p + single_side_bonus)

                    # 倒数第 2 回合 (rem_turns == 1) 可达性折减：温和优先保稳单，仍保留天胡绝杀大单的进取价值
                    if rem_turns == 1:
                        if rem_gap <= next_cap * 1.15:
                            reach_factor = 1.0
                        elif rem_gap <= next_cap * 1.60:
                            reach_factor = 0.78
                        else:
                            reach_factor = 0.55
                        raw_prog *= reach_factor

                    prog_val += raw_prog

        # 第7/8/9关：每获得2重建值，后续每回合手牌+1（最多+4张，即上限8点重建值）
        scale_bonus = 0.0
        if hand_scale and rem_turns > 0:
            old_tier = min(4, cur_pts // 2)
            new_tier = min(4, (cur_pts + comp_count_pts) // 2)
            if new_tier > old_tier:
                scale_bonus = (new_tier - old_tier) * min(rem_turns, 8) * 0.62
            elif cur_pts < 8 and ((cur_pts + comp_count_pts) % 2 == 1) and comp_count_pts > 0:
                scale_bonus = min(rem_turns, 8) * 0.12

        return pts, prog_val + overflow_val + scale_bonus, pts, last_turn_extra_pts

    # 计算确定获得的重建值（不靠宝箱）
    _, _, guar_pts_f, _ = score_one_outcome((0, 0, 0))
    guaranteed_pts = int(round(guar_pts_f))

    # 精确枚举宝箱随机结果（最多枚举到4个宝箱=81种，超出则按期望近似）
    if chests_count == 0:
        exp_pts, exp_prog, _, exp_lt_ov = score_one_outcome((0, 0, 0))
    elif chests_count <= 4:
        outcomes: Dict[Tuple[int, int, int], int] = {(0, 0, 0): 1}
        for _ in range(chests_count):
            nxt: Dict[Tuple[int, int, int], int] = {}
            for (a, b, c), cnt in outcomes.items():
                for da, db, dc in ((chest_amount, 0, 0), (0, chest_amount, 0), (0, 0, chest_amount)):
                    k = (a + da, b + db, c + dc)
                    nxt[k] = nxt.get(k, 0) + cnt
            outcomes = nxt
        total_w = 3 ** chests_count
        exp_pts = 0.0
        exp_prog = 0.0
        exp_lt_ov = 0.0
        for extra, w in outcomes.items():
            p, pv, _, lt_ov = score_one_outcome(extra)
            exp_pts += p * w
            exp_prog += pv * w
            exp_lt_ov += lt_ov * w
        exp_pts /= total_w
        exp_prog /= total_w
        exp_lt_ov /= total_w
    else:
        e_each = (chests_count * chest_amount) // 3
        exp_pts, exp_prog, _, exp_lt_ov = score_one_outcome((e_each, e_each, e_each))

    # 奇数回合宝箱即将消失刷新，适度增加顺路拾取的紧迫激励
    if chests_count > 0 and next_turn_resets_field and rem_turns > 0 and s_meta.get("has_chest"):
        exp_prog += chests_count * 0.04

    # 过圈成长价值（第5关 +1，第10关 +2；保持平衡防止为刷圈而过度放弃采集）
    lap_val = 0.0
    if lap_bonus > 0 and rem_turns > 0:
        per_yield_per_turn = 0.12 if res_set_is_late else 0.22
        lap_val = laps_count * lap_bonus * per_yield_per_turn * min(rem_turns, 8)
        lap_val += end_pos * 0.02 * lap_bonus

    # 早完成早刷新微调奖励
    early_comp_bonus = 0.025 * earliest_comp_rem_cards if rem_turns > 0 else 0.008 * earliest_comp_rem_cards

    # 末位站位微调（按关卡奇偶刷新节奏 + 点数范围 1~3 / 1~5 自适应）
    pos_bonus = 0.0
    reroll_stage = stage_id in (3, 8)
    if rem_turns > 0 and not (reroll_stage and next_turn_resets_field):
        need = [0.0, 0.0, 0.0]
        for qi, (reward, items) in enumerate(quests_raw):
            if not (completed_mask_det & (1 << qi)):
                for r_idx, cur, target in items:
                    rem_need = max(0, target - (cur + det_gains[r_idx]))
                    if rem_need > 0:
                        need[r_idx] += reward * (rem_need / max(1, target))

        step_weights = ((1, 1.2), (2, 1.0), (3, 0.8)) if c_max <= 3 else ((1, 1.2), (2, 1.1), (3, 0.9), (4, 0.7), (5, 0.5))
        # 偶数回合末(×2区下回合持续存在)且未弃牌时，提高对前方持续×2格的卡位权重，引导下回合起手直接踩×2打连击
        persistent_x2_turn = (not next_turn_resets_field) and bool(s_meta.get("has_x2")) and (not is_early_end)
        for step, sw in step_weights:
            t_r, t_m, t_lock = tiles_info[(end_pos + step) % 10]
            # 若下回合初会重洗 ×2 / 封锁，则不迷信当前即将消失的 ×2 也不躲即将解除的封锁
            eff_m = 1 if (next_turn_resets_field and s_meta.get("has_x2")) else t_m
            eff_lock = False if (next_turn_resets_field and s_meta.get("has_lock")) else t_lock
            if not eff_lock:
                # 基础极小权重(<=0.03)作同分打破器；偶数回合持续×2格额外给予显著卡位加权
                pos_bonus += 0.002 * sw * eff_m * need[t_r]
                if persistent_x2_turn and eff_m > 1:
                    pos_bonus += 0.018 * sw * (need[t_r] + 1.2)

    # 最后一回合时：纯冲刺确定/期望重建值 + 提前完成旧单后剩余手牌斩杀新单的期望
    if rem_turns <= 0:
        total_score = (
            exp_pts * 100.0
            + exp_lt_ov * 18.0
            + earliest_comp_rem_cards * 0.2
            + (det_gains[0] + det_gains[1] + det_gains[2]) * 1e-4
        )
    else:
        total_score = exp_pts + exp_prog + lap_val + early_comp_bonus + pos_bonus

    return total_score, exp_pts, guaranteed_pts, exp_prog, lap_val


def solve_turn(state: GameState, top_k: int = 3, max_time_sec: float = 2.0) -> Tuple[List[PlanResult], dict]:
    """
    计算本回合（或回合中途剩余手牌）的最佳行动策略（返回 Top K 方案 + 搜索统计）。
    """
    t0 = time.perf_counter()
    res_types = list(state.res_types)
    res_to_idx = {r: i for i, r in enumerate(res_types)}
    res_set_is_late = ("flame" in res_to_idx or "steel" in res_to_idx)
    rem_turns = max(0, state.max_turns - state.cur_turn)
    # 全部2回合刷新型场地效果(×2 / 重随 / 封锁 / 宝箱)均在偶数回合初刷新 -> 奇数回合末下回合必刷新
    next_turn_resets_field = ((state.cur_turn % 2) == 1)

    # 预编译地图数据
    tiles_info: List[Tuple[int, int, bool]] = []
    init_chest_mask = 0
    for t in state.tiles:
        r_idx = res_to_idx.get(t.res, 0)
        tiles_info.append((r_idx, t.mult, t.locked))
        if t.chest:
            init_chest_mask |= (1 << t.idx)

    # 预编译计划数据
    quests_raw: List[Tuple[int, List[Tuple[int, int, int]]]] = []
    for q in state.quests:
        items = []
        for it in q.items:
            if it.res in res_to_idx and it.target > 0:
                items.append((res_to_idx[it.res], it.cur, it.target))
        quests_raw.append((q.reward, items))

    # 手牌转为多重集 counts[1..5]
    init_counts = [0] * 6
    for c in state.hand:
        if 1 <= c <= 5:
            init_counts[c] += 1
    total_cards = sum(init_counts)

    if total_cards == 0:
        return [], {"nodes": 0, "elapsed_ms": 0.0}

    # 预计算计划初始完成掩码
    def calc_comp_mask(g0: int, g1: int, g2: int) -> int:
        m = 0
        g = (g0, g1, g2)
        for qi, (_, items) in enumerate(quests_raw):
            if not items:
                continue
            if all(cur + g[r_idx] >= target for r_idx, cur, target in items):
                m |= (1 << qi)
        return m

    init_comp_mask = calc_comp_mask(0, 0, 0)

    # 搜索状态与剪枝表
    # 对同一 (pos, can_collect, last_collect, hand_tuple, chest_mask, base_yield)
    # 维护 Pareto 非支配前沿，将 ×4 大单溢出(ov0) 与 ×1 小单溢出(ov1+ov2) 独立比较，防止小单溢出误剪大单溢出
    pareto_memo: Dict[tuple, List[Tuple[int, int, int, int, int, int]]] = {}
    # 记录全局候选方案: key = action_signature (避免同一结果刷屏)
    best_plans: List[Tuple[float, tuple]] = []
    nodes_visited = 0
    pruned_count = 0
    timed_out = False

    # 动作序列编码: (action_type, card_val), action_type: 0=advance, 1=collect, 2=combo
    path: List[Tuple[int, int]] = []

    def is_dominated(key: tuple, g0: int, g1: int, g2: int, ov_big: int, ov_small: int, comp_bonus: int) -> bool:
        front = pareto_memo.get(key)
        if front is None:
            pareto_memo[key] = [(g0, g1, g2, ov_big, ov_small, comp_bonus)]
            return False
        for fg0, fg1, fg2, fob, fos, fcb in front:
            if fg0 >= g0 and fg1 >= g1 and fg2 >= g2 and fob >= ov_big and fos >= ov_small and fcb >= comp_bonus:
                return True
        # 移除被新点支配的旧点
        new_front = [
            pt for pt in front
            if not (g0 >= pt[0] and g1 >= pt[1] and g2 >= pt[2] and ov_big >= pt[3] and ov_small >= pt[4] and comp_bonus >= pt[5])
        ]
        if len(new_front) < 24:
            new_front.append((g0, g1, g2, ov_big, ov_small, comp_bonus))
        pareto_memo[key] = new_front
        return False

    def dfs(
        pos: int,
        can_col: bool,
        last_col: int,  # 0 表示 None, 1..5 表示上次收集牌点数
        c1: int, c2: int, c3: int, c4: int, c5: int,
        chest_mask: int,
        base_y: int,
        g0: int, g1: int, g2: int,
        ov0: int, ov1: int, ov2: int,
        comp_mask: int,
        comp_rem_sum: int,
        chests_cnt: int,
        laps_cnt: int,
        prev_was_plain_adv_val: int,  # 对称性剪枝: 连续无事件前进时强制非递减顺序
    ):
        nonlocal nodes_visited, pruned_count, timed_out
        nodes_visited += 1
        if (nodes_visited & 4095) == 0:
            if time.perf_counter() - t0 > max_time_sec:
                timed_out = True
                return
        if timed_out:
            return

        rem_cards = c1 + c2 + c3 + c4 + c5

        # 任意时刻均可直接结束回合（放弃剩余未使用的手牌以保留当前站位或节省末回合无效空走）
        def record_terminal(is_early_end: bool):
            sc, exp_pts, guar_pts, prog_v, lap_v = _evaluate_terminal(
                res_types=res_types,
                quests_raw=quests_raw,
                det_gains=(g0, g1, g2),
                slot_overflow=(ov0, ov1, ov2),
                completed_mask_det=comp_mask,
                earliest_comp_rem_cards=comp_rem_sum,
                chests_count=chests_cnt,
                laps_count=laps_cnt,
                end_pos=pos,
                end_base_y=base_y,
                rem_turns=rem_turns,
                cur_pts=state.cur_pts,
                stage_id=state.stage_id,
                lap_bonus=state.lap_bonus,
                hand_scale=state.hand_scale,
                res_set_is_late=res_set_is_late,
                tiles_info=tiles_info,
                chest_amount=state.chest_amount,
                next_turn_resets_field=next_turn_resets_field,
                is_early_end=is_early_end,
            )
            # 末回合完全平分时微弱偏向提前结束(省去无收益空走动画)
            if is_early_end and rem_turns <= 0:
                sc += 1e-5
            cap_n = max(top_k * 5, 15)
            if len(best_plans) < cap_n or sc > best_plans[-1][0]:
                seq = list(path)
                if is_early_end:
                    seq.append((3, rem_cards))
                best_plans.append((
                    sc,
                    (seq, exp_pts, guar_pts, prog_v, lap_v, (g0, g1, g2), chests_cnt, laps_cnt, pos, base_y),
                ))
                best_plans.sort(key=lambda x: x[0], reverse=True)
                if len(best_plans) > cap_n:
                    best_plans.pop()

        if rem_cards == 0:
            record_terminal(is_early_end=False)
            return

        # 也允许直接放弃剩余手牌提前结束回合（例如卡住黄金起手位，或末回合跳过无用空走）
        record_terminal(is_early_end=True)

        # 状态支配剪枝
        mkey = (pos, can_col, last_col, c1, c2, c3, c4, c5, chest_mask, base_y)
        if is_dominated(mkey, g0, g1, g2, ov0, ov1 + ov2, comp_rem_sum):
            pruned_count += 1
            return

        counts = (0, c1, c2, c3, c4, c5)
        t_res, t_mult, t_locked = tiles_info[pos]

        # 动作 1: 收集资源 (优先探索，有助于尽早找到高分路径)
        if not t_locked:
            if can_col:
                # 首次收集：任意手牌都可以收集（从大点数到小点数，或小到大顺子起点）
                for v in (5, 4, 3, 2, 1):
                    if counts[v] == 0:
                        continue
                    gain = v * base_y * t_mult
                    ng0 = g0 + (gain if t_res == 0 else 0)
                    ng1 = g1 + (gain if t_res == 1 else 0)
                    ng2 = g2 + (gain if t_res == 2 else 0)
                    # 按 3 个槽位各自独立追踪完成后的后续溢出量（仅对已经完成的槽位累加）
                    nov0 = ov0 + (gain if (comp_mask & 1) else 0)
                    nov1 = ov1 + (gain if (comp_mask & 2) else 0)
                    nov2 = ov2 + (gain if (comp_mask & 4) else 0)
                    ncm = calc_comp_mask(ng0, ng1, ng2)
                    newly = ncm & ~comp_mask
                    ncrs = comp_rem_sum
                    if newly:
                        # 按每条新完成计划的权重加权剩余牌数
                        for qi in range(len(quests_raw)):
                            if newly & (1 << qi):
                                ncrs += (rem_cards - 1) * quests_raw[qi][0]
                    path.append((1, v))
                    dfs(
                        pos, False, v,
                        c1 - (v == 1), c2 - (v == 2), c3 - (v == 3), c4 - (v == 4), c5 - (v == 5),
                        chest_mask, base_y,
                        ng0, ng1, ng2, nov0, nov1, nov2,
                        ncm, ncrs, chests_cnt, laps_cnt, 0,
                    )
                    path.pop()
            elif last_col > 0:
                # 连续收集 COMBO+：严格只能用 last_col + 1
                v = last_col + 1
                if v <= 5 and counts[v] > 0:
                    gain = v * base_y * t_mult
                    dg0 = COMBO_BONUS + (gain if t_res == 0 else 0)
                    dg1 = COMBO_BONUS + (gain if t_res == 1 else 0)
                    dg2 = COMBO_BONUS + (gain if t_res == 2 else 0)
                    ng0, ng1, ng2 = g0 + dg0, g1 + dg1, g2 + dg2
                    step_tot = dg0 + dg1 + dg2
                    nov0 = ov0 + (step_tot if (comp_mask & 1) else 0)
                    nov1 = ov1 + (step_tot if (comp_mask & 2) else 0)
                    nov2 = ov2 + (step_tot if (comp_mask & 4) else 0)
                    ncm = calc_comp_mask(ng0, ng1, ng2)
                    newly = ncm & ~comp_mask
                    ncrs = comp_rem_sum
                    if newly:
                        for qi in range(len(quests_raw)):
                            if newly & (1 << qi):
                                ncrs += (rem_cards - 1) * quests_raw[qi][0]
                    path.append((2, v))
                    dfs(
                        pos, False, v,
                        c1 - (v == 1), c2 - (v == 2), c3 - (v == 3), c4 - (v == 4), c5 - (v == 5),
                        chest_mask, base_y,
                        ng0, ng1, ng2, nov0, nov1, nov2,
                        ncm, ncrs, chests_cnt, laps_cnt, 0,
                    )
                    path.pop()

        # 动作 2: 前进 (Advance)
        # 若还剩最后1张牌且不能采集，如果是最后1回合且不触发宝箱，任意选一张前进即可避免无意义分支
        for v in (1, 2, 3, 4, 5):
            if counts[v] == 0:
                continue
            # 检查路径经过的宝箱(含起始格若当前格尚有未领宝箱，以及途经和终点格)
            hit_mask = 0
            if chest_mask:
                for step in range(0, v + 1):
                    bit = 1 << ((pos + step) % 10)
                    if chest_mask & bit:
                        hit_mask |= bit
            hit_cnt = hit_mask.bit_count() if hit_mask else 0
            # 对称性剪枝：连续两次不触宝箱的纯前进，强迫点数非递减 (a <= b)，不会丢任何落点/过圈状态
            if prev_was_plain_adv_val > 0 and hit_cnt == 0 and v < prev_was_plain_adv_val:
                continue

            raw_pos = pos + v
            crossed = raw_pos // 10
            npos = raw_pos % 10
            nbase = base_y + crossed * state.lap_bonus
            nchest_mask = chest_mask ^ hit_mask
            next_plain = v if (hit_cnt == 0 and not can_col and last_col == 0) else (v if hit_cnt == 0 else 0)
            # 只有上一步本来就是纯前进（且本次无宝箱）才延续对称剪枝标志
            # 更安全：只有当本步前进后直接紧接着再前进时才用 prev_was_plain_adv_val=v
            path.append((0, v))
            dfs(
                npos, True, 0,
                c1 - (v == 1), c2 - (v == 2), c3 - (v == 3), c4 - (v == 4), c5 - (v == 5),
                nchest_mask, nbase,
                g0, g1, g2, ov0, ov1, ov2,
                comp_mask, comp_rem_sum,
                chests_cnt + hit_cnt,
                laps_cnt + crossed,
                v if hit_cnt == 0 else 0,
            )
            path.pop()

    dfs(
        state.pos,
        state.can_collect,
        state.last_collect or 0,
        init_counts[1], init_counts[2], init_counts[3], init_counts[4], init_counts[5],
        init_chest_mask,
        state.base_yield,
        0, 0, 0, 0, 0, 0,
        init_comp_mask,
        0, 0, 0, 0,
    )

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # 将最佳动作序列重演，生成人类可读的详细 StepDetail
    results: List[PlanResult] = []
    seen_sigs = set()

    for sc, payload in best_plans:
        act_seq, exp_pts, guar_pts, prog_v, lap_v, det_g, ch_cnt, lp_cnt, end_pos, end_base = payload
        sig = tuple(act_seq)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)

        steps: List[StepDetail] = []
        cur_pos = state.pos
        cur_base = state.base_yield
        cur_chest_mask = init_chest_mask
        acc = [0, 0, 0]
        comp_done = [False] * len(quests_raw)
        for qi, (_, items) in enumerate(quests_raw):
            if items and all(cur >= target for _, cur, target in items):
                comp_done[qi] = True

        first_resync = None
        for idx_s, (atype, v) in enumerate(act_seq, 1):
            gained_dict = {r: 0 for r in res_types}
            chests_hit: List[int] = []
            laps_crossed = 0
            from_p = cur_pos
            if atype == 3:
                action_str = "end_turn"
                note = f"提前结束回合：放弃剩余 {v} 张手牌，保留当前 {cur_pos}号格站位直接进入下一回合"
            elif atype == 0:
                action_str = "advance"
                if cur_chest_mask:
                    for st in range(0, v + 1):
                        gp = (cur_pos + st) % 10
                        if cur_chest_mask & (1 << gp):
                            chests_hit.append(gp)
                            cur_chest_mask ^= (1 << gp)
                raw_p = cur_pos + v
                laps_crossed = raw_p // 10
                cur_pos = raw_p % 10
                cur_base += laps_crossed * state.lap_bonus
                tile_r = state.tiles[cur_pos].res
                note_parts = [f"用 [{v}] 前进：{from_p}号格 → {cur_pos}号格（{RESOURCE_LABELS.get(tile_r, tile_r)}"]
                if state.tiles[cur_pos].mult > 1:
                    note_parts[-1] += f" ×{state.tiles[cur_pos].mult}"
                if state.tiles[cur_pos].locked:
                    note_parts[-1] += " 已封锁"
                note_parts[-1] += "）"
                if laps_crossed > 0 and state.lap_bonus > 0:
                    note_parts.append(f"过圈！全图基础产出 +{laps_crossed * state.lap_bonus} → {cur_base}")
                if chests_hit:
                    note_parts.append(f"触发宝箱({','.join(str(x)+'号' for x in chests_hit)})，每个随机资源+{state.chest_amount}")
                note = "；".join(note_parts)
            else:
                t = state.tiles[cur_pos]
                tile_r = t.res
                r_idx = res_to_idx[tile_r]
                base_gain = v * cur_base * t.mult
                if atype == 1:
                    action_str = "collect"
                    acc[r_idx] += base_gain
                    gained_dict[tile_r] += base_gain
                    note = f"用 [{v}] 收集：{cur_pos}号格 {RESOURCE_LABELS.get(tile_r, tile_r)} +{base_gain}（{v}×{cur_base}" + (f"×{t.mult}" if t.mult > 1 else "") + "）"
                else:
                    action_str = "combo"
                    for ri, rn in enumerate(res_types):
                        dg = COMBO_BONUS + (base_gain if ri == r_idx else 0)
                        acc[ri] += dg
                        gained_dict[rn] += dg
                    note = f"用 [{v}] 触发 COMBO+ 收集：{RESOURCE_LABELS.get(tile_r, tile_r)} +{base_gain + COMBO_BONUS}，其余全资源 +{COMBO_BONUS}"

            # 检查本步新完成的计划
            comp_this: List[int] = []
            pts_this = 0
            for qi, (reward, items) in enumerate(quests_raw):
                if not comp_done[qi] and items:
                    if all(cur + acc[ri] >= target for ri, cur, target in items):
                        comp_done[qi] = True
                        comp_this.append(qi)
                        pts_this += reward
            if comp_this:
                q_str = "、".join(f"计划{qi+1}(+{quests_raw[qi][0]}重建值)" for qi in comp_this)
                note += f" ★完成 {q_str}！"

            needs_resync = bool(comp_this or chests_hit)
            if needs_resync and first_resync is None and idx_s < len(act_seq):
                first_resync = idx_s

            steps.append(StepDetail(
                step_no=idx_s,
                action=action_str,
                card=v,
                from_pos=from_p,
                to_pos=cur_pos,
                tile_res=state.tiles[cur_pos].res,
                gained=gained_dict,
                chests_hit=chests_hit,
                laps_crossed=laps_crossed,
                new_base_yield=cur_base,
                completed_quests=comp_this,
                pts_gained=pts_this,
                needs_resync=needs_resync,
                note=note,
            ))

        # 汇总终态计划进度
        final_quests = []
        for qi, (reward, items) in enumerate(quests_raw):
            q_items = []
            done = bool(items)
            for ri, cur, target in items:
                nv = min(target, cur + acc[ri])
                if nv < target:
                    done = False
                q_items.append({
                    "res": res_types[ri],
                    "label": RESOURCE_LABELS.get(res_types[ri], res_types[ri]),
                    "before": cur,
                    "after": nv,
                    "target": target,
                })
            final_quests.append({
                "index": qi,
                "reward": reward,
                "completed": done,
                "items": q_items,
            })

        results.append(PlanResult(
            rank=len(results) + 1,
            score=round(sc, 3),
            expected_pts=round(exp_pts, 2),
            guaranteed_pts=guar_pts,
            progress_value=round(prog_v, 2),
            lap_value=round(lap_v, 2),
            total_gained={res_types[i]: det_g[i] for i in range(3)},
            chests_count=ch_cnt,
            laps_count=lp_cnt,
            end_pos=end_pos,
            end_base_yield=end_base,
            final_quests=final_quests,
            steps=steps,
            first_resync_step=first_resync,
        ))
        if len(results) >= top_k:
            break

    stats = {
        "nodes": nodes_visited,
        "pruned": pruned_count,
        "elapsed_ms": round(elapsed_ms, 2),
        "timed_out": timed_out,
    }
    return results, stats


if __name__ == "__main__":
    # 基准测试: 模拟第10关 5张牌 & 第9关 8张牌
    tiles10 = [
        TileState(0, "crystal"), TileState(1, "steel"), TileState(2, "flame"),
        TileState(3, "crystal", chest=True), TileState(4, "steel", chest=True),
        TileState(5, "flame"), TileState(6, "crystal"), TileState(7, "steel"),
        TileState(8, "flame", chest=True), TileState(9, "flame"),
    ]
    st10 = GameState(
        stage_id=10,
        res_types=["crystal", "flame", "steel"],
        tiles=tiles10,
        pos=8,
        base_yield=3,
        hand=[1, 1, 2, 3, 3],
        quests=[
            QuestState(4, [QuestItem("flame", 17, 70), QuestItem("crystal", 2, 70)]),
            QuestState(1, [QuestItem("crystal", 2, 20), QuestItem("steel", 2, 20)]),
            QuestState(1, [QuestItem("steel", 2, 35)]),
        ],
        cur_turn=2, max_turns=10, cur_pts=0, target_pts=12, lap_bonus=2,
    )
    res, st = solve_turn(st10, top_k=3)
    print("Stage 10 (5 cards):", st)
    for p in res:
        print(f"  Rank {p.rank}: score={p.score}, exp_pts={p.expected_pts}, guar={p.guaranteed_pts}, laps={p.laps_count}, chests={p.chests_count}, gained={p.total_gained}")
        for s in p.steps:
            print("    ", s.step_no, s.note)

    # 8张牌压测 (第9关)
    st9 = GameState(
        stage_id=9,
        res_types=["crystal", "flame", "steel"],
        tiles=[
            TileState(0, "crystal", locked=True), TileState(1, "steel"), TileState(2, "flame"),
            TileState(3, "crystal"), TileState(4, "steel", locked=True), TileState(5, "flame"),
            TileState(6, "crystal", locked=True), TileState(7, "steel"), TileState(8, "flame", locked=True),
            TileState(9, "flame"),
        ],
        pos=0, base_yield=4, hand=[1, 2, 2, 3, 4, 4, 5, 5],
        quests=[
            QuestState(4, [QuestItem("flame", 10, 70), QuestItem("steel", 10, 70)]),
            QuestState(1, [QuestItem("crystal", 0, 20), QuestItem("steel", 0, 20)]),
            QuestState(1, [QuestItem("flame", 0, 25)]),
        ],
        cur_turn=5, max_turns=10, cur_pts=6, target_pts=12, hand_scale=True,
    )
    res9, st9_stat = solve_turn(st9, top_k=3)
    print("\nStage 9 (8 cards stress test):", st9_stat)
    if res9:
        p = res9[0]
        print(f"  Rank 1: score={p.score}, exp_pts={p.expected_pts}, guar={p.guaranteed_pts}, gained={p.total_gained}")
        for s in p.steps:
            print("    ", s.step_no, s.note)
