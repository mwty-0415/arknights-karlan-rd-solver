# -*- coding: utf-8 -*-
"""
明日方舟「雪山降临1101」小游戏「喀兰贸易技术研发部」
本地 Web 策略助手后端 (Flask)
"""

from __future__ import annotations
import base64
import ctypes
import json
import os
import re
import sys
import time
import threading
import webbrowser
try:
    import winsound
except Exception:
    winsound = None
from dataclasses import asdict
from typing import Any, Dict, List, Optional
import cv2
from flask import Flask, jsonify, render_template, request

from solver import (
    GameState,
    TileState,
    QuestState,
    QuestItem,
    RESOURCE_LABELS,
    RESOURCE_SETS,
    STAGES,
    solve_turn,
)
from vision import (
    AdbClient,
    BoardRecognizer,
    create_client,
    find_adb_path,
    is_process_admin,
    list_pc_windows,
)
from obs_ctrl import ObsController, finalize_recording_file

BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)

app = Flask(__name__, template_folder=os.path.join(BUNDLE_DIR, "templates"))

# 全局单例（懒加载 OCR，避免阻塞模块导入）
_recognizer_lock = threading.Lock()
_recognizer: Optional[BoardRecognizer] = None
_last_card_infos: List[dict] = []
_last_ctrl_mode: str = "adb"  # "adb" | "pc"
_last_adb_device: str = "127.0.0.1:16384"
_last_adb_path: str = find_adb_path()
_last_pc_window: str = "明日方舟"


SCORE_LOG_PATH = os.path.join(RUNTIME_DIR, "score_history.jsonl")

# 各关默认冲榜推荐配置（超越标杆纪录的目标分 + 中盘等效分剪枝检查回合/门槛）
# 注：剪枝判断使用「等效分 = 已结算分 + 未完成任务进度折算分」，避免刚交大单或差临门一脚被误杀
DEFAULT_RUSH_PRESETS: Dict[int, dict] = {
    1:  {"target_score": 24, "prune_turn": 3, "prune_min_pts": 9},
    2:  {"target_score": 23, "prune_turn": 4, "prune_min_pts": 9},
    3:  {"target_score": 22, "prune_turn": 4, "prune_min_pts": 9},
    4:  {"target_score": 24, "prune_turn": 4, "prune_min_pts": 10},
    5:  {"target_score": 22, "prune_turn": 4, "prune_min_pts": 8},
    6:  {"target_score": 21, "prune_turn": 4, "prune_min_pts": 8},
    7:  {"target_score": 22, "prune_turn": 4, "prune_min_pts": 8},
    8:  {"target_score": 29, "prune_turn": 4, "prune_min_pts": 9},
    9:  {"target_score": 29, "prune_turn": 4, "prune_min_pts": 9},
    10: {"target_score": 25, "prune_turn": 5, "prune_min_pts": 10},
}


def _calc_effective_pts(state: GameState) -> float:
    """计算当前盘面等效分 = 已结算重建值 + 3 条未完成任务的进度折算分。"""
    prog = 0.0
    for q in state.quests:
        if not q.items:
            continue
        sum_t = sum(it.target for it in q.items)
        if sum_t > 0:
            sum_c = sum(min(it.cur, it.target) for it in q.items)
            prog += q.reward * (sum_c / sum_t)
    return round(state.cur_pts + prog, 1)


def _click_end_turn_with_confirm(
    client,
    rec: BoardRecognizer,
    end_xy: tuple,
    is_last: bool,
    allow_abandon_cards: bool = False,
) -> bool:
    """
    点击「结束回合」并闭环处理确认弹窗：
    - allow_abandon_cards=True（求解器主动要求提前弃牌结束回合）：直接点右侧红色「确认放弃」（不勾选不再提示，保留游戏防误弃安全网），返回 True。
    - allow_abandon_cards=False（认为手牌已空）：若游戏意外弹出「还有手牌未用」确认框，立刻点左侧灰色「取消」(320, cy) 并返回 False，防止浪费未选上的手牌！
    """
    client.tap(end_xy[0], end_xy[1], wait=0.95)
    handled_dialog = False
    for _ in range(3):
        chk = client.screencap_bgr()
        has_red, confirm_xy = rec.detect_confirm_red_dialog(chk)
        if not has_red and not rec.detect_abandon_turn_dialog(chk):
            break
        handled_dialog = True
        if not allow_abandon_cards:
            # 意外弹窗说明仍有剩余手牌：点左侧「取消」栏退回局内继续出牌
            client.tap(320, confirm_xy[1], wait=0.75)
        else:
            # 求解器主动弃牌：直接点右侧红色「确认放弃」（不勾选不再提示，让后续回合仍受防误弃保护）
            client.tap(confirm_xy[0], confirm_xy[1], wait=1.05)

    if handled_dialog and not allow_abandon_cards:
        time.sleep(0.35)
        return False

    if handled_dialog:
        time.sleep(2.3 if is_last else 1.5)
    else:
        time.sleep(2.45 if is_last else 1.55)
    return True


def _abandon_current_run(client, rec: BoardRecognizer) -> bool:
    """
    中盘低分剪枝重开：暂离 -> 放弃 -> 确认放弃（红条闭环确认）-> 跳过结算回到选关主界面。
    支持 PC 端与模拟器自适应坐标。
    """
    # 1. 点击左上角「暂离」(80, 46)
    client.tap(80, 46, wait=1.35)
    img1 = client.screencap_bgr()

    # 2. 在选关暂离页动态定位「放弃」按钮，兜底 (1125, 450)
    ab_xy = (1125, 450)
    for (cx, cy), txt in rec._ocr_text(img1[360:540, 1020:1240], scale=1.5):
        if "放弃" in txt:
            ab_xy = (cx + 1020, cy + 360)
            break
    client.tap(ab_xy[0], ab_xy[1], wait=1.05)

    # 3. 闭环点击红色「确认放弃」直到确认弹窗消失
    for _ in range(3):
        img2 = client.screencap_bgr()
        has_red, cf_xy = rec.detect_confirm_red_dialog(img2)
        if not has_red:
            break
        client.tap(cf_xy[0], cf_xy[1], wait=1.4)

    # 4. 点击跳过结算画面回到选关主界面
    for _ in range(5):
        chk = client.screencap_bgr()
        if rec.detect_stage_select_screen(chk) is not None:
            return True
        client.tap(640, 360, wait=1.3)
    return rec.detect_stage_select_screen(client.screencap_bgr()) is not None


class AutoRunner:
    """后台全自动通关 / 单关反复冲分 / 多关连刷冲榜状态机（支持中盘剪枝 & OBS 每关独立留最高分）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.running = False
        self.phase = "idle"  # idle | running | finished | stopped | error
        self.stage_id: Optional[int] = None
        self.max_runs: int = 1        # 当前关目标盘数: 1=单局, 0=无限循环, N=打N盘
        self.target_score: int = 0    # 当前关达标停机分 (>0生效)
        self.runs_done: int = 0       # 当前关已完成完整盘数
        self.pruned_runs: int = 0     # 当前关中盘剪枝重开盘数
        self.total_runs_done: int = 0 # 本次任务总完成盘数
        self.best_pts: int = 0        # 当前关本次最高分
        self.best_video_path: str = ""
        self.saved_videos: List[dict] = []  # 多关连刷时每关珍藏的高光录像
        self.queue_info: List[dict] = []    # 多关队列进度展示
        self.queue_idx: int = 0
        self.obs_enabled: bool = False
        self.history: List[dict] = []
        self.logs: List[str] = []
        self.last_state: Optional[dict] = None
        self.last_plans: List[dict] = []
        self.last_stats: Optional[dict] = None
        self.last_preview: str = ""
        self.result_text: str = ""
        self.error_msg: str = ""

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{ts}] {msg}")
            if len(self.logs) > 150:
                self.logs = self.logs[-150:]

    def start(
        self,
        ctrl_mode: str,
        device: str,
        adb_path: str,
        window_title: str,
        stage_hint: Optional[int],
        max_runs: int = 1,
        target_score: int = 0,
        obs_cfg: Optional[dict] = None,
        prune_cfg: Optional[dict] = None,
        stage_queue: Optional[List[dict]] = None,
    ):
        with self.lock:
            if self.running:
                return False
            self.stop_event.clear()
            self.running = True
            self.phase = "running"
            self.stage_id = stage_hint
            self.max_runs = max(0, int(max_runs))
            self.target_score = max(0, int(target_score))
            self.runs_done = 0
            self.pruned_runs = 0
            self.total_runs_done = 0
            self.best_pts = 0
            self.best_video_path = ""
            self.saved_videos = []
            self.queue_info = []
            self.queue_idx = 0
            self.obs_enabled = bool(obs_cfg and obs_cfg.get("enabled"))
            self.history = []
            self.logs = []
            self.result_text = ""
            self.error_msg = ""

            # 规范化任务队列：单关模式视为长度为1的队列，多关连刷视为长度N的队列
            tasks: List[dict] = []
            if stage_queue and len(stage_queue) > 0:
                for item in stage_queue:
                    sid = int(item.get("stage_id", 1))
                    if sid in STAGES:
                        preset = DEFAULT_RUSH_PRESETS.get(sid, {"target_score": 0, "prune_turn": 4, "prune_min_pts": 8})
                        tasks.append({
                            "stage_id": sid,
                            "max_runs": max(0, int(item.get("max_runs", 0) or 0)),
                            "target_score": max(0, int(item.get("target_score", preset["target_score"]) or 0)),
                            "prune_enabled": bool(item.get("prune_enabled", False)),
                            "prune_turn": max(1, int(item.get("prune_turn", preset["prune_turn"]) or preset["prune_turn"])),
                            "prune_min_pts": max(0, int(item.get("prune_min_pts", preset["prune_min_pts"]) or 0)),
                            "status": "pending",
                            "best_pts": 0,
                            "runs": 0,
                        })
            if not tasks:
                p_cfg = prune_cfg or {}
                tasks.append({
                    "stage_id": stage_hint,  # None 表示自动识别当前关
                    "max_runs": self.max_runs,
                    "target_score": self.target_score,
                    "prune_enabled": bool(p_cfg.get("enabled", False)),
                    "prune_turn": max(1, int(p_cfg.get("turn", 4) or 4)),
                    "prune_min_pts": max(0, int(p_cfg.get("min_pts", 8) or 0)),
                    "status": "pending",
                    "best_pts": 0,
                    "runs": 0,
                })

            self.queue_info = tasks
            self.thread = threading.Thread(
                target=self._run_queue,
                args=(ctrl_mode, device, adb_path, window_title, tasks, obs_cfg or {}),
                daemon=True,
            )
            self.thread.start()
            return True

    def stop(self, reason: str = ""):
        if not self.stop_event.is_set():
            self.stop_event.set()
            msg = f"收到停止指令（{reason}），立即安全停止..." if reason else "收到停止指令，将在当前动作结束后安全停止..."
            self._log(msg)
            if winsound is not None:
                try:
                    winsound.Beep(880, 140)
                except Exception:
                    pass

    def snapshot(self) -> dict:
        with self.lock:
            avg_pts = round(sum(h["pts"] for h in self.history) / len(self.history), 2) if self.history else 0.0
            return {
                "running": self.running,
                "phase": self.phase,
                "stage_id": self.stage_id,
                "max_runs": self.max_runs,
                "target_score": self.target_score,
                "runs_done": self.runs_done,
                "pruned_runs": self.pruned_runs,
                "total_runs_done": self.total_runs_done,
                "best_pts": self.best_pts,
                "best_video_path": self.best_video_path,
                "saved_videos": list(self.saved_videos),
                "queue_info": [dict(q) for q in self.queue_info],
                "queue_idx": self.queue_idx,
                "obs_enabled": self.obs_enabled,
                "avg_pts": avg_pts,
                "history": list(self.history[-40:]),
                "logs": list(self.logs),
                "state": self.last_state,
                "plans": list(self.last_plans),
                "stats": self.last_stats,
                "preview": self.last_preview,
                "result_text": self.result_text,
                "error": self.error_msg,
            }

    def _update_view(self, state_dict, plans_list, stats, img_bgr):
        prev = _encode_preview_jpg(img_bgr)
        with self.lock:
            if state_dict is not None:
                self.last_state = state_dict
                self.stage_id = state_dict.get("stage_id", self.stage_id)
            if plans_list is not None:
                self.last_plans = plans_list
            if stats is not None:
                self.last_stats = stats
            if prev:
                self.last_preview = prev

    def _record_run_result(self, stage_id: int, pts: int, target_pts: int, res_txt: str):
        eval_tag = "春日必然到来" if ("春日" in res_txt or "SPRING" in res_txt.upper() or pts * 100 >= target_pts * 175) else (
            "雪境生存哲学" if pts >= target_pts else "未达标"
        )
        is_new_best = False
        with self.lock:
            self.runs_done += 1
            self.total_runs_done += 1
            rec_item = {
                "run": self.total_runs_done,
                "stage_run": self.runs_done,
                "stage_id": stage_id,
                "stage_name": STAGES.get(stage_id, {}).get("name", f"第{stage_id}关"),
                "pts": pts,
                "target": target_pts,
                "eval": eval_tag,
                "time": time.strftime("%H:%M:%S"),
            }
            self.history.append(rec_item)
            if pts > self.best_pts:
                self.best_pts = pts
                is_new_best = True
            if 0 <= self.queue_idx < len(self.queue_info):
                self.queue_info[self.queue_idx]["runs"] = self.runs_done
                self.queue_info[self.queue_idx]["best_pts"] = self.best_pts
        try:
            with open(SCORE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"date": time.strftime("%Y-%m-%d"), **rec_item}, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec_item, is_new_best

    def _parse_result_pts(self, img_bgr, rec: BoardRecognizer, res_txt: str, fallback_pts: int, target_pts: int) -> int:
        # 优先采用结算画面中央大号红色真值(如 15/4 -> 15)，绝不被局内偶发 OCR 噪点污染
        exact = rec.parse_result_score(img_bgr, target_pts, res_txt)
        if exact is not None:
            return exact
        return fallback_pts

    def _run_queue(
        self,
        ctrl_mode: str,
        device: str,
        adb_path: str,
        window_title: str,
        tasks: List[dict],
        obs_cfg: dict,
    ):
        obs: Optional[ObsController] = None
        obs_recording_by_us = False
        obs_min_keep_global = max(0, int(obs_cfg.get("min_keep_score", 0) or 0))
        obs_keep_only_best = bool(obs_cfg.get("keep_only_best", True))
        cur_locked_stage: Optional[int] = None

        try:
            adb = create_client(mode=ctrl_mode, adb_path=adb_path, device=device, window_title=window_title)
            rec = get_recognizer()
            if ctrl_mode == "pc":
                if not is_process_admin():
                    self._log("⚠️ 提示：当前未以管理员身份运行；若 PC 端无点击反应，请用 start_admin.bat 启动！")
                self._log(f"🖥️ PC 端模式（窗口：{window_title}），2.5 秒后切到游戏窗口接管，请勿遮挡游戏画面...")
                time.sleep(2.5)
                if self.stop_event.is_set():
                    with self.lock:
                        self.phase = "stopped"
                    return
                adb.ensure_foreground()

            if obs_cfg.get("enabled"):
                obs = ObsController(
                    host=str(obs_cfg.get("host", "127.0.0.1")),
                    port=int(obs_cfg.get("port", 4455) or 4455),
                    password=str(obs_cfg.get("password", "")),
                )
                try:
                    info = obs.test_connection()
                    self._log(f"🎬 OBS 已连接 (v{info['obs_version']})，每关独立留存最高分高光录像！")
                except Exception as oe:
                    self._log(f"⚠️ OBS 连接失败，将关闭自动录制继续刷分: {oe}")
                    obs = None

            multi_mode = len(tasks) > 1
            if multi_mode:
                s_names = " → ".join(f"S{t['stage_id']:02d}(目标{t['target_score']})" for t in tasks)
                self._log(f"🏆 启动多关连刷冲榜队列（共 {len(tasks)} 关）：{s_names}")

            for t_idx, task in enumerate(tasks):
                if self.stop_event.is_set():
                    break

                is_last_task = (t_idx == len(tasks) - 1)
                locked_stage: Optional[int] = task["stage_id"]
                cur_locked_stage = locked_stage
                max_runs = task["max_runs"]
                target_score = task["target_score"]
                prune_enabled = task["prune_enabled"]
                prune_turn = task["prune_turn"]
                prune_min_pts = task["prune_min_pts"]

                # 每切换到一个新关卡，独立初始化该关的计数器和 OBS 留档状态（不覆盖上一关的最高分视频！）
                obs_min_keep = obs_min_keep_global if not multi_mode else 0
                obs_best_saved_pts = 0
                obs_best_saved_path: Optional[str] = None

                with self.lock:
                    self.queue_idx = t_idx
                    self.queue_info[t_idx]["status"] = "running"
                    self.stage_id = locked_stage
                    self.max_runs = max_runs
                    self.target_score = target_score
                    self.runs_done = 0
                    self.pruned_runs = 0
                    self.best_pts = 0

                def obs_try_start(run_no: int):
                    nonlocal obs_recording_by_us
                    if obs is None or obs_recording_by_us:
                        return
                    try:
                        started = obs.start_record()
                        obs_recording_by_us = True
                        self._log(f"🎬 OBS 开始录制第 {run_no} 盘" + ("" if started else "（沿用当前录制）"))
                    except Exception as oe:
                        self._log(f"⚠️ OBS 启动录制失败: {oe}")

                def obs_try_finish(keep: bool, sid: int, pts: int):
                    nonlocal obs_recording_by_us, obs_best_saved_pts, obs_best_saved_path
                    if obs is None or not obs_recording_by_us:
                        return
                    try:
                        out_path = obs.stop_record()
                        obs_recording_by_us = False
                        stage_name = STAGES.get(sid, {}).get("name", f"S{sid:02d}")
                        stem = f"S{sid:02d}_{stage_name}_{pts}分_{time.strftime('%Y%m%d_%H%M%S')}"
                        prev_to_del = obs_best_saved_path if (keep and obs_keep_only_best) else None
                        saved, note = finalize_recording_file(out_path, keep=keep, new_stem=stem, delete_prev_path=prev_to_del)
                        if keep and saved:
                            obs_best_saved_pts = pts
                            obs_best_saved_path = saved
                            with self.lock:
                                self.best_video_path = saved
                                # 更新该关在 saved_videos 里的最新最高分录像
                                self.saved_videos = [v for v in self.saved_videos if v["stage_id"] != sid]
                                self.saved_videos.append({"stage_id": sid, "stage_name": stage_name, "pts": pts, "path": saved})
                        self._log(f"🎬 OBS: {note}")
                    except Exception as oe:
                        obs_recording_by_us = False
                        self._log(f"⚠️ OBS 停止/处理录像失败: {oe}")

                mode_desc = "单局通关" if max_runs == 1 else (
                    "无限循环冲分" if max_runs == 0 else f"打 {max_runs} 盘"
                )
                if target_score > 0:
                    mode_desc += f" · 目标 ≥{target_score} 分"
                if prune_enabled and prune_min_pts > 0:
                    mode_desc += f" · ⚡剪枝(T{prune_turn}结束<{prune_min_pts}分重开)"
                st_label = f"第{locked_stage}关「{STAGES[locked_stage]['name']}」" if locked_stage in STAGES else "当前关"
                self._log(f"▶ 开始任务 [{t_idx + 1}/{len(tasks)}] {st_label}：{mode_desc}")

                can_collect_flag = False
                last_collect_val: Optional[int] = None
                prev_hand_len: Optional[int] = None
                last_known_yield: Optional[int] = None
                last_turn_pts = 0
                last_target_pts = STAGES[locked_stage]["target_pts"] if (locked_stage in STAGES) else 10
                tracked_turn: Optional[int] = None
                fail_streak = 0
                just_ended_last_turn = False
                run_cycles = 0

                while not self.stop_event.is_set():
                    run_cycles += 1
                    if run_cycles > 160:
                        raise RuntimeError("单局循环次数异常偏高，已安全中止")

                    img = adb.screencap_bgr()

                    # 0. 最优先兜底检测：若画面意外停在「确认放弃」弹窗，默认点左侧「取消」回到局内重新决策，绝不误弃手牌
                    has_red_dlg, red_xy = rec.detect_confirm_red_dialog(img)
                    if has_red_dlg:
                        self._log("检测到意外确认弹窗，点「取消」返回局内重新同步...")
                        adb.tap(320, red_xy[1], wait=0.9)
                        continue

                    # 1. 检测结算画面
                    res_txt = rec.detect_result_screen(img)
                    if res_txt is not None:
                        self._update_view(None, [], None, img)
                        sid = locked_stage or 1
                        tgt = STAGES.get(sid, {}).get("target_pts", last_target_pts)
                        final_pts = self._parse_result_pts(img, rec, res_txt, last_turn_pts, tgt)
                        rec_item, is_best = self._record_run_result(sid, final_pts, tgt, res_txt)
                        tag_best = " 🏆 新纪录！" if is_best else ""
                        self._log(
                            f"🎉 第{sid}关 第 {self.runs_done} 盘完成：得分 {final_pts}/{tgt}"
                            f"（{rec_item['eval']}）{tag_best}"
                        )

                        if obs is not None and obs_recording_by_us:
                            time.sleep(2.0)
                            # 防冲突兜底：只要打破本关已存最高分，且满足(达到OBS基准分 或 已达到用户设定的目标分)，一律珍藏不误删！
                            hit_target = (target_score > 0 and final_pts >= target_score)
                            should_keep = (final_pts > obs_best_saved_pts) and (final_pts >= obs_min_keep or hit_target)
                            obs_try_finish(keep=should_keep, sid=sid, pts=final_pts)

                        reached_target = (target_score > 0 and final_pts >= target_score)
                        reached_runs = (max_runs > 0 and self.runs_done >= max_runs)
                        if reached_target or reached_runs:
                            with self.lock:
                                self.queue_info[t_idx]["status"] = "done"
                            reason = f"第{sid}关达标 {final_pts}≥{target_score}！" if reached_target else f"第{sid}关已打满 {self.runs_done} 盘（最高 {self.best_pts} 分）"
                            self._log(f"✅ {reason}")
                            if is_last_task:
                                with self.lock:
                                    self.phase = "finished"
                                    self.result_text = f"{reason} | 共完成 {self.total_runs_done} 盘"
                                return
                            # 还有下一关：先退回选关界面再跳出本关循环进入下一关
                            self._log("返回选关界面，准备切换至队列下一关...")
                            for _ in range(4):
                                if self.stop_event.is_set():
                                    break
                                adb.tap(640, 360, wait=1.5)
                                if rec.detect_stage_select_screen(adb.screencap_bgr()) is not None:
                                    break
                            break

                        # 继续刷当前关下一盘
                        self._log(f"准备开始第 {self.runs_done + 1} 盘（当前最高 {self.best_pts} 分，已剪枝 {self.pruned_runs} 盘）...")
                        for _ in range(4):
                            if self.stop_event.is_set():
                                break
                            adb.tap(640, 360, wait=1.5)
                            if rec.detect_stage_select_screen(adb.screencap_bgr()) is not None:
                                break
                        can_collect_flag = False
                        last_collect_val = None
                        prev_hand_len = None
                        last_known_yield = None
                        last_turn_pts = 0
                        tracked_turn = None
                        just_ended_last_turn = False
                        run_cycles = 0
                        fail_streak = 0
                        continue

                    # 2. 检测选关主界面
                    sel_sid = rec.detect_stage_select_screen(img)
                    if sel_sid is not None:
                        if just_ended_last_turn:
                            sid = locked_stage or sel_sid
                            tgt = STAGES.get(sid, {}).get("target_pts", last_target_pts)
                            rec_item, is_best = self._record_run_result(sid, last_turn_pts, tgt, "")
                            self._log(f"🎉 第{sid}关 第 {self.runs_done} 盘已结算：{last_turn_pts}/{tgt}" + (" 🏆 新纪录！" if is_best else ""))
                            if obs is not None and obs_recording_by_us:
                                hit_target = (target_score > 0 and last_turn_pts >= target_score)
                                should_keep = (last_turn_pts > obs_best_saved_pts) and (last_turn_pts >= obs_min_keep or hit_target)
                                obs_try_finish(keep=should_keep, sid=sid, pts=last_turn_pts)
                            just_ended_last_turn = False
                            last_turn_pts = 0
                            run_cycles = 0
                            if (target_score > 0 and rec_item["pts"] >= target_score) or (max_runs > 0 and self.runs_done >= max_runs):
                                with self.lock:
                                    self.queue_info[t_idx]["status"] = "done"
                                if is_last_task:
                                    with self.lock:
                                        self.phase = "finished"
                                        self.result_text = f"全部任务已完成！末关最高 {self.best_pts} 分"
                                    return
                                break

                        if locked_stage is None:
                            locked_stage = sel_sid
                            cur_locked_stage = locked_stage
                            last_target_pts = STAGES[locked_stage]["target_pts"]
                            with self.lock:
                                self.stage_id = locked_stage
                                self.queue_info[t_idx]["stage_id"] = locked_stage
                        elif sel_sid != locked_stage:
                            diff = locked_stage - sel_sid
                            self._log(f"当前在选关页第 {sel_sid} 关，自动切换至第 {locked_stage} 关...")
                            arrow_x = 1248 if diff > 0 else 32
                            for _ in range(min(abs(diff), 9)):
                                if self.stop_event.is_set():
                                    break
                                adb.tap(arrow_x, 335, wait=0.55)
                            time.sleep(0.5)
                            continue

                        self._update_view(None, [], None, img)
                        self._log(f"进入第 {locked_stage} 关「{STAGES[locked_stage]['name']}」（第 {self.runs_done + 1} 盘）...")
                        obs_try_start(self.runs_done + 1)
                        adb.tap(1115, 600, wait=2.1)
                        adb.tap(640, 650, wait=0.8)
                        can_collect_flag = False
                        last_collect_val = None
                        prev_hand_len = None
                        last_known_yield = None
                        last_turn_pts = 0
                        tracked_turn = 1
                        run_cycles = 0
                        fail_streak = 0
                        continue

                    # 3. 识别局内状态
                    try:
                        state, meta = rec.recognize(img, stage_hint=locked_stage)
                        # 若任意任务槽（尤其是刚交完的 ×4 大单）仍在播放刷新滑入动画而被识别为空，等待并复读
                        for _q_retry in range(3):
                            if not any(len(q.items) == 0 for q in state.quests):
                                break
                            time.sleep(0.75)
                            img_retry = adb.screencap_bgr()
                            try:
                                st_retry, meta_retry = rec.recognize(img_retry, stage_hint=locked_stage)
                                img, state, meta = img_retry, st_retry, meta_retry
                            except Exception:
                                break
                        fail_streak = 0
                    except Exception as e:
                        if rec.detect_abandon_turn_dialog(img):
                            self._log("检测到意外结束回合确认框，点「取消」返回局内重新同步...")
                            adb.tap(320, 522, wait=0.9)
                            continue
                        fail_streak += 1
                        self._log(f"画面识别重试 ({fail_streak}/5): {e}")
                        if fail_streak == 1:
                            adb.tap(640, 650, wait=0.85)
                        elif fail_streak >= 5:
                            raise RuntimeError(f"连续 5 次无法识别局内画面，已自动中止：{e}")
                        else:
                            time.sleep(1.1)
                        continue

                    if locked_stage is None:
                        locked_stage = state.stage_id
                        cur_locked_stage = locked_stage
                    if tracked_turn is None:
                        tracked_turn = max(1, min(state.cur_turn, state.max_turns))
                    else:
                        state.cur_turn = max(1, min(tracked_turn, state.max_turns))

                    obs_try_start(self.runs_done + 1)
                    # 局内重建值严格单调不减且单次同步最多增长 <= 8（同时过滤向上跳变如 9/4->91 与特效遮挡向下误读为 0 导致误剪枝/误算手牌成长）
                    if last_turn_pts <= state.cur_pts <= last_turn_pts + 8:
                        last_turn_pts = state.cur_pts
                    else:
                        state.cur_pts = last_turn_pts
                    last_target_pts = state.target_pts

                    if not state.tiles[state.pos].locked and meta.get("detected_yield"):
                        last_known_yield = state.base_yield
                    elif (state.tiles[state.pos].locked or not meta.get("detected_yield")) and last_known_yield is not None:
                        state.base_yield = max(state.base_yield, last_known_yield)

                    # 仅当本轮手牌确实减少（说明上一步出牌已生效）时，用已执行动作精确兜底 can_collect / last_collect（防止 PC 端 COMBO+ 标签漏检断连击）
                    hand_consumed = (prev_hand_len is None) or (len(state.hand) < prev_hand_len)
                    if hand_consumed and len(state.hand) > 0:
                        if can_collect_flag and state.last_collect is None:
                            state.can_collect = True
                        elif (not can_collect_flag) and state.last_collect is None and last_collect_val is not None:
                            state.last_collect = last_collect_val

                    # 4. 手牌已空 -> 先二次复核手牌未处于滑动/抬起动画，再检查中盘剪枝并点「结束回合」
                    if len(state.hand) == 0:
                        if prev_hand_len is not None and prev_hand_len > 0:
                            time.sleep(0.45)
                            chk_hand, _, _ = rec._detect_cards(adb.screencap_bgr())
                            if len(chk_hand) > 0:
                                self._log(f"检测到仍有 {len(chk_hand)} 张手牌（刚播完动画），重新同步出牌...")
                                continue

                        self._update_view(state_to_dict(state), [], None, img)
                        is_last = state.cur_turn >= state.max_turns

                        # ⚡ 中盘低分剪枝检查：按「等效分 = 已得分 + 未完成任务进度折算分」判定，避免误杀刚交大单/临门一脚的潜力局
                        eff_pts = _calc_effective_pts(state)
                        if (
                            prune_enabled
                            and prune_min_pts > 0
                            and (max_runs != 1 or multi_mode)
                            and not is_last
                            and state.cur_turn == prune_turn
                            and eff_pts < prune_min_pts
                        ):
                            with self.lock:
                                self.pruned_runs += 1
                            self._log(
                                f"⚡ 中盘剪枝触发：第 {state.cur_turn} 回合结束已得 {state.cur_pts} 分 / 等效 {eff_pts:.1f} 分"
                                f"（< 门槛 {prune_min_pts} 分），立刻放弃重开省时间！"
                            )
                            obs_try_finish(keep=False, sid=locked_stage or 1, pts=state.cur_pts)
                            _abandon_current_run(adb, rec)
                            can_collect_flag = False
                            last_collect_val = None
                            prev_hand_len = None
                            last_known_yield = None
                            last_turn_pts = 0
                            tracked_turn = None
                            just_ended_last_turn = False
                            run_cycles = 0
                            continue

                        self._log(
                            f"[S{locked_stage:02d} 第{self.runs_done + 1}盘] 第 {state.cur_turn}/{state.max_turns} 回合结束"
                            f"（{state.cur_pts}/{state.target_pts}分）..."
                        )
                        end_xy = meta.get("end_turn_xy") or (1160, 425)
                        ended_ok = _click_end_turn_with_confirm(
                            adb, rec, end_xy, is_last=is_last, allow_abandon_cards=False
                        )
                        if not ended_ok:
                            self._log("⚠️ 游戏提示仍有未出手牌，已自动点「取消」并返回重新同步出牌！")
                            continue
                        can_collect_flag = False
                        last_collect_val = None
                        prev_hand_len = None
                        if is_last:
                            just_ended_last_turn = True
                        else:
                            tracked_turn = min(state.max_turns, (tracked_turn or state.cur_turn) + 1)
                        continue

                    # 5. 计算最优策略并执行
                    plans, stats = solve_turn(state, top_k=3)
                    self._update_view(state_to_dict(state), [plan_to_dict(p) for p in plans], stats, img)
                    if not plans:
                        end_xy = meta.get("end_turn_xy") or (1160, 425)
                        ended_ok = _click_end_turn_with_confirm(
                            adb, rec, end_xy, is_last=(state.cur_turn >= state.max_turns), allow_abandon_cards=False
                        )
                        if ended_ok:
                            can_collect_flag = False
                            last_collect_val = None
                            prev_hand_len = None
                        continue

                    best = plans[0]
                    run_n = best.first_resync_step or len(best.steps)
                    steps_to_run = best.steps[:run_n]
                    self._log(
                        f"[S{locked_stage:02d} 第{self.runs_done + 1}盘] T{state.cur_turn}/{state.max_turns} "
                        f"({state.cur_pts}/{state.target_pts}分, 手牌{state.hand}) → 执行{run_n}/{len(best.steps)}步"
                    )

                    last_act = None
                    last_step_card: Optional[int] = None
                    prev_hand_len = len(state.hand)
                    for st_step in steps_to_run:
                        if self.stop_event.is_set():
                            with self.lock:
                                self.phase = "stopped"
                            self._log("已手动停止自动冲分。")
                            return

                        if st_step.action == "end_turn":
                            is_last = state.cur_turn >= state.max_turns
                            eff_pts = _calc_effective_pts(state)
                            # 提前结束回合前也按等效分检查一次中盘剪枝
                            if (
                                prune_enabled
                                and prune_min_pts > 0
                                and (max_runs != 1 or multi_mode)
                                and not is_last
                                and state.cur_turn == prune_turn
                                and eff_pts < prune_min_pts
                            ):
                                with self.lock:
                                    self.pruned_runs += 1
                                self._log(
                                    f"⚡ 中盘剪枝触发（T{state.cur_turn} 已得 {state.cur_pts} / 等效 {eff_pts:.1f} < {prune_min_pts} 分），直接放弃重开！"
                                )
                                obs_try_finish(keep=False, sid=locked_stage or 1, pts=state.cur_pts)
                                _abandon_current_run(adb, rec)
                                tracked_turn = None
                                last_act = "pruned"
                                break

                            self._log(f"  ⏭ {st_step.note}")
                            end_xy = meta.get("end_turn_xy") or (1160, 425)
                            _click_end_turn_with_confirm(
                                adb, rec, end_xy, is_last=is_last, allow_abandon_cards=True
                            )
                            last_act = "end_turn"
                            if is_last:
                                just_ended_last_turn = True
                            else:
                                tracked_turn = min(state.max_turns, (tracked_turn or state.cur_turn) + 1)
                            break

                        def _pick_card(infos: List[dict], val: int, prefer_combo: bool):
                            if prefer_combo:
                                for ci in infos:
                                    if ci["val"] == val and ci.get("combo"):
                                        return ci
                            for ci in infos:
                                if ci["val"] == val:
                                    return ci
                            return None

                        # 闭环定位目标手牌（若上一步动画刚结束卡牌仍在重排，短暂复检避免点空）
                        card_infos: List[dict] = []
                        target_card = None
                        for _find_try in range(3):
                            img_cur = adb.screencap_bgr()
                            _, card_infos, _ = rec._detect_cards(img_cur)
                            target_card = _pick_card(card_infos, st_step.card, st_step.action == "combo")
                            if target_card is not None:
                                break
                            time.sleep(0.35)

                        if target_card is None:
                            self._log(f"未找到点数 [{st_step.card}]，中断本轮连打并重新同步...")
                            break

                        before_cnt = len(card_infos)
                        step_consumed = False

                        # 闭环出牌：点牌 → 点前进/收集 → 确认手牌数量确实减少；若被动画吞掉则自动重试 1 次，绝不漏牌跳过！
                        for _play_try in range(2):
                            adb.tap(target_card["cx"], target_card["cy"], wait=0.48)
                            if st_step.action == "advance":
                                wait_t = 1.95 if st_step.chests_hit else 1.40
                                adb.tap(988, 608, wait=wait_t)
                            else:
                                if 0 in (st_step.completed_quests or []):
                                    wait_t = 2.95
                                elif st_step.completed_quests:
                                    wait_t = 2.40
                                else:
                                    wait_t = 1.80
                                adb.tap(1160, 608, wait=wait_t)

                            # 轮询确认手牌数量确实减少（最多补等 1.2s，防止长距离前进/连击动画尚未播完）
                            after_infos: List[dict] = []
                            for _chk_i in range(4):
                                _, after_infos, _ = rec._detect_cards(adb.screencap_bgr())
                                if len(after_infos) < before_cnt:
                                    step_consumed = True
                                    break
                                time.sleep(0.30)

                            if step_consumed:
                                time.sleep(0.18)  # 留出剩余手牌向左归位稳定的时间
                                break

                            if _play_try == 0:
                                self._log(f"  ↻ 点数 [{st_step.card}] 未选上或动画未就绪，重新点选重试...")
                                target_card = _pick_card(after_infos, st_step.card, st_step.action == "combo")
                                if target_card is None:
                                    break

                        if not step_consumed:
                            self._log(f"  ⚠️ 点数 [{st_step.card}] 重试后仍未生效，立刻中断本轮连打并重新同步！")
                            break

                        last_act = st_step.action
                        last_step_card = st_step.card

                    if last_act == "pruned":
                        can_collect_flag = False
                        last_collect_val = None
                        prev_hand_len = None
                        last_known_yield = None
                        last_turn_pts = 0
                        run_cycles = 0
                        continue

                    if last_act == "end_turn":
                        can_collect_flag = False
                        last_collect_val = None
                        prev_hand_len = None
                    elif last_act == "advance":
                        can_collect_flag = True
                        last_collect_val = None
                    elif last_act in ("collect", "combo"):
                        can_collect_flag = False
                        last_collect_val = last_step_card
                    else:
                        can_collect_flag = False
                        last_collect_val = None
                    time.sleep(0.28)

            if self.stop_event.is_set():
                with self.lock:
                    self.phase = "stopped"
                self._log(f"已手动停止。本次共完成 {self.total_runs_done} 盘。")
            else:
                with self.lock:
                    self.phase = "finished"
                    self.result_text = f"🎉 全部 {len(tasks)} 关连刷任务已完成！共打完 {self.total_runs_done} 盘"
                self._log(self.result_text)

        except Exception as e:
            with self.lock:
                self.phase = "error"
                self.error_msg = str(e)
            self._log(f"❌ 自动冲分出错：{e}")
        finally:
            if obs is not None and obs_recording_by_us:
                try:
                    out_path = obs.stop_record()
                    finalize_recording_file(out_path, keep=False)
                except Exception:
                    pass
            with self.lock:
                self.running = False


_auto_runner = AutoRunner()


def _global_hotkey_watcher():
    """
    系统级全局急停热键监听线程（零第三方依赖，游戏全屏/无边框下均 100% 生效）：
    按下 F10 或 End 键立刻停止自动挂机并释放鼠标！
    """
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    VK_F10 = 0x79
    VK_END = 0x23
    while True:
        try:
            if _auto_runner.running:
                if (user32.GetAsyncKeyState(VK_F10) & 0x8000) or (user32.GetAsyncKeyState(VK_END) & 0x8000):
                    _auto_runner.stop(reason="全局快捷键 F10/End 急停")
                    time.sleep(0.6)
            time.sleep(0.04)
        except Exception:
            time.sleep(0.2)


threading.Thread(target=_global_hotkey_watcher, daemon=True).start()


def get_recognizer() -> BoardRecognizer:
    global _recognizer
    with _recognizer_lock:
        if _recognizer is None:
            _recognizer = BoardRecognizer()
    return _recognizer


def state_to_dict(state: GameState) -> dict:
    return {
        "stage_id": state.stage_id,
        "res_types": state.res_types,
        "tiles": [asdict(t) for t in state.tiles],
        "pos": state.pos,
        "base_yield": state.base_yield,
        "hand": state.hand,
        "quests": [
            {
                "reward": q.reward,
                "items": [asdict(it) for it in q.items],
            }
            for q in state.quests
        ],
        "cur_turn": state.cur_turn,
        "max_turns": state.max_turns,
        "cur_pts": state.cur_pts,
        "target_pts": state.target_pts,
        "can_collect": state.can_collect,
        "last_collect": state.last_collect,
        "lap_bonus": state.lap_bonus,
        "hand_scale": state.hand_scale,
        "chest_amount": state.chest_amount,
    }


def dict_to_state(data: Dict[str, Any]) -> GameState:
    stage_id = int(data.get("stage_id", 1))
    s_meta = STAGES.get(stage_id, STAGES[1])
    res_types = data.get("res_types") or list(RESOURCE_SETS[s_meta["res_set"]])

    tiles = []
    for i, t in enumerate(data.get("tiles", [])):
        tiles.append(TileState(
            idx=int(t.get("idx", i)),
            res=str(t.get("res", res_types[0])),
            mult=int(t.get("mult", 1)),
            locked=bool(t.get("locked", False)),
            chest=bool(t.get("chest", False)),
            cx=int(t.get("cx", 0)),
            cy=int(t.get("cy", 0)),
        ))

    quests = []
    for q in data.get("quests", []):
        items = []
        for it in q.get("items", []):
            tgt = int(it.get("target", 0))
            if tgt > 0:
                items.append(QuestItem(
                    res=str(it.get("res", res_types[0])),
                    cur=max(0, int(it.get("cur", 0))),
                    target=tgt,
                ))
        quests.append(QuestState(reward=int(q.get("reward", 1)), items=items))

    hand = [int(x) for x in data.get("hand", []) if 1 <= int(x) <= 5]
    last_col = data.get("last_collect")
    last_col = int(last_col) if (last_col is not None and str(last_col).isdigit() and int(last_col) > 0) else None

    return GameState(
        stage_id=stage_id,
        res_types=res_types,
        tiles=tiles,
        pos=int(data.get("pos", 0)) % 10,
        base_yield=max(1, int(data.get("base_yield", 1))),
        hand=hand,
        quests=quests,
        cur_turn=max(1, int(data.get("cur_turn", 1))),
        max_turns=max(1, int(data.get("max_turns", s_meta["max_turns"]))),
        cur_pts=max(0, int(data.get("cur_pts", 0))),
        target_pts=max(1, int(data.get("target_pts", s_meta["target_pts"]))),
        can_collect=bool(data.get("can_collect", False)),
        last_collect=last_col,
        lap_bonus=int(data.get("lap_bonus", s_meta["lap_bonus"])),
        hand_scale=bool(data.get("hand_scale", s_meta["hand_scale"])),
        chest_amount=int(data.get("chest_amount", s_meta.get("chest_amount", 15))),
    )


def plan_to_dict(plan) -> dict:
    return {
        "rank": plan.rank,
        "score": plan.score,
        "expected_pts": plan.expected_pts,
        "guaranteed_pts": plan.guaranteed_pts,
        "progress_value": plan.progress_value,
        "lap_value": plan.lap_value,
        "total_gained": plan.total_gained,
        "chests_count": plan.chests_count,
        "laps_count": plan.laps_count,
        "end_pos": plan.end_pos,
        "end_base_yield": plan.end_base_yield,
        "final_quests": plan.final_quests,
        "first_resync_step": plan.first_resync_step,
        "steps": [asdict(s) for s in plan.steps],
    }


def _encode_preview_jpg(img_bgr) -> str:
    small = cv2.resize(img_bgr, (640, 360), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta", methods=["GET"])
def api_meta():
    return jsonify({
        "stages": STAGES,
        "rush_presets": DEFAULT_RUSH_PRESETS,
        "resource_sets": RESOURCE_SETS,
        "resource_labels": RESOURCE_LABELS,
        "default_mode": _last_ctrl_mode,
        "default_device": _last_adb_device,
        "default_adb_path": _last_adb_path,
        "default_pc_window": _last_pc_window,
        "is_admin": is_process_admin(),
    })


@app.route("/api/pc/windows", methods=["GET"])
def api_pc_windows():
    return jsonify({
        "ok": True,
        "is_admin": is_process_admin(),
        "windows": list_pc_windows(),
    })


@app.route("/api/solve", methods=["POST"])
def api_solve():
    try:
        payload = request.get_json(force=True)
        state = dict_to_state(payload.get("state", payload))
        top_k = int(payload.get("top_k", 3))
        plans, stats = solve_turn(state, top_k=top_k)
        return jsonify({
            "ok": True,
            "state": state_to_dict(state),
            "plans": [plan_to_dict(p) for p in plans],
            "stats": stats,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def _resolve_client_args(payload: dict):
    global _last_ctrl_mode, _last_adb_device, _last_adb_path, _last_pc_window
    ctrl_mode = (payload.get("ctrl_mode") or _last_ctrl_mode or "adb").strip().lower()
    device = (payload.get("device") or _last_adb_device).strip()
    adb_path = (payload.get("adb_path") or _last_adb_path).strip()
    window_title = (payload.get("window_title") or _last_pc_window or "明日方舟").strip()
    _last_ctrl_mode = ctrl_mode
    _last_adb_device = device
    _last_adb_path = adb_path
    _last_pc_window = window_title
    return ctrl_mode, device, adb_path, window_title


@app.route("/api/sync", methods=["POST"])
def api_sync():
    global _last_card_infos
    try:
        payload = request.get_json(silent=True) or {}
        ctrl_mode, device, adb_path, window_title = _resolve_client_args(payload)
        stage_hint = payload.get("stage_hint")
        stage_hint = int(stage_hint) if (stage_hint and str(stage_hint).isdigit()) else None
        can_collect_override = payload.get("can_collect_override")

        client = create_client(mode=ctrl_mode, adb_path=adb_path, device=device, window_title=window_title)
        img = client.screencap_bgr()
        rec = get_recognizer()

        # 若当前停在选关主界面，自动识别出当前关卡号并返回给前端锁定
        sel_stage = rec.detect_stage_select_screen(img)
        if sel_stage is not None:
            return jsonify({
                "ok": True,
                "mode": "stage_select",
                "detected_stage_id": sel_stage,
                "stage_name": STAGES[sel_stage]["name"],
                "stage_desc": STAGES[sel_stage]["desc"],
                "preview": _encode_preview_jpg(img),
            })

        state, meta = rec.recognize(img, stage_hint=stage_hint)

        # 允许用户指定回合中途刚前进完的状态（仅当未处于 COMBO+ 时）
        if can_collect_override is not None and state.last_collect is None:
            state.can_collect = bool(can_collect_override)

        _last_card_infos = meta.get("card_infos", [])
        plans, stats = solve_turn(state, top_k=3)

        return jsonify({
            "ok": True,
            "state": state_to_dict(state),
            "meta": meta,
            "plans": [plan_to_dict(p) for p in plans],
            "stats": stats,
            "preview": _encode_preview_jpg(img),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/exec_steps", methods=["POST"])
def api_exec_steps():
    """
    按用户明确点击，在模拟器或 PC 端中执行指定步数（每步：选牌 -> 点前进/收集资源），
    执行完成后自动重新截屏识别并重算剩余手牌最优策略！
    """
    global _last_card_infos
    try:
        payload = request.get_json(force=True)
        ctrl_mode, device, adb_path, window_title = _resolve_client_args(payload)
        steps = payload.get("steps") or []
        stage_hint = payload.get("stage_hint")
        stage_hint = int(stage_hint) if (stage_hint and str(stage_hint).isdigit()) else None

        if not steps:
            return jsonify({"ok": False, "error": "没有待执行的步骤"}), 400

        adb = create_client(mode=ctrl_mode, adb_path=adb_path, device=device, window_title=window_title)
        rec = get_recognizer()

        last_action = None
        for idx_s, st_item in enumerate(steps):
            action = str(st_item["action"])
            if action == "end_turn":
                img_cur = adb.screencap_bgr()
                _, _, ct, mt, end_xy = rec._detect_header_and_turn(img_cur)
                _click_end_turn_with_confirm(adb, rec, end_xy, is_last=(ct >= mt))
                last_action = "end_turn"
                break

            # 每步前实时抓取一次当前手牌坐标，保证点得绝对精准
            img_cur = adb.screencap_bgr()
            _, card_infos, _ = rec._detect_cards(img_cur)
            card_val = int(st_item["card"])

            # 若是 COMBO+ 优先挑带 combo 标记的牌，否则挑第一张匹配点数的牌
            target_card = None
            if action == "combo":
                for ci in card_infos:
                    if ci["val"] == card_val and ci.get("combo"):
                        target_card = ci
                        break
            if target_card is None:
                for ci in card_infos:
                    if ci["val"] == card_val:
                        target_card = ci
                        break
            if target_card is None:
                return jsonify({"ok": False, "error": f"第 {idx_s+1} 步未在当前手牌中找到点数 [{card_val}]"}), 400

            # 点击卡牌
            adb.tap(target_card["cx"], target_card["cy"], wait=0.45)
            # 点击前进 (988, 608) 或 收集资源 (1160, 608) —— 同时居中适配模拟器与PC端
            if action == "advance":
                adb.tap(988, 608, wait=1.55)
            else:
                adb.tap(1160, 608, wait=2.15)
            last_action = action

        # 执行结束后多等 0.4 秒确保计划刷新/滚数动画稳定
        time.sleep(0.4)
        img_after = adb.screencap_bgr()
        state, meta = rec.recognize(img_after, stage_hint=stage_hint)
        # 如果刚刚执行的最后一步是 advance，那么此时必然处于 can_collect=True！
        if last_action == "advance" and state.last_collect is None and len(state.hand) > 0:
            state.can_collect = True
        _last_card_infos = meta.get("card_infos", [])
        plans, stats = solve_turn(state, top_k=3)

        return jsonify({
            "ok": True,
            "executed": len(steps),
            "state": state_to_dict(state),
            "meta": meta,
            "plans": [plan_to_dict(p) for p in plans],
            "stats": stats,
            "preview": _encode_preview_jpg(img_after),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/end_turn", methods=["POST"])
def api_end_turn():
    """点击「结束回合」，等待新回合发牌动画后自动同步并计算！"""
    global _last_card_infos
    try:
        payload = request.get_json(silent=True) or {}
        ctrl_mode, device, adb_path, window_title = _resolve_client_args(payload)
        stage_hint = payload.get("stage_hint")
        stage_hint = int(stage_hint) if (stage_hint and str(stage_hint).isdigit()) else None

        adb = create_client(mode=ctrl_mode, adb_path=adb_path, device=device, window_title=window_title)
        rec = get_recognizer()
        img_pre = adb.screencap_bgr()
        _, _, ct, mt, end_xy = rec._detect_header_and_turn(img_pre)
        _click_end_turn_with_confirm(adb, rec, end_xy, is_last=(ct >= mt))
        img = adb.screencap_bgr()
        state, meta = rec.recognize(img, stage_hint=stage_hint)
        _last_card_infos = meta.get("card_infos", [])
        plans, stats = solve_turn(state, top_k=3)

        return jsonify({
            "ok": True,
            "state": state_to_dict(state),
            "meta": meta,
            "plans": [plan_to_dict(p) for p in plans],
            "stats": stats,
            "preview": _encode_preview_jpg(img),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/obs/test", methods=["POST"])
def api_obs_test():
    try:
        payload = request.get_json(silent=True) or {}
        obs = ObsController(
            host=str(payload.get("host", "127.0.0.1")),
            port=int(payload.get("port", 4455) or 4455),
            password=str(payload.get("password", "")),
        )
        info = obs.test_connection()
        return jsonify({"ok": True, "info": info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/auto/start", methods=["POST"])
def api_auto_start():
    payload = request.get_json(silent=True) or {}
    ctrl_mode, device, adb_path, window_title = _resolve_client_args(payload)
    stage_hint = payload.get("stage_hint")
    stage_hint = int(stage_hint) if (stage_hint and str(stage_hint).isdigit()) else None
    max_runs = int(payload.get("max_runs", 1))
    target_score = int(payload.get("target_score", 0))
    obs_cfg = payload.get("obs") or {}
    prune_cfg = payload.get("prune") or {}
    stage_queue = payload.get("stage_queue") or []

    started = _auto_runner.start(
        ctrl_mode=ctrl_mode,
        device=device,
        adb_path=adb_path,
        window_title=window_title,
        stage_hint=stage_hint,
        max_runs=max_runs,
        target_score=target_score,
        obs_cfg=obs_cfg,
        prune_cfg=prune_cfg,
        stage_queue=stage_queue,
    )
    if not started:
        return jsonify({"ok": False, "error": "自动通关正在运行中"}), 409
    return jsonify({"ok": True, "status": _auto_runner.snapshot()})


@app.route("/api/auto/stop", methods=["POST"])
def api_auto_stop():
    _auto_runner.stop()
    return jsonify({"ok": True, "status": _auto_runner.snapshot()})


@app.route("/api/auto/status", methods=["GET"])
def api_auto_status():
    return jsonify({"ok": True, "status": _auto_runner.snapshot()})


if __name__ == "__main__":
    url = "http://127.0.0.1:5050"
    print(f"正在启动「喀兰贸易技术研发部」策略求解器: {url}")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
