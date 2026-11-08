"""RFC6455 WebSocket 客户端（纯标准库 socket 实现）。

为什么自己写
------------
部署机 pip 装不了包（见 docs/SHARED_CONTEXT.md），而 Chrome DevTools Protocol
只能走 WebSocket，所以只能自己实现。这里刻意只做**客户端需要的最小子集**：

* 握手（``Sec-WebSocket-Key`` → ``Sec-WebSocket-Accept`` 校验）
* 文本/二进制帧的发送与接收（客户端发送必须加掩码）
* 分片消息重组（CDP 的 ``Runtime.evaluate`` 结果可能很大，Chrome 会分片）
* 控制帧：ping 自动回 pong、close 握手、收到 close 抛异常
* 长度字段三档边界：<=125 用 7 位、<=65535 用 126+16 位、其余用 127+64 位

单测友好的设计
--------------
帧编解码全部是**纯函数**（``build_frame`` / ``parse_frame`` / ``mask_bytes``），
不依赖 socket；socket 只在 ``WebSocketClient`` 里出现。这样本机（沙箱禁止起
子进程、但允许本地 socket）能完整验证协议层。

不确定项
--------
CDP 的 WebSocket 不会发二进制帧（全部是 text），但为了协议完整性仍然实现；
``wss://``（远程调试走 TLS 的场景，例如连到别人的 Chrome）也实现了，但本机
没有验证过真实 TLS 站点，只做了代码路径检查。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CONTROL_OPCODES = frozenset((OP_CLOSE, OP_PING, OP_PONG))
DATA_OPCODES = frozenset((OP_CONTINUATION, OP_TEXT, OP_BINARY))

#: 单条消息上限。CDP 返回大页面 HTML 时可能到几 MB，留 32MB 余量后直接拒绝，
#: 避免内存被恶意/异常服务端打爆。
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
#: 控制帧负载上限由 RFC 规定为 125 字节。
MAX_CONTROL_PAYLOAD = 125

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002


class WebSocketError(Exception):
    """WebSocket 层异常基类。"""


class WebSocketClosed(WebSocketError):
    """对端关闭了连接（或本地已关闭）。"""

    def __init__(self, message: str = "连接已关闭", code: int = CLOSE_NORMAL,
                 reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


class WebSocketTimeout(WebSocketError):
    """等待数据超时。"""


class ProtocolError(WebSocketError):
    """对端行为违反 RFC6455。"""


class IncompleteFrame(WebSocketError):
    """缓冲区里的字节还不够一帧（``parse_frame`` 用它表示「再读一点」）。"""


# --------------------------------------------------------------------------
# 纯函数：掩码 / 帧编解码 / 握手
# --------------------------------------------------------------------------
def mask_bytes(data: bytes, key: bytes) -> bytes:
    """用 4 字节掩码异或 ``data``（RFC6455 5.3）。

    掩码是「可逆的对称操作」，所以同一个函数既用于发送加掩码，也用于接收解掩码。
    """
    if len(key) != 4:
        raise ValueError("WebSocket 掩码必须是 4 字节")
    if not data:
        return b""
    # 逐字节异或太慢（大页面 HTML 有几百 KB），改用「大整数异或」：
    # 等长的大端字节串按位对齐后，整数异或 == 逐字节异或。
    stream = (key * (len(data) // 4 + 1))[: len(data)]
    return _xor_fast(bytes(data), stream)


def _xor_fast(data: bytes, key_stream: bytes) -> bytes:
    return (int.from_bytes(data, "big") ^ int.from_bytes(key_stream, "big")).to_bytes(
        len(data), "big"
    )


def accept_key(client_key: str) -> str:
    """由 ``Sec-WebSocket-Key`` 算出 ``Sec-WebSocket-Accept``。

    RFC6455 的官方测试向量：
    ``dGhlIHNhbXBsZSBub25jZQ==`` → ``s3pPLMBiTxaQ9kYGzzhZRbK+xOo=``
    """
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def new_client_key() -> str:
    """生成 16 字节随机 key（base64）。"""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def build_frame(payload: bytes | str, opcode: int = OP_TEXT, *, fin: bool = True,
                mask: bool = True, mask_key: bytes | None = None) -> bytes:
    """把一段负载编码成 WebSocket 帧。

    ``mask=True`` 是**客户端**行为（RFC6455 规定客户端必须掩码，否则服务端
    会以 1002 断开）。``mask_key`` 只在测试里固定，生产走 ``os.urandom(4)``。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if opcode in CONTROL_OPCODES:
        if len(payload) > MAX_CONTROL_PAYLOAD:
            raise ValueError("控制帧负载不能超过 125 字节")
        if not fin:
            raise ValueError("控制帧不能分片（FIN 必须为 1）")
    if opcode not in CONTROL_OPCODES and opcode not in DATA_OPCODES:
        raise ValueError(f"非法 opcode: {opcode}")

    length = len(payload)
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))

    mask_bit = 0x80 if mask else 0x00
    if length <= 125:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    elif length <= 0xFFFFFFFFFFFFFFFF:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    else:
        raise ValueError("负载过大，无法用 64 位长度字段表示")

    if not mask:
        return bytes(header) + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    if len(key) != 4:
        raise ValueError("掩码长度必须是 4")
    return bytes(header) + key + mask_bytes(payload, key)


def parse_frame(buf: bytes | bytearray | memoryview, *,
                require_mask: bool | None = None) -> tuple["Frame", int]:
    """从缓冲区头部解析一帧，返回 ``(Frame, 消费字节数)``。

    * 字节不够时抛 :class:`IncompleteFrame`（调用方继续读 socket）。
    * ``require_mask=None`` 表示不校验掩码方向；客户端读服务端数据时应传
      ``require_mask=False``（RFC 规定服务端→客户端必须不掩码）。
    """
    if len(buf) < 2:
        raise IncompleteFrame("需要至少 2 字节帧头")

    b0, b1 = buf[0], buf[1]
    fin = bool(b0 & 0x80)
    rsv = b0 & 0x70
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if rsv:
        # 我们没有协商任何扩展（permessage-deflate 等），出现 RSV 位说明对端在
        # 用未协商的扩展 → 按协议错误处理，避免把压缩数据当明文解析出乱码。
        raise ProtocolError(f"未协商的扩展位被置位: RSV=0x{rsv:02x}")
    if opcode not in CONTROL_OPCODES and opcode not in DATA_OPCODES:
        raise ProtocolError(f"非法 opcode: 0x{opcode:x}")

    offset = 2
    if length == 126:
        if len(buf) < offset + 2:
            raise IncompleteFrame("需要 2 字节扩展长度")
        length = struct.unpack_from("!H", buf, offset)[0]
        offset += 2
        if length < 126:
            # RFC 要求最短编码，否则是非法的（可被用于绕过长度检查）
            raise ProtocolError("长度字段未使用最短编码")
    elif length == 127:
        if len(buf) < offset + 8:
            raise IncompleteFrame("需要 8 字节扩展长度")
        length = struct.unpack_from("!Q", buf, offset)[0]
        offset += 8
        if length < 0x10000:
            raise ProtocolError("长度字段未使用最短编码")
        if length >> 63:
            raise ProtocolError("64 位长度最高位必须为 0")

    if opcode in CONTROL_OPCODES:
        if not fin:
            raise ProtocolError("控制帧不能分片")
        if length > MAX_CONTROL_PAYLOAD:
            raise ProtocolError("控制帧负载超过 125 字节")

    if require_mask is not None and masked is not require_mask:
        raise ProtocolError(
            f"掩码方向不符：期望 masked={require_mask}，实际 {masked}"
        )

    key = b""
    if masked:
        if len(buf) < offset + 4:
            raise IncompleteFrame("需要 4 字节掩码")
        key = buf[offset:offset + 4]
        offset += 4

    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"帧负载过大: {length} 字节")

    if len(buf) < offset + length:
        raise IncompleteFrame(f"负载不完整：需要 {offset + length} 字节")

    payload = buf[offset:offset + length]
    if masked:
        payload = mask_bytes(payload, key)
    return Frame(fin=fin, opcode=opcode, payload=bytes(payload), masked=masked), offset + length


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes
    masked: bool = False

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")

    @property
    def is_control(self) -> bool:
        return self.opcode in CONTROL_OPCODES


@dataclass(frozen=True)
class WSURL:
    secure: bool
    host: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        return f"{'https' if self.secure else 'http'}://{self.host}:{self.port}"

    def header_host(self) -> str:
        default = 443 if self.secure else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def parse_ws_url(url: str) -> WSURL:
    """解析 ``ws://host:port/path`` / ``wss://...``。"""
    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ("ws", "wss"):
        raise ValueError(f"不是 WebSocket 地址: {url!r}")
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"WebSocket 地址缺少主机名: {url!r}")
    secure = scheme == "wss"
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return WSURL(secure=secure, host=host, port=int(port), path=path)


def build_handshake_request(url: WSURL, key: str, headers: dict[str, str] | None = None,
                           origin: str | None = None) -> bytes:
    """构造握手请求报文（纯函数，方便单测比对）。

    ``origin`` 默认**不发送**：Chrome 111+ 的 DevTools 端点会校验 ``Origin``，
    带一个不在 ``--remote-allow-origins`` 白名单里的 Origin 会直接被 403 拒绝
    （置信度：高，公开 issue 与 Chrome 源码 net::IsOriginAllowedForDevTools 都可查）。
    不带 Origin 头时 Chrome 视为非浏览器客户端，放行。
    """
    lines = [
        f"GET {url.path} HTTP/1.1",
        f"Host: {url.header_host()}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if origin:
        lines.append(f"Origin: {origin}")
    for name, value in (headers or {}).items():
        if not name or "\n" in str(name) or "\n" in str(value):
            raise ValueError("非法 header")
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def parse_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """解析握手响应，返回 ``(状态码, 头字典, 多余字节)``。

    ``多余字节`` 很重要：服务端可能在同一个 TCP 段里紧跟着发第一帧数据，
    丢掉就会「连上了但收不到第一条消息」。
    """
    marker = raw.find(b"\r\n\r\n")
    if marker < 0:
        raise IncompleteFrame("响应头还不完整")
    head = raw[:marker].decode("iso-8859-1")
    rest = raw[marker + 4:]
    lines = head.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise ProtocolError("不是合法的 HTTP 响应")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise ProtocolError(f"无法解析状态行: {lines[0]!r}") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return status, headers, rest


def verify_handshake(status: int, headers: dict[str, str], key: str) -> None:
    """校验握手响应是否可信（失败抛 :class:`ProtocolError`）。"""
    if status != 101:
        raise ProtocolError(f"握手失败：HTTP {status} {headers.get('reason', '')}".strip())
    if headers.get("upgrade", "").lower() != "websocket":
        raise ProtocolError("响应缺少 Upgrade: websocket")
    if "upgrade" not in headers.get("connection", "").lower():
        raise ProtocolError("响应缺少 Connection: Upgrade")
    expected = accept_key(key)
    if headers.get("sec-websocket-accept", "") != expected:
        raise ProtocolError("Sec-WebSocket-Accept 校验失败（可能不是 WebSocket 服务）")


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class WebSocketClient:
    """同步阻塞的 WebSocket 客户端，够 CDP 用。

    线程模型：**不做内部线程**。CDP 客户端自己起 reader 线程调用 ``recv_text``，
    这里只保证「一个线程读、任意线程写」是安全的（发送加锁）。
    """

    def __init__(self, sock: socket.socket, *, recv_timeout: float = 30.0,
                 buffered: bytes = b"", url: str = "") -> None:
        self._sock = sock
        self._buf = bytearray(buffered)
        self._send_lock = threading.Lock()
        self._closed = False
        self._recv_timeout = float(recv_timeout)
        self.url = url
        self._frag_opcode: int | None = None
        self._frag_buffer = bytearray()

    # ---- 建立 / 关闭 ---------------------------------------------------
    @classmethod
    def connect(cls, url: str, *, timeout: float = 10.0, recv_timeout: float = 30.0,
                headers: dict[str, str] | None = None,
                ssl_context: ssl.SSLContext | None = None) -> "WebSocketClient":
        """完成 TCP 连接 + WebSocket 握手。"""
        target = parse_ws_url(url)
        raw = socket.create_connection((target.host, target.port), timeout=timeout)
        try:
            if target.secure:
                ctx = ssl_context or ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=target.host)
            key = new_client_key()
            request = build_handshake_request(target, key, headers)
            raw.settimeout(timeout)
            raw.sendall(request)
            buffered = b""
            while True:
                chunk = raw.recv(4096)
                if not chunk:
                    raise ProtocolError("握手期间连接被关闭")
                buffered += chunk
                try:
                    status, resp_headers, rest = parse_http_response(buffered)
                except IncompleteFrame:
                    if len(buffered) > 64 * 1024:
                        raise ProtocolError("握手响应头异常大（>64KB）") from None
                    continue
                break
            verify_handshake(status, resp_headers, key)
            sock = WebSocketClient(raw, recv_timeout=recv_timeout, buffered=rest, url=url)
            raw.settimeout(recv_timeout)
            return sock
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    def close_socket(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """发 close 帧并关闭（对端已关闭时静默忽略）。"""
        if self._closed:
            return
        try:
            payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
            self.send_frame(payload, OP_CLOSE)
        except (OSError, WebSocketError):
            pass
        self.close_socket()

    def __enter__(self) -> "WebSocketClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ---- 发送 ----------------------------------------------------------
    def send_frame(self, payload: bytes | str, opcode: int = OP_TEXT, *,
                   fin: bool = True) -> None:
        if self._closed:
            raise WebSocketClosed("连接已关闭，无法发送")
        data = build_frame(payload, opcode, fin=fin, mask=True)
        with self._send_lock:
            self._sock.sendall(data)

    def send_text(self, text: str) -> None:
        self.send_frame(text, OP_TEXT)

    def send_binary(self, data: bytes) -> None:
        self.send_frame(data, OP_BINARY)

    def send_json(self, value: Any) -> None:
        self.send_text(json.dumps(value, ensure_ascii=False))

    def ping(self, payload: bytes = b"") -> None:
        self.send_frame(payload, OP_PING)

    def pong(self, payload: bytes = b"") -> None:
        self.send_frame(payload, OP_PONG)

    # ---- 接收 ----------------------------------------------------------
    def settimeout(self, seconds: float) -> None:
        self._recv_timeout = float(seconds)

    def recv(self, timeout: float | None = None, *, max_control_frames: int = 64) -> tuple[int, bytes]:
        """读一条**完整消息**（自动重组分片、自动回 pong）。

        返回 ``(opcode, payload)``；对端 close 时抛 :class:`WebSocketClosed`。
        ``OP_TEXT``/``OP_BINARY`` 是完整消息；``OP_CLOSE`` 不会返回给调用方。
        """
        limit = self._recv_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, limit)
        control_seen = 0
        while True:
            frame = self._read_frame(deadline)
            if frame.opcode == OP_PING:
                self.pong(frame.payload)
                control_seen += 1
                if control_seen > max_control_frames:
                    raise ProtocolError("短时间内收到过多 ping 帧")
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CLOSE:
                code, reason = _parse_close_payload(frame.payload)
                self._closed = True
                try:
                    # 回一个 close 完成握手（可能已经是对端单向关闭，忽略失败）
                    self.send_frame(struct.pack("!H", CLOSE_NORMAL), OP_CLOSE)
                except (OSError, WebSocketError):
                    pass
                self.close_socket()
                raise WebSocketClosed(f"对端关闭连接 (code={code} {reason})".strip(), code, reason)
            if frame.opcode == OP_CONTINUATION:
                if self._frag_opcode is None:
                    raise ProtocolError("收到没有起始帧的续帧")
                self._frag_buffer.extend(frame.payload)
                if len(self._frag_buffer) > MAX_MESSAGE_BYTES:
                    raise ProtocolError("分片消息超过上限")
                if frame.fin:
                    opcode = self._frag_opcode
                    payload = bytes(self._frag_buffer)
                    self._frag_opcode = None
                    self._frag_buffer.clear()
                    return opcode, payload
                continue
            # 数据帧：OP_TEXT / OP_BINARY
            if not frame.fin:
                self._frag_opcode = frame.opcode
                self._frag_buffer = bytearray(frame.payload)
                continue
            return frame.opcode, frame.payload

    def recv_text(self, timeout: float | None = None) -> str:
        opcode, payload = self.recv(timeout)
        if opcode != OP_TEXT:
            raise ProtocolError(f"期望 text 帧，收到 opcode=0x{opcode:x}")
        return payload.decode("utf-8", errors="replace")

    def recv_json(self, timeout: float | None = None) -> Any:
        return json.loads(self.recv_text(timeout))

    def messages(self, timeout: float | None = None) -> Iterator[tuple[int, bytes]]:
        """生成器：一直读直到连接关闭（CDP reader 线程用）。"""
        while not self._closed:
            try:
                yield self.recv(timeout)
            except WebSocketClosed:
                return

    # ---- 内部 ----------------------------------------------------------
    def _read_frame(self, deadline: float) -> Frame:
        while True:
            try:
                # 直接传 bytearray（不复制整块缓冲区）：CDP 的大响应可能有几 MB，
                # 每读 64KB 就整块拷贝一次会退化成 O(n²)。
                frame, consumed = parse_frame(self._buf, require_mask=False)
            except IncompleteFrame:
                frame, consumed = None, 0
            if frame is not None:
                del self._buf[:consumed]
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSocketTimeout(f"等待 WebSocket 数据超时（{self._recv_timeout}s）")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise WebSocketTimeout(f"等待 WebSocket 数据超时（{self._recv_timeout}s）") from exc
            except OSError as exc:
                if self._closed:
                    raise WebSocketClosed("连接已关闭") from exc
                raise WebSocketClosed(f"读取失败: {exc}") from exc
            if not chunk:
                self._closed = True
                raise WebSocketClosed("对端关闭了 TCP 连接")
            self._buf.extend(chunk)
            if len(self._buf) > MAX_MESSAGE_BYTES + 1024:
                raise ProtocolError("接收缓冲区超过上限")


def _parse_close_payload(payload: bytes) -> tuple[int, str]:
    if len(payload) >= 2:
        code = struct.unpack_from("!H", payload, 0)[0]
        return code, payload[2:].decode("utf-8", errors="replace")
    return CLOSE_NORMAL, ""


def echo_server_handshake(request: bytes) -> tuple[bytes, str]:
    """从握手请求里取出 ``Sec-WebSocket-Key``，生成合法的 101 响应。

    只有自测的假服务端用得到（``tests/test_wsdump.py`` 用它做端到端握手验证）。
    """
    key = ""
    for line in request.decode("iso-8859-1", errors="replace").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "sec-websocket-key":
            key = value.strip()
            break
    if not key:
        raise ProtocolError("握手请求里没有 Sec-WebSocket-Key")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    ).encode("ascii")
    return response, key
