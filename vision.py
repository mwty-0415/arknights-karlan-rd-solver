# -*- coding: utf-8 -*-
"""
明日方舟「雪山降临1101」小游戏「喀兰贸易技术研发部」
模拟器 ADB 截屏 + 视觉识别模块 (Vision & State Extractor)
"""

from __future__ import annotations
import os
import re
import sys
import math
import time
import ctypes
import subprocess
from typing import Dict, List, Optional, Tuple
import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

try:
    import win32gui
    import win32con
    import mss
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False

if HAS_WIN32:
    try:
        # 启用 Per-Monitor V2 DPI 感知，确保 125%/150%/200% 缩放下窗口与鼠标坐标1像素不差
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

from solver import (
    GameState,
    TileState,
    QuestState,
    QuestItem,
    RESOURCE_SETS,
    STAGES,
)

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(BASE_DIR, "assets", "templates")

DEFAULT_ADB_CANDIDATES = [
    r"E:\MuMuPlayer\nx_main\adb.exe",
    r"E:\MuMuPlayer\nx_device\15.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    "adb",
]

ALL_RES = ["gear", "crystal", "wood", "steel", "flame"]


def find_adb_path(custom_path: Optional[str] = None) -> str:
    if custom_path and os.path.exists(custom_path):
        return custom_path
    for p in DEFAULT_ADB_CANDIDATES:
        if p == "adb" or os.path.exists(p):
            return p
    return "adb"


class AdbClient:
    def __init__(self, adb_path: Optional[str] = None, device: str = "127.0.0.1:16384"):
        self.adb_path = find_adb_path(adb_path)
        self.device = device
        self._connected = False

    def ensure_connected(self):
        if not self._connected:
            try:
                subprocess.run(
                    [self.adb_path, "connect", self.device],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=6,
                    check=False,
                )
                self._connected = True
            except Exception:
                pass

    def screencap_bgr(self) -> np.ndarray:
        self.ensure_connected()
        try:
            raw = subprocess.check_output(
                [self.adb_path, "-s", self.device, "exec-out", "screencap", "-p"],
                timeout=8,
            )
        except Exception:
            # 重连一次再试
            self._connected = False
            self.ensure_connected()
            raw = subprocess.check_output(
                [self.adb_path, "-s", self.device, "exec-out", "screencap", "-p"],
                timeout=8,
            )
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("ADB 截屏解码失败，请确认模拟器已启动且处于游戏画面")
        if img.shape[1] != 1280 or img.shape[0] != 720:
            img = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        return img

    def tap(self, x: int, y: int, wait: float = 0.0):
        self.ensure_connected()
        subprocess.run(
            [self.adb_path, "-s", self.device, "shell", "input", "tap", str(int(x)), str(int(y))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        if wait > 0:
            time.sleep(wait)


def is_process_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def list_pc_windows() -> List[dict]:
    """列出当前桌面可见的候选取景窗口（优先把明日方舟排在前面）。"""
    if not HAS_WIN32:
        return []
    wins: List[dict] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            l, t, r, b = win32gui.GetClientRect(hwnd)
            if r >= 400 and b >= 240:
                wins.append({"hwnd": int(hwnd), "title": title, "width": int(r), "height": int(b)})
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return []

    def _prio(item: dict) -> int:
        tl = item["title"].lower()
        if "明日方舟" in tl or "arknights" in tl:
            return 0
        if "mumu" in tl or "雷电" in tl or "bluestacks" in tl:
            return 1
        return 2

    wins.sort(key=lambda x: (_prio(x), -x["width"] * x["height"]))
    return wins


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]


def _send_mouse_click(sx: int, sy: int, hold_sec: float = 0.065):
    """
    使用 Win32 SetCursorPos + 多屏虚拟桌面绝对坐标 SendInput 发送硬件级左键点击，
    完全兼容双屏/不同DPI与 Unity/DirectInput PC 游戏。
    """
    user32 = ctypes.windll.user32
    vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN

    # 计算多屏虚拟桌面 0..65535 绝对坐标
    ax = int(round((sx - vx) * 65535.0 / max(1, vw - 1)))
    ay = int(round((sy - vy) * 65535.0 / max(1, vh - 1)))
    extra = ctypes.c_ulong(0)

    # 先用 SetCursorPos + SendInput(ABSOLUTE|VIRTUALDESK|MOVE) 双重精确定位
    user32.SetCursorPos(int(sx), int(sy))
    flags_move = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
    mv = _INPUT(type=0, union=_INPUT_UNION(mi=_MOUSEINPUT(ax, ay, 0, flags_move, 0, ctypes.pointer(extra))))
    user32.SendInput(1, ctypes.byref(mv), ctypes.sizeof(_INPUT))
    time.sleep(0.05)

    down = _INPUT(type=0, union=_INPUT_UNION(mi=_MOUSEINPUT(ax, ay, 0, 0x0002 | 0x8000 | 0x4000, 0, ctypes.pointer(extra))))
    up = _INPUT(type=0, union=_INPUT_UNION(mi=_MOUSEINPUT(ax, ay, 0, 0x0004 | 0x8000 | 0x4000, 0, ctypes.pointer(extra))))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT))
    time.sleep(hold_sec)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT))


class PcWindowClient:
    """
    明日方舟 PC 端窗口驱动：
    - 通过 Win32 获取纯客户区物理像素坐标（自动去除标题栏/边框并兼容 16:9/16:10/21:9 居中黑边）
    - 通过 mss 高速截取原生高清画面并缩放到 1280x720 喂给统一识图引擎
    - 通过 Win32 SendInput 按比例精确映射回 PC 端客户区点击
    """

    def __init__(self, window_title: str = "明日方舟"):
        if not HAS_WIN32:
            raise RuntimeError("当前环境缺少 pywin32/mss，无法使用 PC 端窗口模式")
        self.window_title = (window_title or "明日方舟").strip()
        self._hwnd: Optional[int] = None

    def _find_hwnd(self) -> int:
        if self._hwnd and win32gui.IsWindow(self._hwnd) and win32gui.IsWindowVisible(self._hwnd):
            t = win32gui.GetWindowText(self._hwnd)
            if self.window_title.lower() in t.lower():
                return self._hwnd

        wins = list_pc_windows()
        kw = self.window_title.lower()
        # 优先精确匹配，其次子串匹配，其次默认明日方舟/Arknights
        for w in wins:
            if w["title"].lower() == kw:
                self._hwnd = w["hwnd"]
                return self._hwnd
        for w in wins:
            if kw in w["title"].lower():
                self._hwnd = w["hwnd"]
                return self._hwnd
        if kw in ("明日方舟", "arknights"):
            for w in wins:
                tl = w["title"].lower()
                if "明日方舟" in tl or "arknights" in tl:
                    self._hwnd = w["hwnd"]
                    return self._hwnd
        raise RuntimeError(
            f"未找到标题包含「{self.window_title}」的 PC 端窗口，请确认游戏已启动且未最小化"
        )

    def _get_effective_rect(self) -> Tuple[int, int, int, int, int]:
        """
        返回 (hwnd, eff_left, eff_top, eff_w, eff_h)，自动扣除超宽/非16:9居中黑边。
        """
        hwnd = self._find_hwnd()
        if win32gui.IsIconic(hwnd):
            raise RuntimeError("明日方舟 PC 端窗口已最小化，请还原到桌面（窗口化或无边框）后再试")
        l, t, r, b = win32gui.GetClientRect(hwnd)
        cw, ch = r - l, b - t
        if cw < 400 or ch < 240:
            raise RuntimeError(f"PC 端窗口尺寸异常 ({cw}x{ch})，请确认窗口未最小化")
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))

        # 若客户区接近 16:9 (误差 < 2%) 直接全用；否则按 16:9 居中裁出有效游戏画面
        target_ratio = 16.0 / 9.0
        cur_ratio = cw / float(ch)
        if abs(cur_ratio - target_ratio) <= 0.03:
            return hwnd, left, top, cw, ch
        elif cur_ratio > target_ratio:
            eff_w = int(round(ch * target_ratio))
            ox = (cw - eff_w) // 2
            return hwnd, left + ox, top, eff_w, ch
        else:
            eff_h = int(round(cw / target_ratio))
            oy = (ch - eff_h) // 2
            return hwnd, left, top + oy, cw, eff_h

    def ensure_foreground(self):
        hwnd = self._find_hwnd()
        try:
            if win32gui.GetForegroundWindow() != hwnd:
                # Windows 前台锁规避：先附着前台线程或轻触 Alt 再切前台，确保切焦点100%生效
                user32 = ctypes.windll.user32
                user32.keybd_event(0x12, 0, 0, 0)       # ALT down
                user32.keybd_event(0x12, 0, 0x0002, 0)  # ALT up
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.18)
        except Exception:
            pass

    def screencap_bgr(self) -> np.ndarray:
        _, left, top, w, h = self._get_effective_rect()
        with mss.mss() as sct:
            mon = {"left": int(left), "top": int(top), "width": int(w), "height": int(h)}
            shot = np.array(sct.grab(mon))  # BGRA
        img = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
        if img.shape[1] != 1280 or img.shape[0] != 720:
            img = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA)
        return img

    def tap(self, x: int, y: int, wait: float = 0.0):
        self.ensure_foreground()
        _, left, top, w, h = self._get_effective_rect()
        sx = int(round(left + (x / 1280.0) * w))
        sy = int(round(top + (y / 720.0) * h))
        _send_mouse_click(sx, sy)
        if wait > 0:
            time.sleep(wait)


def create_client(
    mode: str = "adb",
    adb_path: Optional[str] = None,
    device: str = "127.0.0.1:16384",
    window_title: str = "明日方舟",
):
    if (mode or "adb").lower() == "pc":
        return PcWindowClient(window_title=window_title)
    return AdbClient(adb_path=adb_path, device=device)


def _order_ring_tsp(pts: List[Tuple[float, str, int, int]]) -> List[Tuple[float, str, int, int]]:
    """
    使用 Held-Karp 精确 TSP 算法求 10 个地图节点的最短哈密顿闭合环路，
    再以最右侧节点(火车站)为0号起点、屏幕顺时针方向排序。
    """
    n = len(pts)
    if n <= 2:
        return pts
    s_idx = max(range(n), key=lambda i: pts[i][2])
    pts = [pts[s_idx]] + [pts[i] for i in range(n) if i != s_idx]
    dist = [[math.hypot(pts[i][2] - pts[j][2], pts[i][3] - pts[j][3]) for j in range(n)] for i in range(n)]

    full = (1 << (n - 1)) - 1
    dp: Dict[Tuple[int, int], float] = {}
    parent: Dict[Tuple[int, int], int] = {}
    for i in range(1, n):
        dp[(1 << (i - 1), i)] = dist[0][i]
        parent[(1 << (i - 1), i)] = 0

    for mask in range(1, full + 1):
        for last in range(1, n):
            if not (mask & (1 << (last - 1))):
                continue
            cur_d = dp.get((mask, last))
            if cur_d is None:
                continue
            for nxt in range(1, n):
                if mask & (1 << (nxt - 1)):
                    continue
                nmask = mask | (1 << (nxt - 1))
                nd = cur_d + dist[last][nxt]
                if nd < dp.get((nmask, nxt), 1e18):
                    dp[(nmask, nxt)] = nd
                    parent[(nmask, nxt)] = last

    best_last = min(range(1, n), key=lambda i: dp[(full, i)] + dist[i][0])
    path = []
    mask, cur = full, best_last
    while cur != 0:
        path.append(cur)
        prev = parent[(mask, cur)]
        mask ^= (1 << (cur - 1))
        cur = prev
    path.reverse()
    ordered = [pts[0]] + [pts[i] for i in path]

    # 屏幕坐标系 y 向下：有向面积 > 0 为顺时针
    area2 = sum(
        ordered[i][2] * ordered[(i + 1) % n][3] - ordered[(i + 1) % n][2] * ordered[i][3]
        for i in range(n)
    )
    if area2 < 0:
        ordered = [ordered[0]] + ordered[1:][::-1]
    return ordered


class BoardRecognizer:
    def __init__(self, tpl_dir: str = TPL_DIR):
        self.tpl_dir = tpl_dir
        self.ocr = RapidOCR()
        self.map_tpls = {
            r: cv2.imread(os.path.join(tpl_dir, f"map_{r}.png"), cv2.IMREAD_GRAYSCALE)
            for r in ALL_RES
        }
        self.quest_tpls = {
            r: cv2.imread(os.path.join(tpl_dir, f"quest_{r}.png"), cv2.IMREAD_GRAYSCALE)
            for r in ALL_RES
        }
        self.card_tpls = {
            d: cv2.imread(os.path.join(tpl_dir, f"card_{d}.png"), cv2.IMREAD_GRAYSCALE)
            for d in range(1, 6)
        }
        self.badge_x2 = cv2.imread(os.path.join(tpl_dir, "badge_x2.png"), cv2.IMREAD_COLOR)
        self.badge_chest = cv2.imread(os.path.join(tpl_dir, "badge_chest.png"), cv2.IMREAD_COLOR)
        self.top_tpls = {}
        for tname in ("top_x2", "top_lock", "top_lap", "top_hand", "top_chest"):
            p = os.path.join(tpl_dir, f"{tname}.png")
            if os.path.exists(p):
                self.top_tpls[tname] = cv2.imread(p, cv2.IMREAD_GRAYSCALE)

    def _rec_single(self, crop: np.ndarray) -> str:
        """直接调用 text_recognizer（跳过检测框），适合单数字与斜杠比分。"""
        res, _ = self.ocr.text_recognizer([crop])
        if res and len(res) > 0:
            txt = str(res[0][0]).strip()
            # 全角数字转半角
            trans = str.maketrans("０１２３４５６７８９／", "0123456789/")
            return txt.translate(trans)
        return ""

    def _ocr_text(self, crop: np.ndarray, scale: float = 1.0) -> List[Tuple[Tuple[int, int], str]]:
        if scale != 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        res, _ = self.ocr(crop)
        out = []
        for box, txt, _ in (res or []):
            cx = int((box[0][0] + box[2][0]) / (2 * scale))
            cy = int((box[0][1] + box[2][1]) / (2 * scale))
            out.append(((cx, cy), txt.strip()))
        return out

    def _detect_nodes(self, img: np.ndarray) -> Tuple[List[TileState], str]:
        """检测地图上的10个环形节点，并自动识别前/后5关资源体系、封锁、×2与宝箱。"""
        roi_gray = cv2.cvtColor(img[150:460, 220:960], cv2.COLOR_BGR2GRAY)
        cands = []
        for rname, tpl in self.map_tpls.items():
            res = cv2.matchTemplate(roi_gray, tpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res > 0.55)
            th, tw = tpl.shape[:2]
            for y, x in zip(ys, xs):
                cands.append((float(res[y, x]), rname, int(x + 220 + tw // 2), int(y + 150 + th // 2)))
        cands.sort(reverse=True)
        picked = []
        for sc, rname, cx, cy in cands:
            if all(math.hypot(cx - px, cy - py) > 46 for _, _, px, py in picked):
                picked.append((sc, rname, cx, cy))
                if len(picked) == 10:
                    break

        if len(picked) < 10:
            raise RuntimeError(f"仅检测到 {len(picked)}/10 个地图资源点，请确认游戏未处于弹窗或动画遮挡状态")

        # 判断前5关 (early) 还是后5关 (late)
        early_cnt = sum(1 for _, r, _, _ in picked if r in ("gear", "wood"))
        late_cnt = sum(1 for _, r, _, _ in picked if r in ("flame", "steel"))
        res_set_key = "late" if late_cnt > early_cnt else "early"
        allowed = RESOURCE_SETS[res_set_key]

        # 在确定阵营的3种资源内复核每个节点的分类，避免跨阵营误判
        refined = []
        for sc, rname, cx, cy in picked:
            best_r = rname
            if rname not in allowed:
                best_sc = -1.0
                for cand_r in allowed:
                    tpl = self.map_tpls[cand_r]
                    th, tw = tpl.shape[:2]
                    sub = cv2.cvtColor(img[cy - th : cy + th, cx - tw : cx + tw], cv2.COLOR_BGR2GRAY)
                    if sub.shape[0] >= th and sub.shape[1] >= tw:
                        m = float(cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED).max())
                        if m > best_sc:
                            best_sc = m
                            best_r = cand_r
                sc = best_sc
            refined.append((sc, best_r, cx, cy))

        ordered = _order_ring_tsp(refined)

        tiles: List[TileState] = []
        for idx, (_, rname, cx, cy) in enumerate(ordered):
            # 1. 检测红色封锁斜杠 (节点中心 ±20px)
            p_lock = img[max(0, cy - 20) : cy + 21, max(0, cx - 20) : cx + 21]
            b, g, r = cv2.split(p_lock)
            red_mask = (r.astype(int) > 125) & ((r.astype(int) - g.astype(int)) > 48) & ((r.astype(int) - b.astype(int)) > 48)
            locked = int(red_mask.sum()) >= 16

            # 2. 检测右侧 ×2 徽章与宝箱图标（多尺度兼容模拟器 1.0x 与 PC 端 0.90x 缩放）
            p_right = img[max(0, cy - 28) : min(720, cy + 28), min(1279, cx + 6) : min(1280, cx + 72)]
            mult = 1
            chest = False
            best_x2 = -1.0
            best_ch = -1.0
            for b_sc in (1.0, 0.90):
                t_x2 = self.badge_x2 if b_sc == 1.0 else cv2.resize(self.badge_x2, None, fx=b_sc, fy=b_sc, interpolation=cv2.INTER_AREA)
                if p_right.shape[0] >= t_x2.shape[0] and p_right.shape[1] >= t_x2.shape[1]:
                    sc_x2 = float(cv2.matchTemplate(p_right, t_x2, cv2.TM_CCOEFF_NORMED).max())
                    if sc_x2 > best_x2:
                        best_x2 = sc_x2
                t_ch = self.badge_chest if b_sc == 1.0 else cv2.resize(self.badge_chest, None, fx=b_sc, fy=b_sc, interpolation=cv2.INTER_AREA)
                if p_right.shape[0] >= t_ch.shape[0] and p_right.shape[1] >= t_ch.shape[1]:
                    sc_ch = float(cv2.matchTemplate(p_right, t_ch, cv2.TM_CCOEFF_NORMED).max())
                    if sc_ch > best_ch:
                        best_ch = sc_ch
            if best_x2 >= 0.70:
                mult = 2
            if best_ch >= 0.70:
                chest = True

            tiles.append(TileState(idx=idx, res=rname, mult=mult, locked=locked, chest=chest, cx=cx, cy=cy))

        return tiles, res_set_key

    def _detect_pos_and_yield(self, img: np.ndarray, tiles: List[TileState]) -> Tuple[int, Optional[int]]:
        """通过蓝色光柱 + 图标下方黑色产出数字双重定位当前站位，并 OCR 读出产出数字。"""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        best_idx = 0
        best_score = -1e9

        for t in tiles:
            # 1. 上方蓝色光柱分
            y1 = max(0, t.cy - 88)
            y2 = max(y1 + 1, t.cy - 24)
            x1 = max(0, t.cx - 8)
            x2 = min(1280, t.cx + 9)
            strip = img[y1:y2, x1:x2].astype(np.float32)
            blue_diff = float((strip[:, :, 0] - strip[:, :, 2]).mean())
            bright_blue = float(((strip[:, :, 0] > 200) & ((strip[:, :, 0] - strip[:, :, 2]) > 25)).mean())

            # 2. 正下方黑色基础产出数字像素数 (正常雪地很亮，数字很黑)
            dy1 = min(719, t.cy + 17)
            dy2 = min(720, t.cy + 44)
            dx1 = max(0, t.cx - 16)
            dx2 = min(1280, t.cx + 17)
            sub_g = gray[dy1:dy2, dx1:dx2]
            dark_cnt = int((sub_g < 85).sum())
            has_digit_bonus = 50.0 if 14 <= dark_cnt <= 220 else 0.0

            score = blue_diff + bright_blue * 80.0 + has_digit_bonus
            if score > best_score:
                best_score = score
                best_idx = t.idx

        # 直接用 text_recognizer 读取当前站位下方的产出数字（跳过 DBNet，单数字极准）
        pt = tiles[best_idx]
        dy1 = max(0, pt.cy + 15)
        dy2 = min(720, pt.cy + 48)
        dx1 = max(0, pt.cx - 22)
        dx2 = min(1280, pt.cx + 23)
        crop = img[dy1:dy2, dx1:dx2]
        cg = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        # 优先试干净二值化 + 白边，再回退原图
        _, bw = cv2.threshold(cg, 130, 255, cv2.THRESH_BINARY)
        bw_pad = cv2.copyMakeBorder(bw, 10, 10, 28, 28, cv2.BORDER_CONSTANT, value=255)
        bw_bgr = cv2.cvtColor(bw_pad, cv2.COLOR_GRAY2BGR)
        raw_pad = cv2.copyMakeBorder(crop, 6, 6, 20, 20, cv2.BORDER_REPLICATE)

        detected_yield: Optional[int] = None
        for cand_img in (bw_bgr, raw_pad, crop):
            txt = self._rec_single(cand_img)
            m = re.search(r"\d+", txt)
            if m:
                val = int(m.group(0))
                if 0 <= val <= 30:
                    detected_yield = val
                    break
        return best_idx, detected_yield

    def _detect_cards(self, img: np.ndarray) -> Tuple[List[int], List[dict], Optional[int]]:
        """
        检测左下角全部手牌（支持0~9张，多尺度兼容模拟器1.0x与PC端0.90x缩放）及其 COMBO+ 标记。
        返回: (hand_values, card_infos, inferred_last_collect)
        """
        y_off = 505
        roi = cv2.cvtColor(img[y_off:708, 0:935], cv2.COLOR_BGR2GRAY)
        cands = []
        for scale in (1.0, 0.90):
            for d, tpl in self.card_tpls.items():
                rt = tpl if scale == 1.0 else cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                th, tw = rt.shape[:2]
                if roi.shape[0] < th or roi.shape[1] < tw:
                    continue
                res = cv2.matchTemplate(roi, rt, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(res > 0.72)
                for y, x in zip(ys, xs):
                    cands.append((float(res[y, x]), d, int(x + tw // 2), int(y + y_off + th // 2)))
        cands.sort(reverse=True)
        picked = []
        for sc, d, cx, cy in cands:
            if all(abs(cx - px) > 50 for _, _, px, _ in picked):
                # 验证确实位于暗色卡片上（防止误检背景）
                patch = img[max(0, cy - 60) : min(720, cy + 40), max(0, cx - 28) : min(1280, cx + 28)]
                pg = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
                if (pg < 95).mean() > 0.35:
                    picked.append((sc, d, cx, cy))
        picked.sort(key=lambda x: x[2])

        hand: List[int] = []
        infos: List[dict] = []
        inferred_last_collect: Optional[int] = None

        for sc, d, cx, cy in picked:
            # 检测卡片顶部是否有蓝紫色 COMBO+ 标签（兼容模拟器 1.0x 与 PC 端 0.90x 缩放）
            ty1 = max(0, cy - 135)
            ty2 = max(ty1 + 10, cy - 68)
            top_p = img[ty1:ty2, max(0, cx - 42) : min(1280, cx + 42)]
            b, g, r = cv2.split(top_p)
            combo_blue = (b.astype(int) > 165) & (r.astype(int) < 115) & ((b.astype(int) - r.astype(int)) > 65)
            is_combo = int(combo_blue.sum()) > 95
            if is_combo and d >= 2:
                inferred_last_collect = d - 1
            hand.append(d)
            infos.append({"val": d, "cx": cx, "cy": cy - 10, "score": round(sc, 3), "combo": is_combo})

        return hand, infos, inferred_last_collect

    def _detect_quests(self, img: np.ndarray, res_set_key: str) -> List[QuestState]:
        """自适应识别右上角 3 条重建计划（同时兼容模拟器与 PC 端偏移布局）。"""
        allowed = RESOURCE_SETS[res_set_key]
        rewards = [4, 1, 1]

        # 全扫描右上角 3 条计划面板 [65:285, 960:1275]
        x0, y0, scale = 960, 65, 1.5
        panel = img[y0:285, x0:1275]
        res, _ = self.ocr(cv2.resize(panel, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))

        # 收集所有 "cur/target" 文本框 (left_x, cx, cy, cur, target)
        found_items = []
        for box, txt, _ in (res or []):
            clean = txt.replace(" ", "")
            m = re.search(r"(\d+)/(\d+)", clean)
            if not m:
                continue
            cur_v, tgt_v = int(m.group(1)), int(m.group(2))
            if tgt_v <= 0:
                continue
            lx = int(min(box[0][0], box[3][0]) / scale) + x0
            cx = int((box[0][0] + box[2][0]) / (2 * scale)) + x0
            cy = int((box[0][1] + box[2][1]) / (2 * scale)) + y0
            found_items.append((lx, cx, cy, cur_v, tgt_v))

        # 按 y 分配到第 0/1/2 条计划（模拟器 y≈100/174/249，PC端 y≈90/157/224）
        rows: List[List[tuple]] = [[], [], []]
        for item in found_items:
            cy = item[2]
            if cy < 126:
                rows[0].append(item)
            elif cy < 194:
                rows[1].append(item)
            else:
                rows[2].append(item)

        quests: List[QuestState] = []
        for row_i in range(3):
            row_entries = sorted(rows[row_i], key=lambda it: it[0])[:2]
            items: List[QuestItem] = []
            for lx, cx, cy, cur_val, tgt_val in row_entries:
                # 图标紧贴数字框左侧: [lx-56 : lx+2, cy-18 : cy+28]
                ix1 = max(0, lx - 56)
                ix2 = min(1280, lx + 2)
                iy1 = max(0, cy - 18)
                iy2 = min(720, cy + 28)
                ic_roi = cv2.cvtColor(img[iy1:iy2, ix1:ix2], cv2.COLOR_BGR2GRAY)
                best_r = allowed[0]
                best_sc = -1.0
                for cand_r in allowed:
                    tpl_raw = self.quest_tpls[cand_r]
                    for t_sc in (1.0, 0.90):
                        tpl = tpl_raw if t_sc == 1.0 else cv2.resize(tpl_raw, None, fx=t_sc, fy=t_sc, interpolation=cv2.INTER_AREA)
                        if ic_roi.shape[0] >= tpl.shape[0] and ic_roi.shape[1] >= tpl.shape[1]:
                            m_sc = float(cv2.matchTemplate(ic_roi, tpl, cv2.TM_CCOEFF_NORMED).max())
                            if m_sc > best_sc:
                                best_sc = m_sc
                                best_r = cand_r
                items.append(QuestItem(res=best_r, cur=min(cur_val, tgt_val), target=tgt_val))

            # 若两条槽位识别出相同资源(仅当其中一项已打勾覆盖图标)，分配另一个不冲突资源
            if len(items) == 2 and items[0].res == items[1].res:
                other = [r for r in allowed if r != items[0].res][0]
                if items[0].cur >= items[0].target:
                    items[0].res = other
                elif items[1].cur >= items[1].target:
                    items[1].res = other

            quests.append(QuestState(reward=rewards[row_i], items=items))
        return quests

    def _detect_header_and_turn(self, img: np.ndarray) -> Tuple[int, int, int, int, Tuple[int, int]]:
        """识别 (cur_pts, target_pts, cur_turn, max_turns, end_turn_xy)，同时兼容模拟器与 PC 端。"""
        # 1. 右上角重建值: 定位"至少获得X重建值"的精确右边界，再紧贴其右侧裁出纯净的 cur/target
        hdr = img[8:68, 950:1265]
        scale = 2.0
        hdr_res, _ = self.ocr(cv2.resize(hdr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))

        target_pts = 10
        cur_pts = 0
        left_x = 1144
        hdr_target = None
        for box, txt, _ in (hdr_res or []):
            if "重建" in txt or "至少" in txt or "获得" in txt:
                bx_right = int(max(box[1][0], box[2][0]) / scale) + 950
                left_x = max(1100, min(1195, bx_right + 3))
                m_tgt = re.search(r"(\d+)", txt)
                if m_tgt:
                    hdr_target = int(m_tgt.group(1))
                    target_pts = hdr_target
                break

        pair_crop = img[8:64, left_x:1236]
        raw_pair = self._rec_single(pair_crop).replace(" ", "")
        m_pair = re.search(r"(\d+)/(\d+)", raw_pair)
        if m_pair:
            v_cur = int(m_pair.group(1))
            if 0 <= v_cur <= 55:
                cur_pts = v_cur
            if hdr_target is None and int(m_pair.group(2)) <= 20:
                target_pts = int(m_pair.group(2))
        else:
            # 兜底: 若斜杠被吞或误识成 '1' (如 '9/4' -> '914' -> prefix='91')
            digits = re.sub(r"\D", "", raw_pair)
            suf = str(target_pts)
            if digits.endswith(suf) and len(digits) > len(suf):
                prefix = digits[: -len(suf)]
                if len(prefix) >= 2 and prefix.endswith("1") and int(prefix) > 45:
                    prefix = prefix[:-1]
                if prefix.isdigit() and 0 <= int(prefix) <= 55:
                    cur_pts = int(prefix)
            elif digits.isdigit() and 0 <= int(digits) <= 55:
                cur_pts = int(digits)

        # 2. 右侧回合数与「结束回合」按钮位置: 动态扫描 [305:475, 1085:1245]
        cur_turn, max_turns = 1, 8
        end_turn_xy = (1158, 425)
        mid_x0, mid_y0 = 1085, 305
        mid_items = self._ocr_text(img[mid_y0:475, mid_x0:1245], scale=1.5)
        end_y = None
        for (mx, my), txt in mid_items:
            if "结束" in txt or "回合" in txt:
                end_y = my + mid_y0
                end_turn_xy = (mx + mid_x0, end_y)
                break

        # 紧贴「结束回合」上方精准裁出回合数字框，优先采用多字号更稳的 mid_items，再回退 text_recognizer + 二值化
        turn_y2 = (end_y - 18) if end_y else 388
        turn_y1 = max(295, turn_y2 - 72)

        def _parse_turn_str(s: str) -> Optional[Tuple[int, int]]:
            s = s.replace(" ", "").replace("l", "/").replace("I", "/")
            m = re.search(r"(\d+)\s*/\s*(\d+)", s)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if b in (6, 8, 10) and 1 <= a <= b:
                    return a, b
            digits = re.sub(r"\D", "", s)
            for cand_max in (10, 8, 6):
                suf = str(cand_max)
                if digits.endswith(suf) and len(digits) >= len(suf) + 1:
                    prefix = digits[: -len(suf)]
                    if len(prefix) >= 2 and prefix.endswith("1") and int(prefix) > cand_max:
                        prefix = prefix[:-1]
                    if prefix.isdigit() and 1 <= int(prefix) <= cand_max:
                        return int(prefix), cand_max
            return None

        parsed_turn = None
        # 优先看 mid_items 中位于「结束回合」上方的文本块（DBNet+Rec 对大分子+小分母更稳）
        for (mx, my), txt in mid_items:
            ay = my + mid_y0
            if turn_y1 - 10 <= ay <= turn_y2 + 6 and ("结束" not in txt and "回合" not in txt):
                parsed_turn = _parse_turn_str(txt)
                if parsed_turn:
                    break

        if parsed_turn is None:
            t_crop = img[turn_y1:turn_y2, 1105:1215]
            parsed_turn = _parse_turn_str(self._rec_single(t_crop))
        if parsed_turn is None:
            t_crop = img[turn_y1:turn_y2, 1105:1215]
            tg = cv2.cvtColor(t_crop, cv2.COLOR_BGR2GRAY)
            _, tbw = cv2.threshold(tg, 125, 255, cv2.THRESH_BINARY)
            tbw_bgr = cv2.cvtColor(cv2.copyMakeBorder(tbw, 10, 10, 16, 16, cv2.BORDER_CONSTANT, value=255), cv2.COLOR_GRAY2BGR)
            parsed_turn = _parse_turn_str(self._rec_single(tbw_bgr))

        if parsed_turn is not None:
            cur_turn, max_turns = parsed_turn

        return cur_pts, target_pts, cur_turn, max_turns, end_turn_xy

    def _detect_top_effect(self, img: np.ndarray) -> str:
        """匹配顶部中央场地效果图标 (x:622..658, y:8..44)，正反像双匹配兼容满充能反色状态。"""
        if not self.top_tpls:
            return ""
        roi = cv2.cvtColor(img[8:44, 622:658], cv2.COLOR_BGR2GRAY)
        roi_inv = 255 - roi
        best_name, best_sc = "", 0.0
        for name, tpl in self.top_tpls.items():
            if roi.shape[0] >= tpl.shape[0] and roi.shape[1] >= tpl.shape[1]:
                s1 = float(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED).max())
                s2 = float(cv2.matchTemplate(roi_inv, tpl, cv2.TM_CCOEFF_NORMED).max())
                sc = max(s1, s2)
                if sc > best_sc:
                    best_sc = sc
                    best_name = name
        return best_name if best_sc >= 0.78 else ""

    def _infer_stage_id(
        self,
        res_set_key: str,
        max_turns: int,
        target_pts: int,
        tiles: List[TileState],
        hand: List[int],
        detected_yield: Optional[int],
        top_effect: str,
    ) -> int:
        has_x2 = any(t.mult > 1 for t in tiles)
        has_lock = any(t.locked for t in tiles)
        has_chest = any(t.chest for t in tiles)
        hand_count = len(hand)

        if res_set_key == "early":
            if max_turns == 6 or target_pts == 4:
                return 1
            if top_effect == "top_lap":
                return 5
            if has_lock or top_effect == "top_lock":
                return 4
            if has_x2 or top_effect == "top_x2":
                return 2
            return 3
        else:
            if max_turns == 8 or target_pts == 10:
                if has_chest or top_effect == "top_chest":
                    return 6
                if top_effect == "top_hand" or hand_count != 5:
                    return 7
                return 6
            else:
                if has_chest or top_effect == "top_chest":
                    return 10
                if has_lock or top_effect == "top_lock":
                    return 9
                return 8

    def detect_stage_select_screen(self, img: np.ndarray) -> Optional[int]:
        """
        检测当前是否处于选关主界面（包含「开始重建」或「继续重建」），
        若是，通过左下角关卡名称精确返回 stage_id (1..10)，否则返回 None。
        """
        # 选关界面右下角文字区 [595:670, 1020:1240]
        rb = self._ocr_text(img[595:670, 1020:1240], scale=1.5)
        rb_txt = "".join(t for _, t in rb)
        if "重建" not in rb_txt:
            return None
        # 读取左下角关卡标题区 [330:520, 60:380] (兼容第8~10关复合效果标题上移)
        lt = self._ocr_text(img[330:520, 60:380], scale=1.5)
        lt_txt = "".join(t for _, t in lt)
        # 按关卡名关键词精确匹配
        name_keys = [
            (1, "地基"), (2, "物资"), (3, "额外"), (4, "灵活"), (5, "熟能"),
            (6, "雪地"), (7, "细致"), (8, "联合"), (9, "竣工"), (10, "重建家园"),
        ]
        for sid, kw in name_keys:
            if kw in lt_txt:
                return sid
        # 兜底: 若 OCR 漏字，按“家园”匹配第10关
        if "家园" in lt_txt:
            return 10
        return None

    def detect_result_screen(self, img: np.ndarray) -> Optional[str]:
        """
        检测是否已经到达通关结算画面。
        返回结算摘要文本，未到达则返回 None。
        """
        # 局内左上角固定有「暂离」按钮 [20:75, 45:135]
        tl = self._ocr_text(img[20:75, 45:145], scale=1.5)
        tl_txt = "".join(t for _, t in tl)
        if "暂离" in tl_txt:
            return None
        # 扫描中央和下方文字检查是否结算画面
        mid = self._ocr_text(img[120:650, 200:1080], scale=1.0)
        joined = " ".join(t for _, t in mid)
        for kw in ("春日必然到来", "雪境生存哲学", "重建完成", "重建失败", "本次重建", "SPRING", "WE MADE IT", "历史最高", "最终重建"):
            if kw in joined:
                return joined[:120]
        return None

    def parse_result_score(self, img: np.ndarray, target_pts: int, res_txt: str = "") -> Optional[int]:
        """从结算画面中央大号红色进度数字框 [320:440, 420:620] 精确读取最终重建值（如 15/4 -> 15）。"""
        for cand_str in (
            self._rec_single(img[320:440, 420:620]),
            res_txt,
        ):
            clean = (cand_str or "").replace(" ", "")
            m = re.search(r"(\d+)\s*/\s*(\d+)", clean)
            if m:
                p, t = int(m.group(1)), int(m.group(2))
                if t == target_pts and 0 <= p <= 55:
                    return p
        return None

    def detect_confirm_red_dialog(self, img: np.ndarray) -> Tuple[bool, Tuple[int, int]]:
        """
        极速零误判检测「左灰取消 + 右红确认放弃」全宽确认弹窗（同时覆盖提前结束回合弹窗与放弃重建弹窗）。
        实测：弹窗时右半砖红条占比 ~0.82，正常画面为 0.000。
        返回: (is_present, (confirm_x, confirm_y))
        """
        roi_r = img[495:565, 660:1240]
        b, g, r = cv2.split(roi_r)
        red_mask = (r.astype(int) > 125) & ((r.astype(int) - g.astype(int)) > 45) & ((r.astype(int) - b.astype(int)) > 45)
        red_ratio = float(red_mask.mean())
        if red_ratio < 0.35:
            return False, (960, 530)

        # 计算红条纵向中心 y，确保精准点中大红条正中（结束回合弹窗 y≈522，放弃重建弹窗 y≈540）
        ys = np.where(red_mask)[0]
        cy = int(round(495 + ys.mean())) if len(ys) > 0 else 530
        return True, (960, cy)

    def detect_abandon_turn_dialog(self, img: np.ndarray) -> bool:
        """检测提前结束回合时弹出的「当前回合还有修缮规划可使用，是否放弃并进入下一回合？」确认框。"""
        has_red, _ = self.detect_confirm_red_dialog(img)
        if has_red:
            return True
        mid = self._ocr_text(img[290:565, 300:1150], scale=1.5)
        txt = "".join(t for _, t in mid)
        return ("确认放弃" in txt) or ("下一回合" in txt) or ("不再提示" in txt)

    def recognize(self, img: np.ndarray, stage_hint: Optional[int] = None) -> Tuple[GameState, dict]:
        """从 1280x720 BGR 截图中完整解析 GameState 与视觉元数据。"""
        tiles, res_set_key = self._detect_nodes(img)
        pos, detected_yield = self._detect_pos_and_yield(img, tiles)
        hand, card_infos, inferred_last_col = self._detect_cards(img)
        quests = self._detect_quests(img, res_set_key)
        cur_pts, target_pts, cur_turn, max_turns, end_turn_xy = self._detect_header_and_turn(img)
        top_effect = self._detect_top_effect(img)

        stage_id = stage_hint if (stage_hint in STAGES) else self._infer_stage_id(
            res_set_key, max_turns, target_pts, tiles, hand, detected_yield, top_effect
        )
        s_meta = STAGES[stage_id]
        # 强制使用关卡静态总回合数与目标重建值，彻底杜绝 OCR 将 /8 误读为 /6 导致提前进入末回合冲刺
        max_turns = s_meta["max_turns"]
        target_pts = s_meta["target_pts"]
        cur_turn = max(1, min(cur_turn, max_turns))

        # 处理基础产出: 封锁生效期(第4关=4, 第9关=7)直接锁准；其余若单字 OCR 漏读则先回退 init_base（AutoRunner 会用上步已知产出兜底）
        yield_warning = None
        any_locked = any(t.locked for t in tiles)
        if tiles[pos].locked or detected_yield is None or detected_yield == 0:
            if tiles[pos].locked or (s_meta.get("has_lock") and any_locked):
                base_yield = s_meta.get("lock_base", s_meta["init_base"] + 2)
            else:
                base_yield = s_meta["init_base"]
                yield_warning = f"未能清晰识别当前基础产出，已回退默认 {base_yield}"
        else:
            base_yield = detected_yield

        # 若未检测到 COMBO+，则默认回合初 can_collect=False（若用户已前进过但未采，可在前端一键勾选）
        can_collect = False
        last_collect = inferred_last_col

        state = GameState(
            stage_id=stage_id,
            res_types=list(RESOURCE_SETS[res_set_key]),
            tiles=tiles,
            pos=pos,
            base_yield=base_yield,
            hand=hand,
            quests=quests,
            cur_turn=cur_turn,
            max_turns=max_turns,
            cur_pts=cur_pts,
            target_pts=target_pts,
            can_collect=can_collect,
            last_collect=last_collect,
            lap_bonus=s_meta["lap_bonus"],
            hand_scale=s_meta["hand_scale"],
            chest_amount=s_meta.get("chest_amount", 15),
        )

        meta = {
            "card_infos": card_infos,
            "detected_yield": detected_yield,
            "yield_warning": yield_warning,
            "res_set_key": res_set_key,
            "end_turn_xy": end_turn_xy,
        }
        return state, meta


if __name__ == "__main__":
    col = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
    if not os.path.isdir(col):
        print(f"用法: python vision.py <截图目录>（未找到目录: {col}）")
        sys.exit(0)
    rec = BoardRecognizer()
    tests = sorted(f for f in os.listdir(col) if f.lower().endswith((".png", ".jpg")))
    for fn in tests:
        img = cv2.imread(os.path.join(col, fn))
        if img is None:
            continue
        try:
            st, meta = rec.recognize(img)
        except Exception as exc:
            print(f"{fn:20s} | SKIP: {exc}")
            continue
        q_desc = [
            f"x{q.reward}[" + ",".join(f"{it.res}:{it.cur}/{it.target}" for it in q.items) + "]"
            for q in st.quests
        ]
        spec = []
        for t in st.tiles:
            tags = []
            if t.mult > 1: tags.append("x2")
            if t.locked: tags.append("LOCK")
            if t.chest: tags.append("CHEST")
            if tags: spec.append(f"{t.idx}({','.join(tags)})")
        print(
            f"{fn:20s} | S{st.stage_id:02d} T{st.cur_turn}/{st.max_turns} pts={st.cur_pts}/{st.target_pts} "
            f"pos={st.pos} yield={st.base_yield} hand={st.hand} last_col={st.last_collect}"
        )
        print(f"    spec={spec} | quests={' | '.join(q_desc)}")
        if meta["yield_warning"]:
            print(f"    WARN: {meta['yield_warning']}")
