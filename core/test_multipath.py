"""Тесты Multipath Splitter / Assembler."""

import os
import pytest
from multipath import Splitter, Assembler, AsyncAssembler, Chunk, SplitMode, ChunkFlags


CHANNELS = [0, 1, 2]


def collect_stream(data_chunks: list) -> Callable:
    """Возвращает коллбэк и список накопленных данных."""
    ...


# --- Chunk сериализация ---

def test_chunk_pack_unpack():
    c = Chunk(stream_id=7, seq=42, flags=ChunkFlags.DATA.value, channel=1, payload=b"hello")
    unpacked = Chunk.unpack(c.pack())
    assert unpacked.stream_id == 7
    assert unpacked.seq       == 42
    assert unpacked.channel   == 1
    assert unpacked.payload   == b"hello"


def test_chunk_fin_flag():
    flags = ChunkFlags.DATA.value | ChunkFlags.FIN.value
    c = Chunk(1, 0, flags, 0, b"last")
    assert c.is_fin


def test_chunk_bad_magic():
    data = b"\x00\x00" + b"\x00" * 16
    with pytest.raises(ValueError, match="magic"):
        Chunk.unpack(data)


def test_chunk_truncated():
    c = Chunk(1, 0, ChunkFlags.DATA.value, 0, b"x" * 100)
    truncated = c.pack()[:10]
    with pytest.raises(ValueError):
        Chunk.unpack(truncated)


# --- Splitter ---

def test_striped_round_robin():
    sp = Splitter([0, 1, 2], mode=SplitMode.STRIPED, chunk_size=10)
    results = sp.split(1, b"x" * 30)
    channels = [ch for ch, _ in results]
    assert channels == [0, 1, 2]


def test_redundant_sends_all_channels():
    sp = Splitter([0, 1, 2], mode=SplitMode.REDUNDANT, chunk_size=100)
    results = sp.split(1, b"data")
    channels = [ch for ch, _ in results]
    assert set(channels) == {0, 1, 2}


def test_split_large_data():
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    data = b"A" * 350
    results = sp.split(1, data)
    assert len(results) == 4  # 100+100+100+50


def test_split_fin_on_last():
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    results = sp.split(1, b"x" * 250, fin=True)
    chunks = [c for _, c in results]
    assert not chunks[0].is_fin
    assert not chunks[1].is_fin
    assert chunks[2].is_fin


def test_seq_monotonic():
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=10)
    r1 = sp.split(1, b"a" * 30)
    r2 = sp.split(1, b"b" * 20)
    all_chunks = [c for _, c in r1 + r2]
    seqs = [c.seq for c in all_chunks]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_weighted_picks_one_channel():
    sp = Splitter([0, 1, 2], mode=SplitMode.WEIGHTED, chunk_size=100)
    results = sp.split(1, b"data")
    assert len(results) == 1


def test_stats_update():
    sp = Splitter([0, 1], mode=SplitMode.STRIPED)
    sp.update_stats(0, rtt_ms=50.0, lost=False)
    sp.update_stats(1, rtt_ms=200.0, lost=True)
    stats = sp.get_stats()
    assert stats[1].loss_rate > stats[0].loss_rate


# --- Assembler ---

def _make_assembler() -> tuple:
    received = []
    ended    = []
    asm = Assembler(
        on_data=lambda sid, data: received.append((sid, data)),
        on_stream_end=lambda sid: ended.append(sid),
    )
    return asm, received, ended


def test_assembler_in_order():
    asm, received, _ = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=10)
    chunks = [c for _, c in sp.split(1, b"HelloWorld!", fin=True)]
    for c in chunks:
        asm.receive(c)
    assert b"".join(d for _, d in received) == b"HelloWorld!"


def test_assembler_out_of_order():
    asm, received, _ = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=5)
    chunks = [c for _, c in sp.split(1, b"ABCDEFGHIJ", fin=True)]
    # Доставляем в обратном порядке
    for c in reversed(chunks):
        asm.receive(c)
    result = b"".join(d for _, d in received)
    assert result == b"ABCDEFGHIJ"


def test_assembler_dedup():
    asm, received, _ = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    chunks = [c for _, c in sp.split(1, b"data", fin=True)]
    chunk = chunks[0]
    # Получаем один и тот же чанк 5 раз (REDUNDANT имитация)
    for _ in range(5):
        asm.receive(chunk)
    data = b"".join(d for _, d in received)
    assert data == b"data"
    assert asm.stats["total_duplicates"] == 4


def test_assembler_multiple_streams():
    asm, received, ended = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)

    c1 = [c for _, c in sp.split(1, b"stream_one", fin=True)]
    c2 = [c for _, c in sp.split(2, b"stream_two", fin=True)]

    # Перемешиваем чанки двух стримов
    import random
    mixed = c1 + c2
    random.shuffle(mixed)
    for c in mixed:
        asm.receive(c)

    by_stream = {}
    for sid, data in received:
        by_stream.setdefault(sid, b"")
        by_stream[sid] += data

    assert by_stream[1] == b"stream_one"
    assert by_stream[2] == b"stream_two"
    assert set(ended) == {1, 2}


def test_assembler_fin_triggers_end():
    asm, received, ended = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    chunks = [c for _, c in sp.split(99, b"bye", fin=True)]
    for c in chunks:
        asm.receive(c)
    assert 99 in ended


def test_assembler_stats():
    asm, _, _ = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    chunks = [c for _, c in sp.split(1, b"x" * 50)]
    for c in chunks:
        asm.receive(c)
        asm.receive(c)  # дубль
    s = asm.stats
    assert s["total_received"] == 2
    assert s["total_duplicates"] == 1
    assert s["dedup_ratio"] == pytest.approx(0.5)


def test_cleanup_stale(monkeypatch):
    asm, _, _ = _make_assembler()
    sp = Splitter([0], mode=SplitMode.STRIPED, chunk_size=100)
    # Отправляем без FIN — стрим зависнет
    chunks = [c for _, c in sp.split(1, b"incomplete")]
    for c in chunks:
        asm.receive(c)

    # Симулируем истёкшее время
    for buf in asm._streams.values():
        buf.created_at -= 100.0

    removed = asm.cleanup_stale()
    assert removed == 1
    assert len(asm._streams) == 0


# --- Интеграция Splitter + Assembler ---

def test_full_pipeline_striped():
    asm, received, ended = _make_assembler()
    sp = Splitter(CHANNELS, mode=SplitMode.STRIPED, chunk_size=50)

    original = os.urandom(500)
    chunks = sp.split(1, original, fin=True)
    for _, chunk in chunks:
        asm.receive(chunk)

    result = b"".join(d for _, d in received)
    assert result == original
    assert 1 in ended


def test_full_pipeline_redundant():
    asm, received, ended = _make_assembler()
    sp = Splitter(CHANNELS, mode=SplitMode.REDUNDANT, chunk_size=50)

    original = os.urandom(300)
    chunks = sp.split(1, original, fin=True)
    for _, chunk in chunks:
        asm.receive(chunk)

    result = b"".join(d for _, d in received)
    assert result == original


from typing import Callable  # перемещаем наверх в реальном коде
