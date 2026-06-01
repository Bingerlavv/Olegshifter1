"""
Traffic Padding + Timing Randomization.

Три задачи:
  1. Padding  — размываем длины пакетов (DPI не видит паттерн запрос/ответ)
  2. Chaff    — фейковые пакеты заполняют тишину (нет пауз = нет паттерна)
  3. Timing   — случайные задержки между пакетами (сбиваем timing fingerprint)
"""

import os
import time
import math
import asyncio
import threading
import struct
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Callable, Awaitable


# --- Флаги в заголовке пакета ---

class PktType(Enum):
    DATA  = 0x01   # настоящие данные
    CHAFF = 0x02   # мусор — получатель выбрасывает
    ACK   = 0x03   # служебный


PADDING_HEADER_SIZE = 3   # type(1) + real_len(2)
# real_len — сколько байт из payload настоящие данные (остальное padding)


# --- Распределения для padding размеров ---

class PaddingStrategy(Enum):
    FIXED      = auto()   # всегда до фиксированного размера
    UNIFORM    = auto()   # равномерно в диапазоне
    GAUSSIAN   = auto()   # нормальное распределение (похоже на реальный трафик)
    EXPONENTIAL= auto()   # экспоненциальное (много мелких, редко крупные)
    BIMODAL    = auto()   # два пика: ACK-размер и MTU-размер (имитация HTTP)


@dataclass
class PaddingConfig:
    strategy:    PaddingStrategy = PaddingStrategy.BIMODAL
    min_size:    int  = 64       # минимальный размер пакета после padding
    max_size:    int  = 1400     # максимальный (MTU)
    fixed_size:  int  = 512      # для FIXED стратегии
    # Bimodal: два пика
    peak_small:  int  = 128      # маленький пик (ACK-подобные)
    peak_large:  int  = 1200     # большой пик (данные)
    peak_ratio:  float = 0.3     # вероятность попасть в маленький пик


@dataclass
class TimingConfig:
    base_delay_ms:   float = 0.0    # базовая задержка перед отправкой
    jitter_ms:       float = 5.0    # ±jitter случайный разброс
    burst_prob:      float = 0.1    # вероятность burst (пакет без задержки)
    # Chaff параметры
    chaff_interval_ms:  float = 500.0   # как часто слать мусор в тишину
    chaff_size_min:     int   = 32
    chaff_size_max:     int   = 256
    chaff_enabled:      bool  = True


# --- Padder ---

class Padder:
    """Добавляет padding к payload и снимает его на приёмной стороне."""

    def __init__(self, config: PaddingConfig = PaddingConfig()) -> None:
        self._cfg = config

    def pad(self, data: bytes) -> bytes:
        """
        Возвращает: header(3) + data + random_padding
        Итоговый размер = target_size согласно стратегии.
        """
        target = self._target_size(len(data))
        pad_len = max(0, target - PADDING_HEADER_SIZE - len(data))

        header = struct.pack(">BH", PktType.DATA.value, len(data))
        padding = os.urandom(pad_len)

        return header + data + padding

    def unpad(self, packet: bytes) -> Optional[bytes]:
        """
        Снимает padding. Возвращает None если пакет — chaff.
        Бросает ValueError при плохом заголовке.
        """
        if len(packet) < PADDING_HEADER_SIZE:
            raise ValueError(f"Пакет слишком короткий: {len(packet)}")

        pkt_type, real_len = struct.unpack(">BH", packet[:PADDING_HEADER_SIZE])

        if pkt_type == PktType.CHAFF.value:
            return None   # мусор — выбрасываем

        if pkt_type not in (PktType.DATA.value, PktType.ACK.value):
            raise ValueError(f"Неизвестный тип пакета: {pkt_type}")

        payload_start = PADDING_HEADER_SIZE
        payload_end   = payload_start + real_len

        if payload_end > len(packet):
            raise ValueError("real_len больше размера пакета")

        return packet[payload_start:payload_end]

    def make_chaff(self) -> bytes:
        """Создать фейковый пакет-заглушку."""
        size = int.from_bytes(os.urandom(2), "big") % (
            self._cfg.max_size - self._cfg.min_size
        ) + self._cfg.min_size
        header = struct.pack(">BH", PktType.CHAFF.value, 0)
        return header + os.urandom(max(0, size - PADDING_HEADER_SIZE))

    def _target_size(self, data_len: int) -> int:
        cfg = self._cfg
        minimum = max(cfg.min_size, data_len + PADDING_HEADER_SIZE)

        if cfg.strategy == PaddingStrategy.FIXED:
            return max(minimum, cfg.fixed_size)

        if cfg.strategy == PaddingStrategy.UNIFORM:
            lo = minimum
            hi = max(lo, cfg.max_size)
            span = hi - lo
            if span == 0:
                return lo
            r = int.from_bytes(os.urandom(2), "big") % span
            return lo + r

        if cfg.strategy == PaddingStrategy.GAUSSIAN:
            mean  = (cfg.min_size + cfg.max_size) / 2
            sigma = (cfg.max_size - cfg.min_size) / 6
            val   = int(_gauss(mean, sigma))
            return max(minimum, min(cfg.max_size, val))

        if cfg.strategy == PaddingStrategy.EXPONENTIAL:
            # lambda = 1 / mean; маленькие пакеты вероятнее
            lam = 1.0 / max((cfg.min_size + cfg.max_size) / 4, 1)
            val = int(-math.log(max(_rand_float(), 1e-9)) / lam)
            return max(minimum, min(cfg.max_size, cfg.min_size + val))

        if cfg.strategy == PaddingStrategy.BIMODAL:
            if _rand_float() < cfg.peak_ratio:
                target = _gauss(cfg.peak_small, cfg.peak_small * 0.15)
            else:
                target = _gauss(cfg.peak_large, cfg.peak_large * 0.08)
            return max(minimum, min(cfg.max_size, int(target)))

        return minimum


# --- Timing Randomizer ---

class TimingRandomizer:
    """
    Вычисляет задержки между пакетами.
    Имитирует случайный Poisson-подобный процесс отправки.
    """

    def __init__(self, config: TimingConfig = TimingConfig()) -> None:
        self._cfg = config

    def delay_seconds(self) -> float:
        """Сколько секунд подождать перед отправкой следующего пакета."""
        cfg = self._cfg

        # Burst: иногда шлём мгновенно
        if _rand_float() < cfg.burst_prob:
            return 0.0

        base   = cfg.base_delay_ms
        jitter = (_rand_float() * 2 - 1) * cfg.jitter_ms   # [-jitter, +jitter]
        delay  = max(0.0, base + jitter)

        return delay / 1000.0

    def chaff_interval_seconds(self) -> float:
        """Интервал до следующего chaff пакета (экспоненциальное распределение)."""
        mean = self._cfg.chaff_interval_ms / 1000.0
        # Poisson process → exponential inter-arrival times
        return -math.log(max(_rand_float(), 1e-9)) * mean


# --- Async Pipeline ---

class TrafficShaper:
    """
    asyncio компонент:
      - принимает данные через send(data)
    - добавляет padding + задержку + chaff
      - выдаёт готовые пакеты через on_packet(bytes)
    """

    def __init__(
        self,
        on_packet: Callable[[bytes], Awaitable[None]],
        padding_cfg: PaddingConfig = PaddingConfig(),
        timing_cfg:  TimingConfig  = TimingConfig(),
    ) -> None:
        self._on_packet = on_packet
        self._padder    = Padder(padding_cfg)
        self._timing    = TimingRandomizer(timing_cfg)
        self._timing_cfg = timing_cfg
        self._queue:    asyncio.Queue = asyncio.Queue()
        self._running   = False
        self._tasks:    list = []

    async def start(self) -> None:
        self._running = True
        self._tasks.append(asyncio.create_task(self._send_loop()))
        if self._timing_cfg.chaff_enabled:
            self._tasks.append(asyncio.create_task(self._chaff_loop()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def send(self, data: bytes) -> None:
        """Поставить данные в очередь на отправку."""
        await self._queue.put(data)

    async def _send_loop(self) -> None:
        while self._running:
            data = await self._queue.get()

            delay = self._timing.delay_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            packet = self._padder.pad(data)
            await self._on_packet(packet)

    async def _chaff_loop(self) -> None:
        """Отправляем мусор когда очередь пуста."""
        while self._running:
            interval = self._timing.chaff_interval_seconds()
            await asyncio.sleep(interval)

            if self._queue.empty():
                chaff = self._padder.make_chaff()
                await self._on_packet(chaff)


# --- Приёмная сторона ---

class TrafficReceiver:
    """Снимает padding с входящих пакетов, игнорирует chaff."""

    def __init__(self, on_data: Callable[[bytes], None]) -> None:
        self._padder  = Padder()
        self._on_data = on_data
        self._stats   = {"total": 0, "chaff": 0, "data": 0, "errors": 0}

    def receive(self, packet: bytes) -> None:
        self._stats["total"] += 1
        try:
            data = self._padder.unpad(packet)
            if data is None:
                self._stats["chaff"] += 1
            else:
                self._stats["data"] += 1
                if data:
                    self._on_data(data)
        except ValueError:
            self._stats["errors"] += 1

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# --- Утилиты ---

def _rand_float() -> float:
    """Криптографически случайное float в [0, 1)."""
    return int.from_bytes(os.urandom(4), "big") / 0x1_0000_0000


def _gauss(mean: float, sigma: float) -> float:
    """Box-Muller transform для нормального распределения."""
    u1 = max(_rand_float(), 1e-10)
    u2 = _rand_float()
    z  = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
    return mean + sigma * z
