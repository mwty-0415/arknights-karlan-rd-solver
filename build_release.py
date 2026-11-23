# -*- coding: utf-8 -*-
"""
Windows 免安装发行版打包脚本 (PyInstaller --onedir -> .zip)
运行: python build_release.py
输出: release/Arknights-Karlan-RD-Solver-v1.0.0-win64.zip
"""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
import zipfile

VERSION = "v1.0.0"
APP_NAME = "KarlanRDSolver"
ZIP_NAME = f"Arknights-Karlan-RD-Solver-{VERSION}-win64.zip"

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT, "dist", APP_NAME)
RELEASE_DIR = os.path.join(ROOT, "release")
ZIP_PATH = os.path.join(RELEASE_DIR, ZIP_NAME)

ADMIN_BAT = (
    "@echo off\r\n"
    "net session >nul 2>&1\r\n"
    "if %errorlevel% neq 0 (\r\n"
    "    echo Requesting Administrator privileges for Arknights PC client...\r\n"
    "    powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\"\r\n"
    "    exit /b\r\n"
    ")\r\n"
    "cd /d \"%~dp0\"\r\n"
    f"\"{APP_NAME}.exe\"\r\n"
    "pause\r\n"
)

QUICKSTART_TXT = """============================================================
  明日方舟「喀兰贸易技术研发部」精确策略求解器 & 自动凹分工具
============================================================

【快速启动】
1. PC 端游戏用户（强烈推荐）：
   右键或直接双击 `start_admin_pc.bat`（会自动弹出 UAC 请求管理员权限，
   以便向管理员权限运行的《明日方舟》PC 端发送点击指令）。
2. MuMu 模拟器 ADB 用户 / 仅手动沙盒使用：
   直接双击 `KarlanRDSolver.exe` 即可。

启动后会自动打开浏览器 http://127.0.0.1:5050 。

【常用说明】
- 全局急停热键：挂机过程中随时按键盘 `F10` 或 `End` 键立刻停止并释放鼠标！
- PC 端游戏请保持 16:9 窗口化或无边框（不要最小化或被其他窗口遮挡）。
- 如需自动录制最高分高光视频：在 OBS Studio 中开启「工具 -> WebSocket 服务器设置」，
  在网页右侧填写端口与密码并勾选「启用自动录制」即可。
- 详细说明与源码请见 GitHub 仓库：
  https://github.com/mwty-0415/arknights-karlan-rd-solver
"""


def main():
    if os.path.exists(os.path.join(ROOT, "build")):
        shutil.rmtree(os.path.join(ROOT, "build"))
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        APP_NAME,
        "--add-data",
        f"{os.path.join(ROOT, 'templates')};templates",
        "--add-data",
        f"{os.path.join(ROOT, 'assets')};assets",
        "--collect-all",
        "rapidocr_onnxruntime",
        "--hidden-import",
        "win32gui",
        "--hidden-import",
        "win32con",
        "--hidden-import",
        "mss",
        "--hidden-import",
        "websockets",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
        "--exclude-module", "torch",
        "--exclude-module", "torchvision",
        "--exclude-module", "torchaudio",
        "--exclude-module", "tensorflow",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "sklearn",
        "--exclude-module", "IPython",
        "--exclude-module", "jupyter",
        "--exclude-module", "notebook",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "PySide6",
        "--exclude-module", "sympy",
        "--exclude-module", "transformers",
        "--exclude-module", "triton",
        os.path.join(ROOT, "app.py"),
    ]
    print(">>> Running PyInstaller:")
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    # 写入管理员启动脚本与快速说明（纯 ASCII + CRLF 避免 Windows cmd 编码乱码）
    with open(os.path.join(DIST_DIR, "start_admin_pc.bat"), "wb") as f:
        f.write(ADMIN_BAT.encode("ascii"))
    with open(os.path.join(DIST_DIR, "README_QUICKSTART.txt"), "w", encoding="utf-8") as f:
        f.write(QUICKSTART_TXT)

    os.makedirs(RELEASE_DIR, exist_ok=True)
    if os.path.exists(ZIP_PATH):
        os.remove(ZIP_PATH)

    print(f">>> Creating release archive: {ZIP_PATH}")
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _, filenames in os.walk(DIST_DIR):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arc = os.path.join(APP_NAME, os.path.relpath(full, DIST_DIR))
                zf.write(full, arc)

    size_mb = os.path.getsize(ZIP_PATH) / (1024 * 1024)
    print(f">>> Build succeeded! {ZIP_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
