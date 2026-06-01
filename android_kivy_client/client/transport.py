"""
Transport abstraction + WebSocket implementation.

Transport — это "труба" по которой идут зашифрованные пакеты.
Несколько транспортов работают параллельно (multipath).

WebSocket транспорт:
  - Клиент: wss://server/path  (обычный HTTPS handshake)
  - Сервер: принимает WS, и fallback к настоящему сайту при неправильной аутентификации
  - Аутентификация: первый пакет содержит HMAC от общего секрета
"""

import os
import hmac
import ssl
import time
import hashlib
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

from crypto_core import KeyPair, _derive_keys


log = logging.getLogger(__name__)


# --- Абстрактный транспорт ---

class Transport(ABC):
    """Базовый класс — одна "труба" для пакетов."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    async def recv(self) -> bytes:
        """Блокируется пока не придёт пакет. Бросает ConnectionError если закрыто."""
        ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...


# --- Конфигурация ---

@dataclass
class WSConfig:
    url:       str      = "wss://localhost:8443/ws"
    # Для сервера:
    host:      str      = "0.0.0.0"
    port:      int      = 8443
    path:      str      = "/ws"
    # Аутентификация:
    preshared: bytes    = b"change-this-secret-in-production-please"
    # Fallback на реальный сайт (для сервера):
    #   upstream для HTTP reverse proxy, напр. ("example.com", 80) — если None,
    #   на запросы с неправильным путём отдаём статичную nginx-подобную заглушку
    fallback_upstream: Optional[tuple] = None
    # HTML-заглушка для fallback. Если None — встроенная "nginx welcome" страница.
    fallback_html:     Optional[bytes] = None
    # TLS (для сервера):
    tls_cert:  Optional[str] = None
    tls_key:   Optional[str] = None
    # TLS (для клиента):
    tls_verify_ca:       Optional[str] = None   # путь к CA (для self-signed)
    tls_insecure:        bool          = False  # не проверять сертификат (dev!)
    tls_server_hostname: Optional[str] = None   # SNI override


# --- Аутентификация ---

AUTH_CHALLENGE_SIZE = 32
AUTH_TAG_SIZE       = 32
AUTH_TIMESTAMP_SIZE = 8

def make_auth_payload(preshared: bytes) -> bytes:
    """
    Клиент отправляет:
      challenge(32) || timestamp(8) || HMAC-SHA256(preshared, challenge||timestamp)(32)
    """
    challenge = os.urandom(AUTH_CHALLENGE_SIZE)
    ts        = int(time.time()).to_bytes(AUTH_TIMESTAMP_SIZE, "big")
    mac       = hmac.new(preshared, challenge + ts, hashlib.sha256).digest()
    return challenge + ts + mac


def verify_auth_payload(payload: bytes, preshared: bytes, max_age_sec: int = 30) -> bool:
    """Сервер проверяет."""
    expected_size = AUTH_CHALLENGE_SIZE + AUTH_TIMESTAMP_SIZE + AUTH_TAG_SIZE
    if len(payload) != expected_size:
        return False

    challenge = payload[:AUTH_CHALLENGE_SIZE]
    ts_bytes  = payload[AUTH_CHALLENGE_SIZE : AUTH_CHALLENGE_SIZE + AUTH_TIMESTAMP_SIZE]
    mac       = payload[AUTH_CHALLENGE_SIZE + AUTH_TIMESTAMP_SIZE:]

    expected = hmac.new(preshared, challenge + ts_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        return False

    ts = int.from_bytes(ts_bytes, "big")
    if abs(int(time.time()) - ts) > max_age_sec:
        return False

    return True


# --- WebSocket Client Transport ---

class WebSocketClientTransport(Transport):
    """Клиентская сторона — подключается к серверу."""


    def __init__(self, config: WSConfig) -> None:
        self._cfg = config
        self._ws: Optional[object] = None
        self._connected = False
        self._keypair: Optional[KeyPair] = None
        self._shared_key: Optional[bytes] = None

    @property
    def shared_key(self) -> Optional[bytes]:
        """Согласованный DH-секрет этого канала (32 байта)."""
        return self._shared_key

    async def connect(self) -> None:
        if self._connected:
            return

        # TLS context для wss://
        ssl_ctx = None
        if self._cfg.url.startswith("wss://"):
            ssl_ctx = _make_client_ssl_context(
                verify_ca       = self._cfg.tls_verify_ca,
                insecure        = self._cfg.tls_insecure,
            )

        self._ws = await ws_connect(
            self._cfg.url,
            max_size        = 2**22,
            ssl             = ssl_ctx,
            server_hostname = self._cfg.tls_server_hostname if ssl_ctx else None,
        )

        # --- X25519 handshake с привязкой к preshared ---
        self._keypair = KeyPair.generate()
        await self._ws.send(self._keypair.public_bytes)

        server_pub = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        if isinstance(server_pub, str):
            server_pub = server_pub.encode()
        if len(server_pub) != 32:
            await self._ws.close()
            raise ConnectionError(f"Некорректный публичный ключ сервера: {server_pub!r}")

        dh_secret        = self._keypair.exchange(server_pub)
        self._shared_key = dh_secret
        # HKDF с preshared как солью привязывает handshake к секрету.
        # Без знания preshared атакующий не выведет auth_key даже с ephemeral ключами.
        auth_key, _ = _derive_keys(dh_secret, self._cfg.preshared, b"auth_c", b"auth_s")

        hmac_tag = hmac.new(auth_key, b"handshake-ok", hashlib.sha256).digest()
        await self._ws.send(hmac_tag)

        ack = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        if ack != b"OK":
            await self._ws.close()
            raise ConnectionError(f"Сервер отверг handshake: {ack!r}")

        self._connected = True
        log.info("WS клиент подключён к %s (X25519 + TLS=%s)",
                 self._cfg.url, ssl_ctx is not None)

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Не подключено")
        await self._ws.send(data)

    async def recv(self) -> bytes:
        if not self._connected:
            raise ConnectionError("Не подключено")
        try:
            msg = await self._ws.recv()
            return msg if isinstance(msg, bytes) else msg.encode()
        except websockets.exceptions.ConnectionClosed as e:
            self._connected = False
            raise ConnectionError(f"Соединение закрыто: {e}") from e

    async def close(self) -> None:
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    @property
    def is_connected(self) -> bool:
        return self._connected


# --- WebSocket Server Transport ---

class WebSocketServerTransport:
    """
    Серверная сторона — слушает входящие.
    Один экземпляр = один сервер, который может принять много клиентов.
    Для каждого клиента создаётся ClientSession через коллбэк.
    """

    def __init__(
        self,
        config: WSConfig,
        on_client: Callable[["ServerClientSession"], Awaitable[None]],
    ) -> None:
        self._cfg = config
        self._on_client = on_client
        self._server = None

    async def start(self) -> None:
        ssl_ctx = None
        if self._cfg.tls_cert and self._cfg.tls_key:
            import ssl
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(self._cfg.tls_cert, self._cfg.tls_key)

        self._server = await ws_serve(
            self._handle_connection,
            self._cfg.host,
            self._cfg.port,
            ssl=ssl_ctx,
            max_size=2**22,
            process_request=self._process_request,
            server_header="nginx",
        )
        log.info("WS сервер слушает %s:%d (path=%s)",
                 self._cfg.host, self._cfg.port, self._cfg.path)

    async def _process_request(self, connection, request):
        """
        Вызывается до WS-upgrade (websockets поддерживает async здесь).
        Неправильный путь → HTTP-ответ (async TCP-прокси на upstream или заглушка).
        """
        if request.path == self._cfg.path:
            return None   # продолжаем WS-upgrade

        log.info("HTTP fallback: %s", request.path)
        return await self._build_fallback_response(request)

    async def _build_fallback_response(self, request):
        from websockets.http11 import Response
        from websockets.datastructures import Headers

        if self._cfg.fallback_upstream:
            try:
                status, headers, body = await _http_proxy_async(
                    self._cfg.fallback_upstream, request,
                )
                return Response(status, _status_reason(status),
                                Headers(headers), body)
            except Exception as e:
                log.warning("fallback upstream недоступен (%s), отдаём заглушку", e)

        body = self._cfg.fallback_html or _DEFAULT_NGINX_PAGE
        return Response(
            404, "Not Found",
            Headers([
                ("Content-Type",   "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Connection",     "close"),
            ]),
            body,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, ws) -> None:
        """Обработка одного клиента — X25519 handshake с привязкой к preshared."""
        peer = getattr(ws, "remote_address", "unknown")

        try:
            # 1. Первый пакет — публичный ключ клиента
            client_pub = await asyncio.wait_for(ws.recv(), timeout=10.0)
            if isinstance(client_pub, str):
                client_pub = client_pub.encode()
            if len(client_pub) != 32:
                log.warning("Неверный публичный ключ от %s — fallback", peer)
                await self._serve_fallback(ws)
                return

            # 2. Сервер отправляет свой публичный ключ
            server_kp = KeyPair.generate()
            dh_secret = server_kp.exchange(client_pub)
            await ws.send(server_kp.public_bytes)

            # 3. Ждём HMAC подтверждение
            auth_key, _ = _derive_keys(
                dh_secret, self._cfg.preshared, b"auth_c", b"auth_s",
            )
            expected = hmac.new(auth_key, b"handshake-ok", hashlib.sha256).digest()

            received_tag = await asyncio.wait_for(ws.recv(), timeout=10.0)
            if isinstance(received_tag, str):
                received_tag = received_tag.encode()

            if not hmac.compare_digest(expected, received_tag):
                log.warning("Неверный HMAC от %s — fallback", peer)
                await self._serve_fallback(ws)
                return

        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            log.warning("Handshake не завершён от %s", peer)
            return

        # 4. Подтверждаем клиенту
        await ws.send(b"OK")

        session = ServerClientSession(ws, peer, shared_key=dh_secret)
        try:
            await self._on_client(session)
        except Exception:
            log.exception("Ошибка в обработчике клиента")
        finally:
            await session.close()

    async def _serve_fallback(self, ws) -> None:
        """
        Сюда попадаем если клиент знал путь, но не смог пройти X25519/HMAC.
        Реальный fallback на HTTP уже обработан в _process_request (до upgrade).
        Здесь — просто тихо закрываем, имитируя "обычную" смерть WS-сессии.
        """
        await asyncio.sleep(0.5)
        await ws.close(code=1011, reason="internal error")


class ServerClientSession(Transport):
    """Одна клиентская сессия на сервере."""

    def __init__(self, ws, peer, shared_key: Optional[bytes] = None) -> None:
        self._ws = ws
        self._peer = peer
        self._connected = True
        self._shared_key = shared_key

    @property
    def shared_key(self) -> Optional[bytes]:
        """Согласованный DH-секрет этого канала (32 байта)."""
        return self._shared_key

    async def connect(self) -> None:
        pass   # уже подключен

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Клиент отключён")
        await self._ws.send(data)

    async def recv(self) -> bytes:
        if not self._connected:
            raise ConnectionError("Клиент отключён")
        try:
            msg = await self._ws.recv()
            return msg if isinstance(msg, bytes) else msg.encode()
        except websockets.exceptions.ConnectionClosed as e:
            self._connected = False
            raise ConnectionError(f"Клиент отключился: {e}") from e

    async def close(self) -> None:
        self._connected = False
        try:
            await self._ws.close()
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def peer(self):
        return self._peer


# --- TLS helpers ---

def _make_client_ssl_context(
    verify_ca: Optional[str] = None,
    insecure:  bool          = False,
) -> ssl.SSLContext:
    """Локальный хелпер (дублирует tls_utils.make_client_ssl_context)."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif verify_ca:
        ctx.load_verify_locations(verify_ca)
    return ctx


# --- Fallback helpers ---

_DEFAULT_NGINX_PAGE = (
    b"<html>\r\n<head><title>404 Not Found</title></head>\r\n"
    b"<body>\r\n<center><h1>404 Not Found</h1></center>\r\n"
    b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


_HTTP_REASONS = {
    200: "OK",       301: "Moved Permanently", 302: "Found",
    304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable",
}


def _status_reason(code: int) -> str:
    return _HTTP_REASONS.get(code, "OK")


async def _http_proxy_async(
    upstream: tuple,
    request,
    timeout: float = 5.0,
    max_body: int  = 2 * 1024 * 1024,
):
    """
    Асинхронный HTTP/1.0 reverse proxy через asyncio.open_connection.
    Читает ответ upstream целиком и возвращает (status, headers, body).
    Подходит для статичных страниц; для стриминга ставь nginx.
    """
    host, port = upstream
    raw_req = (
        f"GET {request.path} HTTP/1.0\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: Mozilla/5.0\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout,
    )
    try:
        writer.write(raw_req)
        await writer.drain()

        buf = bytearray()
        while len(buf) < max_body:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=timeout)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    head_end = buf.find(b"\r\n\r\n")
    if head_end < 0:
        raise ConnectionError("битый ответ upstream")

    head = bytes(buf[:head_end]).decode("iso-8859-1")
    body = bytes(buf[head_end + 4:])

    lines  = head.split("\r\n")
    parts  = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 else 502

    headers = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k in ("transfer-encoding", "content-length", "connection"):
                continue
            headers.append((k, v.strip()))
    headers.append(("content-length", str(len(body))))
    headers.append(("connection",     "close"))

    return status, headers, body
