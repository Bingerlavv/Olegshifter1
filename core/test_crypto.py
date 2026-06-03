"""Тесты криптографического ядра."""

import os
import time
import pytest
from crypto_core import KeyPair, CryptoSession, Handshake, _derive_keys, _zeroize


def make_session_pair() -> tuple:
    """Создаём пару сессий (клиент, сервер) с общими ключами."""
    salt = os.urandom(32)
    secret = os.urandom(32)
    send_k, recv_k = _derive_keys(secret, salt)
    client = CryptoSession(send_k, recv_k, is_initiator=True)
    server = CryptoSession(recv_k, send_k, is_initiator=False)
    return client, server


# --- Базовое шифрование ---

def test_encrypt_decrypt_roundtrip():
    client, server = make_session_pair()
    plaintext = b"Hello, DPI bypass!"
    packet = client.encrypt(stream_id=1, plaintext=plaintext)
    _, _, result = server.decrypt(packet)
    assert result == plaintext


def test_different_stream_ids():
    client, server = make_session_pair()
    for sid in [0, 1, 42, 0xFFFFFFFF]:
        pkt = client.encrypt(stream_id=sid, plaintext=b"data")
        stream_id, _, data = server.decrypt(pkt)
        assert stream_id == sid
        assert data == b"data"


def test_seq_increments():
    client, server = make_session_pair()
    seqs = []
    for _ in range(5):
        pkt = client.encrypt(1, b"x")
        _, seq, _ = server.decrypt(pkt)
        seqs.append(seq)
    assert seqs == list(range(5))


def test_tampered_packet_rejected():
    client, server = make_session_pair()
    packet = bytearray(client.encrypt(1, b"secret"))
    packet[-1] ^= 0xFF  # портим последний байт
    with pytest.raises(Exception):
        server.decrypt(bytes(packet))


def test_large_payload():
    client, server = make_session_pair()
    data = os.urandom(64 * 1024)  # 64 KB
    pkt = client.encrypt(1, data)
    _, _, result = server.decrypt(pkt)
    assert result == data


def test_empty_payload():
    client, server = make_session_pair()
    pkt = client.encrypt(1, b"")
    _, _, result = server.decrypt(pkt)
    assert result == b""


# --- Key rotation ---

def test_rotation_triggered_by_volume(monkeypatch):
    from crypto_core import MAX_BYTES_PER_KEY
    client, server = make_session_pair()

    monkeypatch.setattr("crypto_core.MAX_BYTES_PER_KEY", 10)

    client.encrypt(1, b"x" * 20)  # превышаем лимит
    assert client.get_rotation_public_key() is not None


def test_rotation_triggered_by_time(monkeypatch):
    client, server = make_session_pair()
    monkeypatch.setattr("crypto_core.KEY_ROTATION_INTERVAL", 0)

    client.encrypt(1, b"trigger")
    assert client.get_rotation_public_key() is not None


def test_key_rotation_still_works():
    client, server = make_session_pair()

    # До ротации
    pkt = client.encrypt(1, b"before rotation")
    _, _, result = server.decrypt(pkt)
    assert result == b"before rotation"

    # Симулируем ротацию вручную
    client_kp = KeyPair.generate()
    server_kp = KeyPair.generate()

    dh = client_kp.private_key.exchange(server_kp.public_key)
    salt = os.urandom(32)
    s, r = _derive_keys(dh, salt)

    client_new = CryptoSession(s, r, is_initiator=True)
    server_new = CryptoSession(r, s, is_initiator=False)

    pkt = client_new.encrypt(1, b"after rotation")
    _, _, result = server_new.decrypt(pkt)
    assert result == b"after rotation"


# --- Handshake ---

def test_handshake_creates_working_session():
    server_static = KeyPair.generate()
    preshared = os.urandom(64)  # в реальности — заранее согласованный секрет

    client_hs = Handshake(KeyPair.generate(), preshared)
    server_hs = Handshake(server_static, preshared)

    initiation = client_hs.generate_initiation()
    server_session, response = server_hs.process_initiation(initiation)
    client_session = client_hs.process_response(response)

    # Сервер шифрует → клиент расшифровывает
    pkt = server_session.encrypt(1, b"from server")
    _, _, result = client_session.decrypt(pkt)
    assert result == b"from server"


# --- Zeroize ---

def test_zeroize_clears_memory():
    buf = bytearray(b"secret key material")
    _zeroize(buf)
    assert all(b == 0 for b in buf)


def test_zeroize_empty():
    _zeroize(bytearray())  # не должно падать


# --- KeyPair ---

def test_keypair_exchange_symmetric():
    kp1 = KeyPair.generate()
    kp2 = KeyPair.generate()
    assert kp1.exchange(kp2.public_bytes) == kp2.exchange(kp1.public_bytes)


def test_keypair_public_bytes_length():
    kp = KeyPair.generate()
    assert len(kp.public_bytes) == 32


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
