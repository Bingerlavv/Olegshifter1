"""Тесты Traffic Padding + Timing Randomization."""

import os
import math
import asyncio
import statistics
import pytest
from padding import (
    Padder, TimingRandomizer, TrafficShaper, TrafficReceiver,
    PaddingConfig, PaddingStrategy, TimingConfig,
    PktType, PADDING_HEADER_SIZE, _rand_float, _gauss,
)


# --- Padder ---

def test_pad_unpad_roundtrip():
    p = Padder()
    data = b"secret payload 123"
    packet = p.pad(data)
    result = p.unpad(packet)
    assert result == data


def test_pad_preserves_empty():
    p = Padder()
    packet = p.pad(b"")
    assert p.unpad(packet) == b""


def test_pad_size_at_least_min():
    cfg = PaddingConfig(min_size=256, max_size=1400)
    p   = Padder(cfg)
    for _ in range(20):
        pkt = p.pad(b"x")
        assert len(pkt) >= 256


def test_pad_size_not_exceed_max():
    cfg = PaddingConfig(strategy=PaddingStrategy.UNIFORM, min_size=64, max_size=500)
    p   = Padder(cfg)
    for _ in range(50):
        pkt = p.pad(b"hello")
        assert len(pkt) <= 500


def test_pad_fixed_strategy():
    cfg = PaddingConfig(strategy=PaddingStrategy.FIXED, fixed_size=300)
    p   = Padder(cfg)
    for _ in range(10):
        pkt = p.pad(b"data")
        assert len(pkt) == 300


def test_pad_large_data_exceeds_fixed():
    cfg = PaddingConfig(strategy=PaddingStrategy.FIXED, fixed_size=10)
    p   = Padder(cfg)
    big = os.urandom(500)
    pkt = p.pad(big)
    assert p.unpad(pkt) == big
    assert len(pkt) >= len(big) + PADDING_HEADER_SIZE


def test_unpad_chaff_returns_none():
    p     = Padder()
    chaff = p.make_chaff()
    assert p.unpad(chaff) is None


def test_unpad_bad_header_raises():
    with pytest.raises(ValueError):
        Padder().unpad(b"\xFF\x00\x00extra")


def test_unpad_too_short_raises():
    with pytest.raises(ValueError):
        Padder().unpad(b"\x01")


def test_chaff_is_random_size():
    cfg    = PaddingConfig(min_size=64, max_size=400)
    p      = Padder(cfg)
    sizes  = {len(p.make_chaff()) for _ in range(30)}
    assert len(sizes) > 1   # не одинаковые размеры


@pytest.mark.parametrize("strategy", list(PaddingStrategy))
def test_all_strategies_roundtrip(strategy):
    cfg  = PaddingConfig(strategy=strategy)
    p    = Padder(cfg)
    data = os.urandom(100)
    pkt  = p.pad(data)
    assert p.unpad(pkt) == data


def test_bimodal_distribution():
    """Проверяем что bimodal даёт два кластера размеров."""
    cfg   = PaddingConfig(strategy=PaddingStrategy.BIMODAL,
                          peak_small=128, peak_large=1200, peak_ratio=0.4)
    p     = Padder(cfg)
    sizes = [len(p.pad(b"x")) for _ in range(200)]
    small = [s for s in sizes if s < 400]
    large = [s for s in sizes if s >= 400]
    # Оба кластера должны быть представлены
    assert len(small) > 20
    assert len(large) > 20


def test_gaussian_centered():
    cfg   = PaddingConfig(strategy=PaddingStrategy.GAUSSIAN, min_size=64, max_size=1400)
    p     = Padder(cfg)
    sizes = [len(p.pad(b"x")) for _ in range(200)]
    mean  = statistics.mean(sizes)
    # Среднее должно быть примерно в центре диапазона
    assert 200 < mean < 1200


# --- TimingRandomizer ---

def test_delay_non_negative():
    t = TimingRandomizer()
    for _ in range(50):
        assert t.delay_seconds() >= 0


def test_delay_within_bounds():
    cfg = TimingConfig(base_delay_ms=10, jitter_ms=5, burst_prob=0.0)
    t   = TimingRandomizer(cfg)
    for _ in range(50):
        d = t.delay_seconds()
        assert 0 <= d <= (10 + 5) / 1000 + 0.001


def test_burst_prob_one_always_zero():
    cfg = TimingConfig(base_delay_ms=100, jitter_ms=50, burst_prob=1.0)
    t   = TimingRandomizer(cfg)
    for _ in range(20):
        assert t.delay_seconds() == 0.0


def test_chaff_interval_positive():
    t = TimingRandomizer(TimingConfig(chaff_interval_ms=100))
    for _ in range(20):
        assert t.chaff_interval_seconds() > 0


def test_chaff_interval_exponential_mean():
    cfg     = TimingConfig(chaff_interval_ms=200)
    t       = TimingRandomizer(cfg)
    samples = [t.chaff_interval_seconds() for _ in range(500)]
    mean    = statistics.mean(samples)
    # Среднее экспоненциального распределения = lambda = interval/1000
    assert abs(mean - 0.2) < 0.05


# --- TrafficReceiver ---

def test_receiver_passes_data():
    received = []
    r = TrafficReceiver(on_data=received.append)
    p = Padder()
    r.receive(p.pad(b"hello"))
    assert received == [b"hello"]


def test_receiver_ignores_chaff():
    received = []
    r = TrafficReceiver(on_data=received.append)
    p = Padder()
    for _ in range(5):
        r.receive(p.make_chaff())
    assert received == []
    assert r.stats["chaff"] == 5


def test_receiver_counts_errors():
    received = []
    r = TrafficReceiver(on_data=received.append)
    r.receive(b"\xFF\x00\x00bad")
    assert r.stats["errors"] == 1


def test_receiver_stats():
    received = []
    r = TrafficReceiver(on_data=received.append)
    p = Padder()
    r.receive(p.pad(b"a"))
    r.receive(p.pad(b"b"))
    r.receive(p.make_chaff())
    s = r.stats
    assert s["data"]  == 2
    assert s["chaff"] == 1
    assert s["total"] == 3


# --- TrafficShaper async ---

@pytest.mark.asyncio
async def test_shaper_delivers_data():
    sent = []

    async def collect(packet: bytes):
        sent.append(packet)

    cfg    = TimingConfig(base_delay_ms=0, jitter_ms=0, chaff_enabled=False)
    shaper = TrafficShaper(on_packet=collect, timing_cfg=cfg)
    await shaper.start()

    await shaper.send(b"payload_one")
    await shaper.send(b"payload_two")
    await asyncio.sleep(0.05)
    await shaper.stop()

    padder   = Padder()
    receiver = TrafficReceiver(on_data=lambda d: None)
    results  = []
    for pkt in sent:
        data = padder.unpad(pkt)
        if data:
            results.append(data)

    assert b"payload_one" in results
    assert b"payload_two" in results


@pytest.mark.asyncio
async def test_shaper_sends_chaff_when_idle():
    sent = []

    async def collect(packet: bytes):
        sent.append(packet)

    cfg    = TimingConfig(chaff_interval_ms=20, chaff_enabled=True)
    shaper = TrafficShaper(on_packet=collect, timing_cfg=cfg)
    await shaper.start()
    await asyncio.sleep(0.15)   # 150ms → ~7 chaff пакетов
    await shaper.stop()

    padder = Padder()
    chaff_count = sum(1 for pkt in sent if padder.unpad(pkt) is None)
    assert chaff_count >= 3


# --- Утилиты ---

def test_rand_float_in_range():
    for _ in range(100):
        v = _rand_float()
        assert 0.0 <= v < 1.0


def test_gauss_mean_approx():
    samples = [_gauss(100, 10) for _ in range(500)]
    assert abs(statistics.mean(samples) - 100) < 3


def test_gauss_std_approx():
    samples = [_gauss(100, 15) for _ in range(500)]
    assert abs(statistics.stdev(samples) - 15) < 4
