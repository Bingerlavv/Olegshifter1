"""
Сервер (exit node).

Запуск:
    python server.py

Слушает NUM_CHANNELS WS + GRPC_CHANNELS gRPC портов.
Когда клиент подключается по всем каналам — поднимает ExitNode.

Mixed transports: каналы от одного клиента могут прийти в любом порядке
и через разные протоколы. Сервер собирает все NUM_CHANNELS+GRPC_CHANNELS
каналов по IP клиента, затем стартует ChannelManager.
"""

import os
import re
import asyncio
from typing import Dict, Union

import shared
from transport import WebSocketServerTransport, WSConfig, ServerClientSession
from grpc_transport import GrpcServerTransport, GrpcServerClientSession, GrpcConfig
from channel_manager import ChannelManager, ChannelManagerConfig
from multipath import SplitMode
from padding import PaddingConfig, PaddingStrategy
from socks5 import ExitNode

log = shared.setup_logging("server")

_TOTAL_CHANNELS = shared.NUM_CHANNELS + shared.GRPC_CHANNELS

# gRPC peer: "ipv4:1.2.3.4:56789" → "1.2.3.4"
_GRPC_PEER_RE = re.compile(r"(?:ipv[46]:)?(\[?[\w:.]+\]?):\d+$")


def _peer_ip(peer) -> str:
    """Извлекаем IP из WS peer (tuple) или gRPC peer (str)."""
    if isinstance(peer, tuple):
        return peer[0]
    m = _GRPC_PEER_RE.match(str(peer))
    return m.group(1) if m else str(peer)


AnySession = Union[ServerClientSession, GrpcServerClientSession]


class ServerApp:
    def __init__(self):
        self._servers: list = []
        self._pending: Dict[str, Dict[int, AnySession]] = {}
        self._active:  Dict[str, tuple] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        use_tls  = os.path.exists(shared.CERT_PATH)
        cert     = shared.CERT_PATH if use_tls else None
        key      = shared.KEY_PATH  if use_tls else None

        # --- WS серверы ---
        for ch_id in range(shared.NUM_CHANNELS):
            port = shared.SERVER_BASE_PORT + ch_id
            cfg  = WSConfig(
                host      = shared.SERVER_HOST,
                port      = port,
                preshared = shared.PRESHARED,
                tls_cert  = cert,
                tls_key   = key,
            )

            def _ws_handler(channel_id: int):
                async def handler(session: ServerClientSession):
                    await self._on_session(channel_id, session)
                return handler

            srv = WebSocketServerTransport(cfg, on_client=_ws_handler(ch_id))
            await srv.start()
            self._servers.append(srv)
            log.info("WS канал %d слушает порт %d", ch_id, port)

        # --- gRPC серверы ---
        for grpc_idx in range(shared.GRPC_CHANNELS):
            ch_id = shared.NUM_CHANNELS + grpc_idx
            port  = shared.GRPC_BASE_PORT + grpc_idx
            cfg   = GrpcConfig(
                host      = shared.SERVER_HOST,
                port      = port,
                preshared = shared.PRESHARED,
                tls_cert  = cert,
                tls_key   = key,
            )

            def _grpc_handler(channel_id: int):
                async def handler(session: GrpcServerClientSession):
                    await self._on_session(channel_id, session)
                return handler

            srv = GrpcServerTransport(cfg, on_client=_grpc_handler(ch_id))
            await srv.start()
            self._servers.append(srv)
            log.info("gRPC канал %d слушает порт %d", ch_id, port)

    async def _on_session(self, channel_id: int, session: AnySession) -> None:
        """Один канал подключился. Реконнект → replace_channel; иначе — собираем."""
        peer = _peer_ip(session.peer)
        log.info("Клиент %s подключился по каналу %d", peer, channel_id)

        # Если для этого пира уже работает exit node — это реконнект канала.
        # Заменяем транспорт в существующем менеджере, не создавая новую сессию.
        # Новая сессия = новые shared_key → новые session keys, тогда как клиент
        # продолжает шифровать старыми ключами → InvalidTag.
        async with self._lock:
            active_entry = self._active.get(peer)

        if active_entry is not None:
            manager, _ = active_entry
            log.info("Реконнект канала %d от %s → replace_channel", channel_id, peer)
            await manager.replace_channel(channel_id, session)
            return

        ready = None
        async with self._lock:
            self._pending.setdefault(peer, {})[channel_id] = session
            if len(self._pending[peer]) >= _TOTAL_CHANNELS:
                ready = self._pending.pop(peer)

        if ready is None:
            await self._hold_session(session)
            return

        log.info("Все %d каналов от %s готовы — запускаем exit node", _TOTAL_CHANNELS, peer)
        await self._run_exit_node(peer, ready)

    async def _hold_session(self, session: AnySession) -> None:
        while session.is_connected:
            await asyncio.sleep(0.5)

    async def _run_exit_node(self, peer: str, transports: Dict[int, AnySession]) -> None:
        crypto  = shared.make_crypto(transports, is_initiator=False)
        mgr_cfg = ChannelManagerConfig(
            split_mode          = SplitMode.ADAPTIVE,
            chunk_size          = 1024,
            padding_cfg         = PaddingConfig(strategy=PaddingStrategy.BIMODAL),
            ping_interval_sec   = 15.0,
            channel_timeout_sec = 60.0,
        )
        manager   = ChannelManager(crypto, transports, mgr_cfg)
        exit_node = ExitNode(manager)
        await manager.start()

        self._active[peer] = (manager, exit_node)
        try:
            while any(t.is_connected for t in transports.values()):
                await asyncio.sleep(1.0)
        finally:
            await manager.close()
            self._active.pop(peer, None)
            log.info("Клиент %s отключился", peer)

    async def stop(self) -> None:
        for srv in self._servers:
            try:
                await srv.stop()
            except Exception:
                pass
        for manager, _ in self._active.values():
            await manager.close()


async def main():
    app = ServerApp()
    await app.start()
    log.info("Сервер запущен (%d WS + %d gRPC каналов). Ctrl+C для остановки.",
             shared.NUM_CHANNELS, shared.GRPC_CHANNELS)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.stop()
        log.info("Сервер остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
