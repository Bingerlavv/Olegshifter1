"""
gRPC-транспорт — второй тип "трубы" рядом с WebSocket.

Зачем: многие DPI по-разному относятся к HTTP/2 (gRPC) и HTTP/1.1-upgrade
(WebSocket). Multipath с разнотипными транспортами труднее профилировать.

Протокол:
  service Tunnel { rpc Stream(stream Packet) returns (stream Packet); }

Шифрование идёт слоем выше (ChannelManager → CryptoSession), так что в
Packet.data лежит уже шифртекст. В handshake — тот же X25519 + preshared-HMAC,
но обёрнут в пару "служебных" Packet'ов в начале потока.

Зависимости: grpcio, grpcio-tools (для сборки stub'ов из .proto).
Регенерация stub'ов:
    python -m grpc_tools.protoc -Icore/proto --python_out=core/proto \\
        --grpc_python_out=core/proto core/proto/tunnel.proto
"""

from __future__ import annotations

import os
import ssl
import asyncio
import hmac as _hmac
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional, AsyncIterator, Callable, Awaitable

try:
    import grpc
    from grpc import aio as grpc_aio
except ImportError as e:
    raise ImportError(
        "для gRPC-транспорта нужен grpcio: pip install grpcio grpcio-tools"
    ) from e

from proto import tunnel_pb2, tunnel_pb2_grpc

from transport import Transport
from crypto_core import KeyPair, _derive_keys


log = logging.getLogger(__name__)


# --- Конфиг ---

@dataclass
class GrpcConfig:
    # Клиент: target сервера, напр. "server.example.com:50051"
    target:          str = "127.0.0.1:50051"
    # Сервер:
    host:            str = "0.0.0.0"
    port:            int = 50051
    # Общий:
    preshared:       bytes = b"change-this-secret-in-production-please"
    # TLS:
    tls_cert:        Optional[str] = None    # для сервера
    tls_key:         Optional[str] = None
    tls_ca:          Optional[str] = None    # для клиента (self-signed CA)
    tls_insecure:    bool          = False   # клиент: не проверять
    tls_server_name: Optional[str] = None    # SNI override


# --- Общие служебные сообщения хендшейка ---

def _pkt(data: bytes) -> tunnel_pb2.Packet:
    return tunnel_pb2.Packet(data=data)


# --- Клиент ---

class GrpcClientTransport(Transport):
    """Клиент — открывает bidi-stream к серверу."""

    def __init__(self, cfg: GrpcConfig) -> None:
        self._cfg = cfg
        self._channel: Optional[grpc_aio.Channel] = None
        self._call: Optional[grpc_aio.StreamStreamCall] = None
        self._connected = False
        self._shared_key: Optional[bytes] = None
        self._send_q: asyncio.Queue[bytes] = asyncio.Queue()

    @property
    def shared_key(self) -> Optional[bytes]:
        return self._shared_key

    async def connect(self) -> None:
        if self._connected:
            return

        credentials = self._make_client_credentials()
        options = [
            ("grpc.keepalive_time_ms",        20_000),
            ("grpc.keepalive_timeout_ms",     10_000),
            ("grpc.http2.max_pings_without_data", 0),
        ]
        # SNI: grpc принимает override через "grpc.ssl_target_name_override"
        if self._cfg.tls_server_name:
            options.append(
                ("grpc.ssl_target_name_override", self._cfg.tls_server_name),
            )

        if credentials is None:
            self._channel = grpc_aio.insecure_channel(
                self._cfg.target, options=options,
            )
        else:
            self._channel = grpc_aio.secure_channel(
                self._cfg.target, credentials, options=options,
            )

        stub = tunnel_pb2_grpc.TunnelStub(self._channel)
        self._call = stub.Stream(self._outbound_iter())

        # Handshake поверх потока:
        kp = KeyPair.generate()
        await self._call.write(_pkt(kp.public_bytes))

        srv_pub_msg = await self._call.read()
        if srv_pub_msg is None or len(srv_pub_msg.data) != 32:
            await self.close()
            raise ConnectionError(
                f"некорректный серверный pubkey: {srv_pub_msg!r}",
            )

        dh = kp.exchange(srv_pub_msg.data)
        self._shared_key = dh
        auth_key, _ = _derive_keys(dh, self._cfg.preshared, b"auth_c", b"auth_s")
        tag = _hmac.new(auth_key, b"handshake-ok", hashlib.sha256).digest()
        await self._call.write(_pkt(tag))

        ack = await self._call.read()
        if ack is None or ack.data != b"OK":
            await self.close()
            raise ConnectionError(f"сервер отверг handshake: {ack!r}")

        self._connected = True
        log.info(
            "gRPC клиент подключён к %s (TLS=%s)",
            self._cfg.target, credentials is not None,
        )

    def _make_client_credentials(self):
        if self._cfg.tls_insecure:
            return None
        if self._cfg.tls_ca:
            with open(self._cfg.tls_ca, "rb") as f:
                root = f.read()
            return grpc.ssl_channel_credentials(root_certificates=root)
        # TLS с системным CA
        return grpc.ssl_channel_credentials()

    async def _outbound_iter(self) -> AsyncIterator[tunnel_pb2.Packet]:
        while True:
            item = await self._send_q.get()
            if item is None:
                return
            yield _pkt(item)

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Не подключено")
        await self._send_q.put(data)

    async def recv(self) -> bytes:
        if not self._connected or self._call is None:
            raise ConnectionError("Не подключено")
        try:
            msg = await self._call.read()
        except grpc.aio.AioRpcError as e:
            self._connected = False
            raise ConnectionError(f"gRPC закрыт: {e}") from e
        if msg is None:
            self._connected = False
            raise ConnectionError("gRPC поток закрыт сервером")
        return msg.data

    async def close(self) -> None:
        self._connected = False
        if self._call is not None:
            try:
                await self._send_q.put(None)
                self._call.cancel()
            except Exception:
                pass
            self._call = None
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception:
                pass
            self._channel = None

    @property
    def is_connected(self) -> bool:
        return self._connected


# --- Сервер ---

class GrpcServerClientSession(Transport):
    """Одна клиентская сессия на gRPC-сервере."""

    def __init__(self, peer: str, shared_key: bytes) -> None:
        self._peer = peer
        self._shared_key = shared_key
        self._connected = True
        self._recv_q:  asyncio.Queue = asyncio.Queue()
        self._send_q:  asyncio.Queue = asyncio.Queue()
        self._done = asyncio.Event()

    @property
    def peer(self) -> str:
        return self._peer

    @property
    def shared_key(self) -> Optional[bytes]:
        return self._shared_key

    @property
    def is_connected(self) -> bool:
        return self._connected

    # Transport API:

    async def connect(self) -> None:
        pass

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Клиент отключён")
        await self._send_q.put(data)

    async def recv(self) -> bytes:
        if not self._connected:
            raise ConnectionError("Клиент отключён")
        item = await self._recv_q.get()
        if item is None:
            self._connected = False
            raise ConnectionError("поток закрыт")
        return item

    async def close(self) -> None:
        if self._connected:
            self._connected = False
            await self._send_q.put(None)
            self._done.set()

    # --- Внутренние: вызываются из gRPC-servicer ---

    async def _feed(self, data: bytes) -> None:
        await self._recv_q.put(data)

    async def _signal_closed(self) -> None:
        self._connected = False
        await self._recv_q.put(None)
        self._done.set()

    async def _outbound(self) -> AsyncIterator[tunnel_pb2.Packet]:
        while True:
            item = await self._send_q.get()
            if item is None:
                return
            yield _pkt(item)


class _TunnelServicer(tunnel_pb2_grpc.TunnelServicer):
    def __init__(
        self,
        preshared: bytes,
        on_client: Callable[[GrpcServerClientSession], Awaitable[None]],
    ) -> None:
        self._preshared = preshared
        self._on_client = on_client

    async def Stream(self, request_iterator, context) -> AsyncIterator[tunnel_pb2.Packet]:
        peer = context.peer()
        try:
            client_pub_msg = await _anext(request_iterator)
        except StopAsyncIteration:
            return

        if not client_pub_msg or len(client_pub_msg.data) != 32:
            log.warning("gRPC %s: неверный pubkey", peer)
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad pubkey")
            return

        server_kp = KeyPair.generate()
        dh = server_kp.exchange(client_pub_msg.data)
        yield _pkt(server_kp.public_bytes)

        try:
            tag_msg = await _anext(request_iterator)
        except StopAsyncIteration:
            return

        auth_key, _ = _derive_keys(dh, self._preshared, b"auth_c", b"auth_s")
        expected = _hmac.new(auth_key, b"handshake-ok", hashlib.sha256).digest()
        if not tag_msg or not _hmac.compare_digest(expected, tag_msg.data):
            log.warning("gRPC %s: плохой HMAC", peer)
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad auth")
            return

        yield _pkt(b"OK")

        session = GrpcServerClientSession(peer, shared_key=dh)

        # Параллельно: тянем входящие из request_iterator в session._recv_q,
        # и отдаём исходящие из session._send_q в yield.
        async def pump_inbound():
            try:
                async for msg in request_iterator:
                    await session._feed(msg.data)
            finally:
                await session._signal_closed()

        async def pump_outbound(out_queue: asyncio.Queue):
            while True:
                item = await session._send_q.get()
                await out_queue.put(item)
                if item is None:
                    return

        inbound_task = asyncio.create_task(pump_inbound())
        handler_task = asyncio.create_task(self._run_handler(session))

        out_q: asyncio.Queue = asyncio.Queue()
        outbound_task = asyncio.create_task(pump_outbound(out_q))

        try:
            while True:
                item = await out_q.get()
                if item is None:
                    return
                yield _pkt(item)
        finally:
            await session.close()
            for t in (inbound_task, handler_task, outbound_task):
                t.cancel()

    async def _run_handler(self, session: GrpcServerClientSession) -> None:
        try:
            await self._on_client(session)
        except Exception:
            log.exception("Ошибка в gRPC-обработчике клиента")
        finally:
            await session.close()


async def _anext(it):
    """Полифилл — в 3.10+ есть встроенный anext()."""
    try:
        return await it.__anext__()
    except StopAsyncIteration:
        return None


class GrpcServerTransport:
    """
    gRPC-сервер, параллельный WebSocket-серверу по смыслу.
    Принимает bidi-stream от клиента, после handshake создаёт
    GrpcServerClientSession и передаёт её в on_client.
    """

    def __init__(
        self,
        cfg: GrpcConfig,
        on_client: Callable[[GrpcServerClientSession], Awaitable[None]],
    ) -> None:
        self._cfg = cfg
        self._on_client = on_client
        self._server: Optional[grpc_aio.Server] = None

    async def start(self) -> None:
        self._server = grpc_aio.server(
            options=[
                ("grpc.keepalive_time_ms",          20_000),
                ("grpc.keepalive_timeout_ms",       10_000),
                ("grpc.http2.max_pings_without_data", 0),
            ]
        )
        tunnel_pb2_grpc.add_TunnelServicer_to_server(
            _TunnelServicer(self._cfg.preshared, self._on_client),
            self._server,
        )

        addr = f"{self._cfg.host}:{self._cfg.port}"
        if self._cfg.tls_cert and self._cfg.tls_key:
            with open(self._cfg.tls_cert, "rb") as f:
                cert_chain = f.read()
            with open(self._cfg.tls_key, "rb") as f:
                priv_key = f.read()
            creds = grpc.ssl_server_credentials([(priv_key, cert_chain)])
            self._server.add_secure_port(addr, creds)
        else:
            self._server.add_insecure_port(addr)

        await self._server.start()
        log.info("gRPC сервер слушает %s (TLS=%s)",
                 addr, bool(self._cfg.tls_cert))

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=2.0)
            self._server = None
