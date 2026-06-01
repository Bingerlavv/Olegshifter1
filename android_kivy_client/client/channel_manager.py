"""
ChannelManager — оркестратор всего стека.

Склеивает:
  Splitter ─── разрезает данные на чанки, выбирает каналы
      ↓
  encrypt ─── шифрует каждый чанк индивидуально (CryptoSession)
      ↓
  Padder ──── добавляет padding + chaff
      ↓
  Transport ─ физическая отправка (WebSocket / gRPC / QUIC ...)

Для каждого канала крутится recv-task: принимает пакеты, снимает padding,
расшифровывает, распаковывает Chunk и отдаёт в Assembler.
"""

import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Awaitable

from crypto_core import CryptoSession
from multipath  import Splitter, Assembler, Chunk, SplitMode, ChunkFlags
from padding    import Padder, PaddingConfig
from transport  import Transport


log = logging.getLogger(__name__)


@dataclass
class ChannelManagerConfig:
    split_mode:    SplitMode = SplitMode.ADAPTIVE
    chunk_size:    int       = 1024
    padding_cfg:   PaddingConfig = None
    # Измерение RTT через ping-и:
    ping_interval_sec: float = 10.0
    # Если канал молчит столько секунд — считаем мёртвым:
    channel_timeout_sec: float = 30.0


class ChannelManager:
    """
    Управляет мультипутевой зашифрованной передачей между двумя пирами.

    Использование:
        mgr = ChannelManager(crypto, transports={0: ws1, 1: ws2, 2: grpc})
        mgr.set_on_data(lambda sid, data: ...)
        await mgr.start()
        await mgr.send(stream_id=1, data=b"hello")
        ...
        await mgr.close()
    """

    def __init__(
        self,
        crypto:     CryptoSession,
        transports: Dict[int, Transport],
        config:     Optional[ChannelManagerConfig] = None,
    ) -> None:
        self._crypto     = crypto
        self._transports = transports
        self._cfg        = config or ChannelManagerConfig()

        self._padder = Padder(self._cfg.padding_cfg or PaddingConfig())

        self._splitter = Splitter(
            channels   = list(transports.keys()),
            mode       = self._cfg.split_mode,
            chunk_size = self._cfg.chunk_size,
        )

        self._assembler = Assembler(
            on_data       = self._dispatch_data,
            on_stream_end = self._dispatch_stream_end,
        )

        self._on_data: Optional[Callable[[int, bytes], None]]       = None
        self._on_stream_end: Optional[Callable[[int], None]]        = None
        self._on_channel_down: Optional[Callable[[int, str], None]] = None

        self._running    = False
        self._send_lock  = asyncio.Lock()
        self._recv_tasks: List[asyncio.Task] = []
        self._ping_task: Optional[asyncio.Task] = None
        self._last_recv_at: Dict[int, float]    = {}
        self._pending_pings: Dict[int, float]   = {}   # channel_id → send_time

    # ------------------------------------------------------------------
    #  Публичный API
    # ------------------------------------------------------------------

    def set_on_data(self, cb: Callable[[int, bytes], None]) -> None:
        self._on_data = cb

    def set_on_stream_end(self, cb: Callable[[int], None]) -> None:
        self._on_stream_end = cb

    def set_on_channel_down(self, cb: Callable[[int, str], None]) -> None:
        self._on_channel_down = cb

    async def start(self) -> None:
        """Поднимаем recv-циклы для каждого канала."""
        self._running = True
        now = time.monotonic()
        for ch_id, transport in self._transports.items():
            self._last_recv_at[ch_id] = now
            task = asyncio.create_task(self._recv_loop(ch_id, transport))
            self._recv_tasks.append(task)

        self._ping_task = asyncio.create_task(self._ping_loop())

    async def close(self) -> None:
        self._running = False
        for t in self._recv_tasks:
            t.cancel()
        if self._ping_task:
            self._ping_task.cancel()

        # Ждём завершения всех задач
        await asyncio.gather(
            *self._recv_tasks,
            self._ping_task if self._ping_task else asyncio.sleep(0),
            return_exceptions=True,
        )
        self._recv_tasks.clear()

        for transport in self._transports.values():
            try:
                await transport.close()
            except Exception:
                pass

    async def send(self, stream_id: int, data: bytes, fin: bool = False) -> None:
        """
        Отправить данные в stream_id через мультипуть.
        Splitter сам решает по каким каналам и в каком режиме.
        """
        if not self._running:
            raise ConnectionError("ChannelManager не запущен")

        # Нарезка + выбор каналов
        plans = self._splitter.split(stream_id, data, fin=fin)

        # Группируем по каналам чтобы отправлять параллельно
        by_channel: Dict[int, List[Chunk]] = {}
        for ch_id, chunk in plans:
            by_channel.setdefault(ch_id, []).append(chunk)

        # Отправляем по каналам параллельно
        await asyncio.gather(
            *(self._send_on_channel(ch_id, chunks) for ch_id, chunks in by_channel.items()),
            return_exceptions=False,
        )

    async def send_ping(self, channel_id: int) -> None:
        """Отправляем служебный ping для измерения RTT."""
        if channel_id not in self._transports:
            return
        ping_chunk = Chunk(
            stream_id = 0,
            seq       = 0,
            flags     = ChunkFlags.PING.value,
            channel   = channel_id,
            payload   = b"",
        )
        self._pending_pings[channel_id] = time.monotonic()
        await self._send_on_channel(channel_id, [ping_chunk])

    async def replace_channel(self, ch_id: int, transport: Transport) -> None:
        """
        Горячая замена канала — старый уже убран _mark_channel_down,
        новый подключённый транспорт передаётся сюда.
        Запускает новый recv_loop без перезапуска всего менеджера.
        """
        self._transports[ch_id] = transport
        self._last_recv_at[ch_id] = time.monotonic()
        self._splitter.add_channel(ch_id)
        task = asyncio.create_task(self._recv_loop(ch_id, transport))
        self._recv_tasks.append(task)
        log.info("Канал %d переподключён", ch_id)

    def get_stats(self) -> dict:
        return {
            "splitter":  {ch: vars(s) for ch, s in self._splitter.get_stats().items()},
            "assembler": self._assembler.stats,
            "channels":  list(self._transports.keys()),
        }

    # ------------------------------------------------------------------
    #  Внутренности — отправка
    # ------------------------------------------------------------------

    async def _send_on_channel(self, ch_id: int, chunks: List[Chunk]) -> None:
        transport = self._transports.get(ch_id)
        if transport is None or not transport.is_connected:
            return

        for chunk in chunks:
            try:
                raw       = chunk.pack()
                encrypted = self._crypto.encrypt(stream_id=ch_id, plaintext=raw)
                padded    = self._padder.pad(encrypted)
                await transport.send(padded)
            except ConnectionError as e:
                log.warning("Канал %d упал при отправке: %s", ch_id, e)
                await self._mark_channel_down(ch_id, str(e), transport)
                return

    # ------------------------------------------------------------------
    #  Внутренности — приём
    # ------------------------------------------------------------------

    async def _recv_loop(self, ch_id: int, transport: Transport) -> None:
        """Цикл приёма на одном канале."""
        while self._running:
            try:
                packet = await transport.recv()
            except asyncio.CancelledError:
                return
            except ConnectionError as e:
                log.info("Канал %d закрыт: %s", ch_id, e)
                await self._mark_channel_down(ch_id, str(e), transport)
                return
            except Exception:
                log.exception("Ошибка приёма на канале %d", ch_id)
                await self._mark_channel_down(ch_id, "recv error", transport)
                return

            self._last_recv_at[ch_id] = time.monotonic()

            try:
                self._process_incoming(ch_id, packet)
            except Exception:
                log.exception("Ошибка обработки пакета на канале %d", ch_id)

    def _process_incoming(self, ch_id: int, packet: bytes) -> None:
        # 1. Снимаем padding
        payload = self._padder.unpad(packet)
        if payload is None:
            return   # chaff

        # 2. Расшифровываем
        stream_id, _seq, plaintext = self._crypto.decrypt(payload)

        # 3. Распаковываем Chunk
        chunk = Chunk.unpack(plaintext)

        # 4. Если это pong-ответ на наш ping — обновляем RTT
        if chunk.is_ping:
            self._handle_ping(ch_id)
            return

        # 5. Отдаём в Assembler
        self._assembler.receive(chunk)

    def _handle_ping(self, ch_id: int) -> None:
        """Получили ping — значит канал жив. Если был pending — считаем RTT."""
        sent_at = self._pending_pings.pop(ch_id, None)
        if sent_at is not None:
            rtt_ms = (time.monotonic() - sent_at) * 1000
            self._splitter.update_stats(ch_id, rtt_ms=rtt_ms, lost=False)

    # ------------------------------------------------------------------
    #  Ping loop — измеряем RTT и детектим молчание каналов
    # ------------------------------------------------------------------

    async def _ping_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._cfg.ping_interval_sec)
            except asyncio.CancelledError:
                return

            now = time.monotonic()
            for ch_id in list(self._transports.keys()):
                transport = self._transports[ch_id]
                if not transport.is_connected:
                    continue

                silent_for = now - self._last_recv_at.get(ch_id, now)
                if silent_for > self._cfg.channel_timeout_sec:
                    await self._mark_channel_down(ch_id, f"silent for {silent_for:.0f}s")
                    continue

                try:
                    await self.send_ping(ch_id)
                except Exception:
                    log.warning("Не смогли отправить ping на канал %d", ch_id)

    # ------------------------------------------------------------------
    #  Коллбэки
    # ------------------------------------------------------------------

    def _dispatch_data(self, stream_id: int, data: bytes) -> None:
        if self._on_data:
            try:
                self._on_data(stream_id, data)
            except Exception:
                log.exception("on_data callback упал")

    def _dispatch_stream_end(self, stream_id: int) -> None:
        if self._on_stream_end:
            try:
                self._on_stream_end(stream_id)
            except Exception:
                log.exception("on_stream_end callback упал")

    async def _mark_channel_down(
        self, ch_id: int, reason: str, transport: Transport = None
    ) -> None:
        # Если канал уже был заменён через replace_channel — не трогаем новый транспорт.
        # transport=None (напр. из ping_loop) всегда удаляет текущий.
        if transport is not None and self._transports.get(ch_id) is not transport:
            log.debug("Канал %d уже заменён, mark_down проигнорирован", ch_id)
            return
        old = self._transports.pop(ch_id, None)
        if old:
            try:
                await old.close()
            except Exception:
                pass
        self._splitter.remove_channel(ch_id)

        if self._on_channel_down:
            try:
                self._on_channel_down(ch_id, reason)
            except Exception:
                log.exception("on_channel_down callback упал")

        log.warning("Канал %d помечен упавшим: %s", ch_id, reason)
