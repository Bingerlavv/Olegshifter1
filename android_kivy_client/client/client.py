"""
Клиент.

Запуск:
    python client.py

Поднимает NUM_CHANNELS WS + GRPC_CHANNELS gRPC соединений и локальный SOCKS5.

Mixed transports: WS-каналы (ID 0..N-1) + gRPC-каналы (ID N..N+G-1)
идут в один ChannelManager — ADAPTIVE сплиттер сам выбирает куда слать.
Это усложняет DPI-анализ, т.к. трафик разбит по двум разным протоколам.

Авто-переподключение с экспоненциальным backoff (1s → 2s → ... → 60s).
"""

import os
import asyncio
from typing import Dict

import shared
from transport import WebSocketClientTransport, WSConfig, Transport
from grpc_transport import GrpcClientTransport, GrpcConfig
from channel_manager import ChannelManager, ChannelManagerConfig
from multipath import SplitMode
from padding import PaddingConfig, PaddingStrategy
from socks5 import SOCKS5Server, SOCKS5Config

log = shared.setup_logging("client")

RECONNECT_BASE = 1.0
RECONNECT_CAP  = 60.0


def _make_ws_config(ch_id: int) -> WSConfig:
    use_tls = os.path.exists(shared.CERT_PATH)
    scheme  = "wss" if use_tls else "ws"
    port    = shared.SERVER_BASE_PORT + ch_id
    return WSConfig(
        url                 = f"{scheme}://{shared.SERVER_HOST}:{port}/ws",
        preshared           = shared.PRESHARED,
        tls_verify_ca       = shared.CERT_PATH if use_tls and not shared.TLS_INSECURE else None,
        tls_insecure        = shared.TLS_INSECURE,
        tls_server_hostname = "localhost" if use_tls else None,
    )


def _make_grpc_config(grpc_idx: int) -> GrpcConfig:
    port = shared.GRPC_BASE_PORT + grpc_idx
    return GrpcConfig(
        target    = f"{shared.SERVER_HOST}:{port}",
        preshared = shared.PRESHARED,
        tls_insecure = True,   # в dev — без TLS; в проде tls_ca=shared.CERT_PATH
    )


class ClientApp:
    def __init__(self):
        self._manager:  ChannelManager = None
        self._socks5:   SOCKS5Server   = None
        self._transport_cfgs: Dict[int, object] = {}   # ch_id → конфиг для реконнекта
        self._reconnect_tasks: Dict[int, asyncio.Task] = {}

    async def start(self) -> None:
        transports: Dict[int, Transport] = {}

        # --- WS-каналы ---
        for ch_id in range(shared.NUM_CHANNELS):
            cfg = _make_ws_config(ch_id)
            t   = WebSocketClientTransport(cfg)
            try:
                await t.connect()
            except Exception as e:
                log.error("WS канал %d не подключился: %s", ch_id, e)
                for tr in transports.values(): await tr.close()
                raise
            transports[ch_id] = t
            self._transport_cfgs[ch_id] = cfg
            log.info("WS канал %d подключён", ch_id)

        # --- gRPC-каналы ---
        for grpc_idx in range(shared.GRPC_CHANNELS):
            ch_id = shared.NUM_CHANNELS + grpc_idx
            cfg   = _make_grpc_config(grpc_idx)
            t     = GrpcClientTransport(cfg)
            try:
                await t.connect()
            except Exception as e:
                log.warning("gRPC канал %d не подключился (продолжаю без него): %s", ch_id, e)
                continue   # gRPC опционален — работаем и без него
            transports[ch_id] = t
            self._transport_cfgs[ch_id] = cfg
            log.info("gRPC канал %d подключён", ch_id)

        crypto = shared.make_crypto(transports, is_initiator=True)
        mgr_cfg = ChannelManagerConfig(
            split_mode          = SplitMode.ADAPTIVE,
            chunk_size          = 1024,
            padding_cfg         = PaddingConfig(strategy=PaddingStrategy.BIMODAL),
            ping_interval_sec   = 15.0,
            channel_timeout_sec = 60.0,
        )
        self._manager = ChannelManager(crypto, transports, mgr_cfg)
        self._manager.set_on_channel_down(self._on_channel_down)
        await self._manager.start()

        self._socks5 = SOCKS5Server(
            self._manager,
            SOCKS5Config(host=shared.SOCKS5_HOST, port=shared.SOCKS5_PORT),
        )
        await self._socks5.start()

        ws_n   = shared.NUM_CHANNELS
        grpc_n = len(transports) - ws_n
        log.info("")
        log.info("=" * 60)
        log.info("ГОТОВО! Каналов: %d WS + %d gRPC", ws_n, grpc_n)
        log.info("SOCKS5 прокси: %s:%d", shared.SOCKS5_HOST, shared.SOCKS5_PORT)
        log.info("=" * 60)

    def _on_channel_down(self, ch_id: int, reason: str) -> None:
        log.warning("Канал %d упал (%s), переподключаю...", ch_id, reason)
        task = self._reconnect_tasks.get(ch_id)
        if task is None or task.done():
            self._reconnect_tasks[ch_id] = asyncio.create_task(
                self._reconnect_loop(ch_id)
            )

    async def _reconnect_loop(self, ch_id: int) -> None:
        delay = RECONNECT_BASE
        cfg   = self._transport_cfgs.get(ch_id)
        if cfg is None:
            return
        while self._manager is not None:
            await asyncio.sleep(delay)
            try:
                if isinstance(cfg, WSConfig):
                    t = WebSocketClientTransport(cfg)
                else:
                    t = GrpcClientTransport(cfg)
                await t.connect()
                await self._manager.replace_channel(ch_id, t)
                log.info("Канал %d переподключён", ch_id)
                return
            except Exception as e:
                log.warning("Канал %d реконнект неудача (%s), жду %.0fs", ch_id, e, delay)
                delay = min(delay * 2, RECONNECT_CAP)

    async def stop(self) -> None:
        for t in self._reconnect_tasks.values():
            t.cancel()
        if self._socks5:
            await self._socks5.stop()
        if self._manager:
            await self._manager.close()


async def main():
    app = ClientApp()
    await app.start()
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.stop()
        log.info("Клиент остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
