"""Тесты ChannelManager — оркестратор всего стека."""

import os
import asyncio
import pytest

from crypto_core import CryptoSession, _derive_keys
from multipath   import SplitMode
from padding     import PaddingConfig, PaddingStrategy
from transport   import Transport
from channel_manager import ChannelManager, ChannelManagerConfig


# --- In-memory транспорт для тестов ---

class MemoryTransport(Transport):
    """Пара таких транспортов соединяется напрямую через asyncio.Queue."""

    def __init__(self) -> None:
        self._in:  asyncio.Queue = asyncio.Queue()
        self._peer: "MemoryTransport" = None
        self._connected = True
        self.loss_rate: float = 0.0
        self.delay_sec:  float = 0.0

    @classmethod
    def pair(cls) -> tuple:
        a = cls()
        b = cls()
        a._peer = b
        b._peer = a
        return a, b

    async def connect(self) -> None:
        pass

    async def send(self, data: bytes) -> None:
        if not self._connected or not self._peer._connected:
            raise ConnectionError("закрыто")

        # Имитируем потери
        if self.loss_rate > 0:
            import random
            if random.random() < self.loss_rate:
                return

        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)

        await self._peer._in.put(data)

    async def recv(self) -> bytes:
        if not self._connected:
            raise ConnectionError("закрыто")
        data = await self._in.get()
        if data is None:
            raise ConnectionError("EOF")
        return data

    async def close(self) -> None:
        self._connected = False
        try:
            await self._in.put(None)
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        return self._connected


# --- Хелпер ---

def make_crypto_pair() -> tuple:
    """Две сессии с согласованными ключами (клиент/сервер)."""
    salt = os.urandom(32)
    dh   = os.urandom(32)
    send_k, recv_k = _derive_keys(dh, salt)
    client = CryptoSession(send_k, recv_k, is_initiator=True)
    server = CryptoSession(recv_k, send_k, is_initiator=False)
    return client, server


async def make_manager_pair(
    num_channels: int = 3,
    mode: SplitMode = SplitMode.STRIPED,
) -> tuple:
    """Возвращает (client_mgr, server_mgr, client_received, server_received)."""
    # Создаём N пар соединённых транспортов
    client_ts = {}
    server_ts = {}
    for ch_id in range(num_channels):
        a, b = MemoryTransport.pair()
        client_ts[ch_id] = a
        server_ts[ch_id] = b

    cli_crypto, srv_crypto = make_crypto_pair()

    cfg = ChannelManagerConfig(
        split_mode        = mode,
        chunk_size        = 64,
        ping_interval_sec = 100.0,  # долгий чтобы не мешал тестам
        channel_timeout_sec = 1000.0,
    )

    client = ChannelManager(cli_crypto, client_ts, cfg)
    server = ChannelManager(srv_crypto, server_ts, cfg)

    client_received = []
    server_received = []
    client.set_on_data(lambda sid, data: client_received.append((sid, data)))
    server.set_on_data(lambda sid, data: server_received.append((sid, data)))

    await client.start()
    await server.start()

    return client, server, client_received, server_received


# --- Тесты ---

@pytest.mark.asyncio
async def test_basic_send_receive():
    """Клиент шлёт серверу, сервер шлёт клиенту."""
    client, server, client_received, server_received = await make_manager_pair()

    try:
        await client.send(stream_id=1, data=b"ping from client", fin=True)
        await asyncio.sleep(0.1)

        await server.send(stream_id=2, data=b"pong from server", fin=True)
        await asyncio.sleep(0.1)

        assert (1, b"ping from client") in server_received
        assert (2, b"pong from server") in client_received

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_large_message_split_across_channels():
    """Большое сообщение режется на чанки и идёт по 3 каналам параллельно."""
    client, server, _, server_received = await make_manager_pair(
        num_channels=3,
        mode=SplitMode.STRIPED,
    )

    try:
        original = os.urandom(5000)   # 5KB при chunk_size=64 → ~79 чанков
        await client.send(stream_id=42, data=original, fin=True)
        await asyncio.sleep(0.3)

        collected = b"".join(data for sid, data in server_received if sid == 42)
        assert collected == original

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_redundant_mode_survives_packet_loss():
    """В REDUNDANT режиме даже 50% потерь на одном канале не ломают поток."""
    client, server, _, server_received = await make_manager_pair(
        num_channels=3,
        mode=SplitMode.REDUNDANT,
    )

    # На 2 каналах выставляем потери
    for ch_id in [0, 1]:
        client._transports[ch_id].loss_rate = 0.5

    try:
        data = os.urandom(2000)
        await client.send(stream_id=1, data=data, fin=True)
        await asyncio.sleep(0.3)

        collected = b"".join(d for sid, d in server_received if sid == 1)
        assert collected == data

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_multiple_streams_concurrently():
    """Несколько стримов одновременно — не должны перепутываться."""
    client, server, _, server_received = await make_manager_pair()

    try:
        streams = {sid: os.urandom(500) for sid in range(1, 6)}

        # Отправляем все стримы параллельно
        await asyncio.gather(*(
            client.send(sid, data, fin=True)
            for sid, data in streams.items()
        ))
        await asyncio.sleep(0.3)

        # Собираем по stream_id
        collected: dict = {}
        for sid, data in server_received:
            collected.setdefault(sid, b"")
            collected[sid] += data

        for sid, expected in streams.items():
            assert collected[sid] == expected, f"stream {sid} mismatch"

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_channel_down_callback():
    """Когда канал падает — срабатывает on_channel_down."""
    client, server, _, _ = await make_manager_pair(num_channels=2)

    down_channels = []
    client.set_on_channel_down(lambda ch_id, reason: down_channels.append(ch_id))

    try:
        # Закрываем один канал
        await client._transports[0].close()
        await asyncio.sleep(0.1)

        # Recv loop обнаружит закрытие и вызовет коллбэк
        assert 0 in down_channels

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_survives_channel_failure_mid_stream():
    """Один канал падает — остальные продолжают работать (REDUNDANT)."""
    client, server, _, server_received = await make_manager_pair(
        num_channels=3,
        mode=SplitMode.REDUNDANT,
    )

    try:
        # Начинаем отправку
        await client.send(stream_id=1, data=b"before failure", fin=False)
        await asyncio.sleep(0.05)

        # Ломаем один канал
        await client._transports[0].close()
        await asyncio.sleep(0.05)

        # Отправляем ещё
        await client.send(stream_id=1, data=b"after failure", fin=True)
        await asyncio.sleep(0.2)

        collected = b"".join(d for sid, d in server_received if sid == 1)
        assert b"before failure" in collected
        assert b"after failure" in collected

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_stream_end_callback():
    """FIN флаг вызывает on_stream_end на приёмнике."""
    client, server, _, _ = await make_manager_pair()

    ended = []
    server.set_on_stream_end(lambda sid: ended.append(sid))

    try:
        await client.send(stream_id=99, data=b"bye", fin=True)
        await asyncio.sleep(0.1)
        assert 99 in ended

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_padding_with_bimodal():
    """Стек работает с BIMODAL padding стратегией."""
    client_ts = {}
    server_ts = {}
    for ch_id in range(2):
        a, b = MemoryTransport.pair()
        client_ts[ch_id] = a
        server_ts[ch_id] = b

    cli_crypto, srv_crypto = make_crypto_pair()

    cfg = ChannelManagerConfig(
        split_mode  = SplitMode.STRIPED,
        chunk_size  = 64,
        padding_cfg = PaddingConfig(
            strategy=PaddingStrategy.BIMODAL,
            min_size=128, max_size=1400,
        ),
        ping_interval_sec = 100.0,
    )

    client = ChannelManager(cli_crypto, client_ts, cfg)
    server = ChannelManager(srv_crypto, server_ts, cfg)

    received = []
    server.set_on_data(lambda sid, data: received.append((sid, data)))

    await client.start()
    await server.start()

    try:
        original = os.urandom(1500)
        await client.send(stream_id=1, data=original, fin=True)
        await asyncio.sleep(0.3)

        collected = b"".join(d for sid, d in received if sid == 1)
        assert collected == original

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_empty_payload():
    """Пустой стрим с fin — должен корректно завершиться."""
    client, server, _, server_received = await make_manager_pair()

    ended = []
    server.set_on_stream_end(lambda sid: ended.append(sid))

    try:
        await client.send(stream_id=5, data=b"", fin=True)
        await asyncio.sleep(0.1)
        assert 5 in ended

    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_stats_reporting():
    """Статистика возвращается и обновляется."""
    client, server, _, _ = await make_manager_pair()

    try:
        await client.send(stream_id=1, data=b"hello", fin=True)
        await asyncio.sleep(0.1)

        stats = server.get_stats()
        assert stats["assembler"]["total_received"] >= 1
        assert len(stats["channels"]) == 3

    finally:
        await client.close()
        await server.close()
