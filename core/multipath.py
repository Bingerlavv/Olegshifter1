"""
Multipath Splitter / Assembler.

Splitter нарезает поток байт на чанки и распределяет их по каналам.
Assembler принимает чанки с разных каналов, дедуплицирует и собирает в порядке seq.

Режимы:
  STRIPED   — чанки по очереди (максимальная скорость)
  REDUNDANT — каждый чанк по всем каналам (максимальная надёжность)
  WEIGHTED  — пропорционально весу канала
  ADAPTIVE  — автовыбор на основе потерь и задержек
"""

import os
import time
import struct
import asyncio
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Set, Awaitable


# --- Константы ---

CHUNK_MAGIC    = b"\xAB\xCD"   # маркер начала чанка
CHUNK_HEADER   = 18            # magic(2)+stream(4)+seq(8)+flags(1)+length(2)+channel(1)
MAX_CHUNK_SIZE = 1400          # байт payload — чуть меньше MTU
REASSEMBLY_TTL = 30.0          # секунд хранить незавершённые стримы
DEDUP_WINDOW   = 1024          # последних seq помним для дедупликации


class SplitMode(Enum):
    STRIPED   = auto()
    REDUNDANT = auto()
    WEIGHTED  = auto()
    ADAPTIVE  = auto()


class ChunkFlags(Enum):
    DATA  = 0x01
    FIN   = 0x02   # последний чанк стрима
    PING  = 0x04   # служебный (для измерения задержки)


# --- Структура чанка ---

@dataclass
class Chunk:
    stream_id: int    # uint32  — к какому потоку данных относится
    seq:       int    # uint64  — глобальный порядковый номер
    flags:     int    # uint8
    channel:   int    # uint8   — через какой канал пришёл
    payload:   bytes

    # ------------------------------------------------------------------

    def pack(self) -> bytes:
        header = struct.pack(
            ">2sIQBH B",
            CHUNK_MAGIC,
            self.stream_id,
            self.seq,
            self.flags,
            len(self.payload),
            self.channel,
        )
        return header + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> "Chunk":
        if len(data) < CHUNK_HEADER:
            raise ValueError(f"Слишком короткий чанк: {len(data)}")
        magic, stream_id, seq, flags, length, channel = struct.unpack(
            ">2sIQBH B", data[:CHUNK_HEADER]
        )
        if magic != CHUNK_MAGIC:
            raise ValueError(f"Неверный magic: {magic!r}")
        payload = data[CHUNK_HEADER : CHUNK_HEADER + length]
        if len(payload) != length:
            raise ValueError("Усечённый payload")
        return cls(stream_id, seq, flags, channel, payload)

    @property
    def is_fin(self) -> bool:
        return bool(self.flags & ChunkFlags.FIN.value)

    @property
    def is_ping(self) -> bool:
        return bool(self.flags & ChunkFlags.PING.value)


# --- Статистика канала (для ADAPTIVE режима) ---

@dataclass
class ChannelStats:
    channel_id:   int
    weight:       float = 1.0    # базовый вес (для WEIGHTED)
    rtt_ms:       float = 100.0  # скользящее среднее RTT
    loss_rate:    float = 0.0    # доля потерянных чанков [0..1]
    bytes_sent:   int   = 0
    _alpha:       float = 0.2    # EMA коэффициент

    def update_rtt(self, rtt_ms: float) -> None:
        self.rtt_ms = (1 - self._alpha) * self.rtt_ms + self._alpha * rtt_ms

    def update_loss(self, lost: bool) -> None:
        self.loss_rate = (1 - self._alpha) * self.loss_rate + self._alpha * (1.0 if lost else 0.0)

    @property
    def effective_weight(self) -> float:
        """Чем меньше потерь и RTT — тем выше вес."""
        score = self.weight / max(self.rtt_ms, 1.0) / max(self.loss_rate + 0.01, 0.01)
        return max(score, 0.001)


# --- Splitter ---

class Splitter:
    """
    Принимает сырые данные для stream_id и выдаёт список Chunk
    для отправки по каналам.
    """

    def __init__(
        self,
        channels: List[int],
        mode: SplitMode = SplitMode.ADAPTIVE,
        chunk_size: int = MAX_CHUNK_SIZE,
    ) -> None:
        self._channels   = channels
        self._mode       = mode
        self._chunk_size = chunk_size
        self._seq:  Dict[int, int] = {}   # seq per stream_id
        self._rr_idx = 0                  # round-robin индекс для STRIPED
        self._lock       = threading.Lock()
        self._stats: Dict[int, ChannelStats] = {
            ch: ChannelStats(ch) for ch in channels
        }

    def split(self, stream_id: int, data: bytes, fin: bool = False) -> List[tuple]:
        """
        Возвращает список (channel_id, Chunk).
        Последний чанк помечается FIN если fin=True.
        """
        pieces = [data[i:i+self._chunk_size] for i in range(0, max(len(data), 1), self._chunk_size)]
        if not pieces:
            pieces = [b""]

        result = []
        with self._lock:
            for idx, piece in enumerate(pieces):
                is_last = (idx == len(pieces) - 1)
                flags = ChunkFlags.DATA.value
                if is_last and fin:
                    flags |= ChunkFlags.FIN.value

                seq = self._seq.get(stream_id, 0)
                self._seq[stream_id] = seq + 1

                channels_for_chunk = self._pick_channels(seq)
                for ch in channels_for_chunk:
                    chunk = Chunk(stream_id, seq, flags, ch, piece)
                    result.append((ch, chunk))
                    self._stats[ch].bytes_sent += len(piece)

        return result

    def _pick_channels(self, seq: int) -> List[int]:
        """Выбираем каналы в зависимости от режима."""
        if self._mode == SplitMode.REDUNDANT:
            return list(self._channels)

        if self._mode == SplitMode.STRIPED:
            ch = self._channels[self._rr_idx % len(self._channels)]
            self._rr_idx += 1
            return [ch]

        if self._mode == SplitMode.WEIGHTED:
            return [self._weighted_pick()]

        # ADAPTIVE: при высоких потерях дублируем
        picked = self._weighted_pick()
        stats  = self._stats[picked]
        if stats.loss_rate > 0.1:
            # дублируем на лучший запасной канал
            backup = self._weighted_pick(exclude=picked)
            return [picked, backup] if backup is not None else [picked]
        return [picked]

    def _weighted_pick(self, exclude: Optional[int] = None) -> Optional[int]:
        candidates = [ch for ch in self._channels if ch != exclude]
        if not candidates:
            return None
        weights = [self._stats[ch].effective_weight for ch in candidates]
        total   = sum(weights)
        r = (hash(os.urandom(4)) % 10000) / 10000 * total
        cumulative = 0.0
        for ch, w in zip(candidates, weights):
            cumulative += w
            if r <= cumulative:
                return ch
        return candidates[-1]

    def add_channel(self, channel_id: int) -> None:
        with self._lock:
            if channel_id not in self._channels:
                self._channels.append(channel_id)
                self._stats[channel_id] = ChannelStats(channel_id)

    def remove_channel(self, channel_id: int) -> None:
        with self._lock:
            if channel_id in self._channels:
                self._channels.remove(channel_id)
                self._stats.pop(channel_id, None)

    def update_stats(self, channel_id: int, rtt_ms: float, lost: bool) -> None:
        with self._lock:
            if channel_id in self._stats:
                self._stats[channel_id].update_rtt(rtt_ms)
                self._stats[channel_id].update_loss(lost)

    def set_mode(self, mode: SplitMode) -> None:
        with self._lock:
            self._mode = mode

    def get_stats(self) -> Dict[int, ChannelStats]:
        with self._lock:
            return dict(self._stats)


# --- Assembler ---

@dataclass
class _StreamBuffer:
    chunks:     Dict[int, bytes] = field(default_factory=dict)   # seq → payload
    received:   Set[int]         = field(default_factory=set)    # для дедупликации
    fin_seq:    Optional[int]    = None
    created_at: float            = field(default_factory=time.monotonic)
    delivered_through: int       = -1   # последний доставленный seq


class Assembler:
    """
    Принимает Chunk с любых каналов, дедуплицирует, восстанавливает порядок.
    Вызывает on_data(stream_id, data) когда данные готовы к доставке.
    """

    def __init__(
        self,
        on_data: Callable[[int, bytes], None],
        on_stream_end: Optional[Callable[[int], None]] = None,
        reorder_window: int = 64,
    ) -> None:
        self._on_data       = on_data
        self._on_stream_end = on_stream_end
        self._window        = reorder_window
        self._streams:  Dict[int, _StreamBuffer] = {}
        self._dedup:    Dict[int, Set[int]]       = defaultdict(set)  # stream→set(seq)
        self._lock      = threading.Lock()
        self._total_received   = 0
        self._total_duplicates = 0

    def receive(self, chunk: Chunk) -> None:
        """Принять чанк (вызывается при получении данных с любого канала)."""
        if chunk.is_ping:
            return

        with self._lock:
            self._total_received += 1
            sid = chunk.stream_id
            seq = chunk.seq

            # Дедупликация
            if seq in self._dedup[sid]:
                self._total_duplicates += 1
                return
            self._dedup[sid].add(seq)

            # Чистим окно дедупликации
            if len(self._dedup[sid]) > DEDUP_WINDOW:
                min_seq = min(self._dedup[sid])
                self._dedup[sid].discard(min_seq)

            # Буфер стрима
            if sid not in self._streams:
                self._streams[sid] = _StreamBuffer()

            buf = self._streams[sid]
            buf.chunks[seq] = chunk.payload
            buf.received.add(seq)

            if chunk.is_fin:
                buf.fin_seq = seq

            self._flush(sid)

    def _flush(self, sid: int) -> None:
        """Доставляем всё что идёт подряд начиная с delivered_through+1."""
        buf = self._streams[sid]
        while True:
            next_seq = buf.delivered_through + 1
            if next_seq not in buf.chunks:
                break
            data = buf.chunks.pop(next_seq)
            buf.delivered_through = next_seq

            if data:  # не доставляем пустые padding чанки
                self._on_data(sid, data)

            if buf.fin_seq == next_seq:
                if self._on_stream_end:
                    self._on_stream_end(sid)
                del self._streams[sid]
                # Dedup оставляем ещё DEDUP_WINDOW чанков — дубли могут прийти после FIN
                break

    def cleanup_stale(self) -> int:
        """Удаляем зависшие стримы. Возвращает количество удалённых."""
        now = time.monotonic()
        with self._lock:
            stale = [
                sid for sid, buf in self._streams.items()
                if now - buf.created_at > REASSEMBLY_TTL
            ]
            for sid in stale:
                del self._streams[sid]
                self._dedup.pop(sid, None)
        return len(stale)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "total_received":   self._total_received,
                "total_duplicates": self._total_duplicates,
                "active_streams":   len(self._streams),
                "dedup_ratio": (
                    self._total_duplicates / self._total_received
                    if self._total_received else 0.0
                ),
            }


# --- Async обёртка ---

class AsyncAssembler:
    """asyncio-совместимая обёртка над Assembler."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._assembler = Assembler(
            on_data=self._on_data_sync,
            on_stream_end=None,
        )

    def _on_data_sync(self, stream_id: int, data: bytes) -> None:
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._queue.put_nowait, (stream_id, data))
        except RuntimeError:
            pass

    def receive(self, chunk: Chunk) -> None:
        self._assembler.receive(chunk)

    async def read(self) -> tuple:
        """Ждём следующий (stream_id, data)."""
        return await self._queue.get()

    @property
    def stats(self) -> dict:
        return self._assembler.stats
