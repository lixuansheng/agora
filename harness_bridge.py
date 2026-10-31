"""Loopback-only adapter for the installed DeepSeek Harness 0.2.1 desktop Host."""

from __future__ import annotations

import base64
from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import subprocess
import shutil
import socket
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

import websocket
import yaml
from codex_bridge import RecoveryRequired


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class HarnessError(RuntimeError):
    pass


class HarnessClient:
    def __init__(self, home: str | Path | None = None, url: str = "") -> None:
        self.home = Path(home or os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
        self.manual_url = url.rstrip("/")
        self.url = self.manual_url
        self.discovered_at = 0.0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cookie = ""
        self.cookie_until = 0.0
        self.cancel_event = threading.Event()
        self.active_session: str | None = None
        self.last_usage: list[dict[str, Any]] = []
        self.full_access = False
        self.notice = lambda text: None

    @staticmethod
    def _native_listener_ports():
        """Read Windows TCP ownership directly, without a PowerShell subprocess."""
        if os.name != "nt":
            return []
        api = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
        size = wintypes.DWORD()
        api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        for _ in range(3):
            table = ctypes.create_string_buffer(size.value)
            code = api(table, ctypes.byref(size), False, socket.AF_INET, 3, 0)
            if code == 122:
                continue
            if code:
                return []
            break
        else:
            return []
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        count = min(wintypes.DWORD.from_buffer(table).value, (len(table) - 4) // 24)
        ports, owners = [], {}
        for index in range(count):
            state, address, port, _, _, pid = (wintypes.DWORD * 6).from_buffer(table, 4 + 24 * index)
            if state != 2 or socket.inet_ntoa(address.to_bytes(4, "little")) != "127.0.0.1":
                continue
            if pid not in owners:
                owners[pid] = False
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        image = ctypes.create_unicode_buffer(32768)
                        length = wintypes.DWORD(len(image))
                        if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                            owners[pid] = Path(image.value).name.casefold() == "deepseek harness.exe"
                    finally:
                        kernel.CloseHandle(handle)
            if owners[pid]:
                ports.append(socket.ntohs(port & 65535))
        return sorted(set(ports))

    def discover(self, force=False) -> str:
        if self.manual_url:
            parsed = urllib.parse.urlsplit(self.manual_url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path or parsed.query or parsed.username:
                raise HarnessError("Harness 地址必须为 http://127.0.0.1:端口")
            self.url = self.manual_url
            return self.url
        if not force and self.url and time.monotonic() - self.discovered_at < 5:
            return self.url
        try:
            native_ports = self._native_listener_ports()
        except (OSError, AttributeError, ValueError):
            native_ports = []
        if native_ports:
            discovered = "http://127.0.0.1:" + str(native_ports[0])
            if discovered != self.url:
                self.cookie, self.cookie_until = "", 0.0
            self.url, self.discovered_at = discovered, time.monotonic()
            return self.url
        command = (
            "$ProgressPreference='SilentlyContinue'; $ids=@(Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id); "
            "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
            "Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.OwningProcess -in $ids } | "
            "Select-Object -ExpandProperty LocalPort"
        )
        powershell = shutil.which("pwsh") or Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-EncodedCommand", base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessError("Harness 自动发现失败，请确认 Windows PowerShell 可用，或在设置中填写 Harness 本机地址") from exc
        if result.returncode:
            raise HarnessError("Harness 端口查询失败，请确认桌面应用已打开，或在设置中填写 Harness 本机地址")
        for line in result.stdout.splitlines():
            if line.strip().isdigit():
                discovered = "http://127.0.0.1:" + line.strip()
                if discovered != self.url:
                    self.url = discovered
                    self.cookie = ""
                    self.cookie_until = 0.0
                self.discovered_at = time.monotonic()
                return self.url
        raise HarnessError("未找到正在运行的 DeepSeek Harness，请先打开桌面应用")

    def authenticate(self) -> None:
        self.discover()
        if self.cookie and time.time() < self.cookie_until:
            return
        # Read only the local owner-session grant; provider API keys are never used here.
        path = self.home / ".credentials.yaml"
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            grant = document["records"]["client-connection/browser-session"]
            if grant["kind"] != "grant" or grant["payload"]["version"] != 1:
                raise ValueError("unsupported grant")
            encoded = grant["payload"]["secret"]
            secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise HarnessError("无法使用 Harness 本地会话凭据，请确认应用和 DSH_HOME") from exc
        authority = urllib.parse.urlsplit(self.url).netloc
        now = int(time.time() * 1000)
        body = _b64(json.dumps({
            "version": 1, "authority": authority, "issuedAt": now, "expiresAt": now + 3_600_000,
        }, separators=(",", ":")).encode())
        name = "dsh-auth-" + _b64(hashlib.sha256(authority.encode()).digest())
        signature = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        self.cookie = f"{name}=v1.{body}.{signature}"
        self.cookie_until = time.time() + 3500

    def _rpc_error(self, method, args, detail, uncertain=False):
        if method == "session/prompt" and uncertain:
            request = (args or {}).get("request", {})
            return RecoveryRequired("Harness 提交接收状态不明；未重新发送任务，请核对原对话。"
                f" sessionId={request.get('sessionId', '')} requestId={request.get('requestId', '')}：{detail}")
        return HarnessError(f"Harness {method}: {detail}")

    def rpc(self, method: str, args: dict[str, Any] | None = None) -> Any:
        for attempt in range(2):
            self.authenticate()
            rpc_id = str(uuid.uuid4())
            payload = {"type": "client-request", "rpcId": rpc_id, "method": method, "payload": {"args": args or {}}}
            request = urllib.request.Request(
                self.url + "/api/" + method,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Cookie": self.cookie, "Content-Type": "application/json"},
            )
            try:
                with self.opener.open(request, timeout=45) as response:
                    envelope = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self.cookie = ""
                    self.cookie_until = 0.0
                    self.discover(force=True)
                    continue
                raise self._rpc_error(method, args, f"HTTP {exc.code}", uncertain=exc.code >= 500 or exc.code == 408) from None
            except (OSError, ValueError, http.client.HTTPException) as exc:
                # Only read requests may be replayed after an ambiguous network failure.
                if attempt == 0 and not self.manual_url and method in {"session/list", "session/modelCatalog"}:
                    self.discover(force=True)
                    continue
                if not self.manual_url:
                    self.discovered_at = 0.0
                raise self._rpc_error(method, args, "本地服务未响应", uncertain=True) from exc
        if not isinstance(envelope, dict) or envelope.get("rpcId") != rpc_id or envelope.get("type") != "server-response":
            raise self._rpc_error(method, args, "返回了不匹配的 RPC 响应", uncertain=True)
        result = envelope.get("result", {})
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise self._rpc_error(method, args, "RPC 结果格式无效", uncertain=True)
        if not result["ok"]:
            error = result.get("error", {})
            if not isinstance(error, dict):
                raise self._rpc_error(method, args, "RPC 错误格式无效", uncertain=True)
            raise HarnessError(f"Harness {method}: {error.get('code', '')} {error.get('message', '请求失败')}")
        return result.get("value")

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.rpc("session/list", {"_request": {}}).get("items", [])
        return [row for row in rows if not row.get("parentSessionId") and row.get("origin") != "subagent"]

    def models(self) -> dict[str, Any]:
        return self.rpc("session/modelCatalog")

    def create(self, cwd: str, title: str = "") -> str:
        session_id = self.rpc("session/create", {"request": {"cwd": cwd}})["sessionId"]
        if title:
            self.rpc("session/rename", {"request": {"sessionId": session_id, "title": title[:120]}})
        return session_id

    def follow(self, session_id: str) -> tuple[Any, str, dict[str, Any]]:
        self.authenticate()
        connection = websocket.create_connection(
            self.url.replace("http://", "ws://", 1) + "/api/remote.mux",
            cookie=self.cookie, timeout=15, suppress_origin=True, http_no_proxy=["127.0.0.1"],
        )
        stream_id = str(uuid.uuid4())
        try:
            connection.send(json.dumps({
                "type": "open", "streamId": stream_id, "endpoint": "session/follow",
                "payload": {"args": {"request": {"address": {"kind": "session", "sessionId": session_id}, "maxMessages": 30}}},
            }))
            while True:
                frame = json.loads(connection.recv())
                if frame.get("streamId") != stream_id:
                    continue
                if frame.get("type") == "error":
                    raise HarnessError(str(frame.get("error", {}).get("message", "follow 失败")))
                value = frame.get("value", {})
                if value.get("type") == "snapshot":
                    connection.settimeout(1)
                    return connection, stream_id, value
        except Exception:
            connection.close()
            raise

    def history(self, session_id: str) -> list[dict[str, Any]]:
        connection, _, snapshot = self.follow(session_id)
        connection.close()
        return [record["event"] for record in snapshot.get("records", []) if record.get("type") == "event"]

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.active_session:
            self.rpc("session/cancel", {"request": {"sessionId": self.active_session}})

    def ask(self, session_id: str, text: str, selection: dict[str, str] | None = None,
            progress: Callable[[str], None] | None = None, timeout: float = 900, readonly: bool = False) -> str:
        if self.cancel_event.is_set():
            raise HarnessError("任务已停止")
        row = next((row for row in self.list_sessions() if row["sessionId"] == session_id), None)
        if not row:
            raise HarnessError("找不到所选 Harness 对话")
        if row.get("running"):
            raise HarnessError("所选 Harness 对话正在运行，请等待完成或选择新对话")
        current = row.get("projections", {}).get("values", {}).get("modelSelection", {})
        if selection and any(current.get(key) != value for key, value in selection.items()):
            self.rpc("session/selectModel", {"request": {"sessionId": session_id, **selection}})
        if readonly:
            result = self.rpc("commands/execute", {"agentId": session_id,
                "line": "/permission read-only", "submittedAttachments": []})
            checked = next((item for item in self.list_sessions() if item["sessionId"] == session_id), {})
            preset = checked.get("projections", {}).get("values", {}).get("permissions", {}).get("currentValue")
            if not result or result.get("result", {}).get("kind") == "error" or preset != "read-only":
                raise HarnessError("Harness 未能确认只读权限，未发送并行核证任务")
        elif self.full_access:
            permissions = row.get("projections", {}).get("values", {}).get("permissions", {})
            if permissions.get("currentValue") != "danger-full-access":
                result = self.rpc("commands/execute", {"agentId": session_id,
                    "line": "/permission danger-full-access", "submittedAttachments": []})
                if not result or result.get("result", {}).get("kind") == "error":
                    raise HarnessError("Harness 无法设置完全权限，未发送模型任务")
                checked = next((item for item in self.list_sessions() if item["sessionId"] == session_id), {})
                preset = checked.get("projections", {}).get("values", {}).get("permissions", {}).get("currentValue")
                if preset != "danger-full-access":
                    raise HarnessError("Harness 完全权限未生效，未发送模型任务")
        connection, stream_id, snapshot = self.follow(session_id)
        request_id = str(uuid.uuid4())
        self.active_session = session_id
        messages: list[str] = []
        self.last_usage = []
        admitted = False
        active_turn = None
        observed_turn = None
        last_seq = max((row.get("event", {}).get("seq", -1) for row in snapshot.get("records", [])
                        if isinstance(row.get("event", {}).get("seq", -1), int)), default=-1)
        seen_events = set()
        try:
            if self.cancel_event.is_set():
                raise HarnessError("任务已停止")
            self.rpc("session/prompt", {"request": {
                "requestId": request_id, "sessionId": session_id, "mode": "queue",
                "content": [{"type": "text", "text": text}], "clientTimeZone": "Asia/Shanghai",
            }})
            last_activity = time.monotonic()
            reconnects, replay = 0, deque()
            while time.monotonic() - last_activity < timeout:
                if self.cancel_event.is_set():
                    self.rpc("session/cancel", {"request": {"sessionId": session_id}})
                    raise HarnessError("任务已停止")
                try:
                    if replay:
                        frame = {"streamId": stream_id, "value": replay.popleft()}
                    else:
                        raw = connection.recv()
                        if not raw:
                            raise websocket.WebSocketConnectionClosedException("stream closed")
                        frame = json.loads(raw)
                except websocket.WebSocketTimeoutException:
                    continue
                except (websocket.WebSocketException, OSError) as exc:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    recovery_error = exc
                    while reconnects < 3:
                        reconnects += 1
                        self.notice(f"Harness 消息流中断，正在第 {reconnects}/3 次连接原请求，不重复发送任务。")
                        if self.cancel_event.wait(.25 * 2 ** (reconnects - 1)):
                            raise HarnessError("任务已停止")
                        try:
                            connection, stream_id, snapshot = self.follow(session_id)
                            break
                        except (websocket.WebSocketException, OSError, HarnessError) as error:
                            recovery_error = error
                    else:
                        raise RecoveryRequired("Harness 消息流连续恢复失败，未重新发送任务，请查看原对话。") from recovery_error
                    replay.extend(snapshot.get("records", []))
                    continue
                if frame.get("streamId") != stream_id:
                    continue
                if frame.get("type") == "error":
                    raise HarnessError(str(frame.get("error", {}).get("message", "消息流失败")))
                value = frame.get("value", {})
                if value.get("type") != "event":
                    continue
                event = value["event"]
                seq = event.get("seq")
                if isinstance(seq, int):
                    if seq <= last_seq:
                        continue
                    last_seq = seq
                else:
                    fingerprint = json.dumps(event, sort_keys=True)
                    if fingerprint in seen_events:
                        continue
                    seen_events.add(fingerprint)
                kind = event["type"]
                data = event.get("data", {})
                # Host emits turn/start before admitting that turn's user message.
                if kind == "turn/start":
                    observed_turn = data.get("turn")
                if kind == "user/message":
                    source = data.get("source", {})
                    if source.get("rpcId") == request_id or source.get("requestId") == request_id or data.get("requestId") == request_id:
                        if observed_turn is None or (active_turn is not None and active_turn != observed_turn):
                            raise RecoveryRequired("Harness 当前请求无法关联到已开始的回合，请核对原对话，未重新发送。")
                        admitted = True
                        active_turn = observed_turn
                    elif source.get("kind") == "user":
                        raise RecoveryRequired("该 Harness 对话收到其他输入，请在原应用确认结果")
                if admitted and data.get("turn", observed_turn) == active_turn:
                    last_activity = time.monotonic()
                    reconnects = 0
                if kind == "assistant/message" and admitted:
                    if data.get("turn") != active_turn:
                        continue
                    if data.get("usage"):
                        self.last_usage.append(data["usage"])
                    result = assistant_text(data)
                    if result:
                        messages.append(result)
                        if progress:
                            progress(result)
                if kind == "turn/end":
                    if data.get("turn") == observed_turn:
                        observed_turn = None
                    if not admitted or data.get("turn") != active_turn:
                        continue
                    reason = data.get("reason", {})
                    reason = reason.get("kind") if isinstance(reason, dict) else reason
                    if reason != "completed":
                        raise HarnessError("Harness 回合未完成，请查看原对话")
                    if messages:
                        return messages[-1]
                    raise HarnessError("Harness 回合结束，但没有收到文本回复")
            try:
                self.rpc("session/cancel", {"request": {"sessionId": session_id}})
            finally:
                raise RecoveryRequired("Harness 长时间无进展，已发送停止请求；未重新发送任务，请核对原对话。")
        except RecoveryRequired as exc:
            if f"requestId={request_id}" in str(exc):
                raise
            raise RecoveryRequired(f"{exc} sessionId={session_id} requestId={request_id}") from exc
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RecoveryRequired("Harness 已提交后的消息流格式异常，未重新发送任务；请核对原对话。"
                f" sessionId={session_id} requestId={request_id}") from exc
        finally:
            self.active_session = None
            try:
                connection.send(json.dumps({"type": "cancel", "streamId": stream_id}))
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass


def assistant_text(data: dict[str, Any]) -> str:
    """Extract visible text only; reasoning and tool arguments are deliberately excluded."""
    message = data.get("message", data)
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    parts = [part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
    return "".join(parts)
