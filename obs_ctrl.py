# -*- coding: utf-8 -*-
"""
OBS Studio WebSocket v5 轻量控制器 (兼容 OBS 28+ 内置 WebSocket)
用于实现「每局自动启停录制 + 自动珍藏破纪录最高分视频 + 自动清理废片」。
"""

from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple
import websockets


class ObsController:
    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str = ""):
        self.host = host.strip() or "127.0.0.1"
        self.port = int(port) if port else 4455
        self.password = password or ""

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def _request_async(
        self,
        req_type: str,
        req_data: Optional[Dict[str, Any]] = None,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        async with websockets.connect(self.uri, open_timeout=timeout, close_timeout=2.0) as ws:
            # 1. 接收 Hello (op=0)
            hello_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            hello = json.loads(hello_raw)
            if hello.get("op") != 0:
                raise RuntimeError(f"OBS 握手异常: {hello}")

            d = hello.get("d", {})
            identify_d: Dict[str, Any] = {
                "rpcVersion": d.get("rpcVersion", 1),
                "eventSubscriptions": 0,
            }
            auth_info = d.get("authentication")
            if auth_info:
                salt = auth_info["salt"]
                challenge = auth_info["challenge"]
                secret_hash = hashlib.sha256((self.password + salt).encode("utf-8")).digest()
                secret_b64 = base64.b64encode(secret_hash).decode("ascii")
                auth_hash = hashlib.sha256((secret_b64 + challenge).encode("utf-8")).digest()
                identify_d["authentication"] = base64.b64encode(auth_hash).decode("ascii")

            # 2. 发送 Identify (op=1)
            await ws.send(json.dumps({"op": 1, "d": identify_d}))

            # 3. 接收 Identified (op=2)
            ident_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            ident = json.loads(ident_raw)
            if ident.get("op") != 2:
                raise RuntimeError("OBS 鉴权失败，请检查 WebSocket 密码是否正确")

            # 4. 发送 Request (op=6)
            req_id = uuid.uuid4().hex[:12]
            req_payload: Dict[str, Any] = {
                "requestType": req_type,
                "requestId": req_id,
            }
            if req_data:
                req_payload["requestData"] = req_data
            await ws.send(json.dumps({"op": 6, "d": req_payload}))

            # 5. 等待 RequestResponse (op=7)
            while True:
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(msg_raw)
                if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == req_id:
                    rd = msg["d"]
                    status = rd.get("requestStatus", {})
                    if not status.get("result"):
                        code = status.get("code")
                        comment = status.get("comment", "")
                        raise RuntimeError(f"OBS 请求 {req_type} 失败 (code={code}): {comment}")
                    return rd.get("responseData") or {}

    def request(self, req_type: str, req_data: Optional[Dict[str, Any]] = None, timeout: float = 6.0) -> Dict[str, Any]:
        return asyncio.run(self._request_async(req_type, req_data, timeout=timeout))

    def test_connection(self) -> Dict[str, Any]:
        ver = self.request("GetVersion")
        rec = self.request("GetRecordStatus")
        return {
            "obs_version": ver.get("obsVersion", "unknown"),
            "ws_version": ver.get("obsWebSocketVersion", "5.x"),
            "recording": bool(rec.get("outputActive", False)),
        }

    def start_record(self) -> bool:
        """若当前未在录制则启动录制；返回是否新启动成功。"""
        st = self.request("GetRecordStatus")
        if st.get("outputActive"):
            return False
        self.request("StartRecord")
        return True

    def stop_record(self) -> Optional[str]:
        """停止当前录制并返回刚录完的视频完整路径 outputPath。"""
        st = self.request("GetRecordStatus")
        if not st.get("outputActive"):
            return None
        res = self.request("StopRecord", timeout=10.0)
        return res.get("outputPath")


def finalize_recording_file(
    output_path: Optional[str],
    keep: bool,
    new_stem: str = "",
    delete_prev_path: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """
    在 OBS 停止录制后处理视频文件：
    - keep=True: 等待 OBS 释放文件句柄，重命名为带关卡与分数的清晰文件名；如提供 delete_prev_path 则顺便删掉旧的低分录像。
    - keep=False: 等待句柄释放后自动删除未破纪录的废片。
    返回: (final_saved_path_or_None, status_note)
    """
    if not output_path:
        return None, "OBS 未返回视频路径"

    # 等待 OBS 完成封装并释放文件锁（最多 6 秒）
    for _ in range(24):
        if os.path.exists(output_path):
            try:
                with open(output_path, "rb+"):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            time.sleep(0.25)

    if not os.path.exists(output_path):
        return None, f"未找到录像文件: {output_path}"

    if not keep:
        for _ in range(12):
            try:
                os.remove(output_path)
                return None, "未破纪录，已自动清理本局录像释放空间"
            except OSError:
                time.sleep(0.3)
        return None, f"尝试删除废片被占用: {os.path.basename(output_path)}"

    # 保留并重命名
    folder = os.path.dirname(output_path)
    _, ext = os.path.splitext(output_path)
    safe_stem = "".join(c if c not in r'\/:*?"<>|' else "_" for c in new_stem).strip()
    target_path = os.path.join(folder, f"{safe_stem}{ext}") if safe_stem else output_path
    if target_path != output_path and os.path.exists(target_path):
        target_path = os.path.join(folder, f"{safe_stem}_{int(time.time()) % 1000}{ext}")

    saved_path = output_path
    if target_path != output_path:
        for _ in range(12):
            try:
                os.rename(output_path, target_path)
                saved_path = target_path
                break
            except OSError:
                time.sleep(0.3)

    extra = ""
    if delete_prev_path and delete_prev_path != saved_path and os.path.exists(delete_prev_path):
        try:
            os.remove(delete_prev_path)
            extra = "（已自动替换旧低分录像）"
        except OSError:
            pass

    return saved_path, f"已珍藏高光录像: {os.path.basename(saved_path)}{extra}"
