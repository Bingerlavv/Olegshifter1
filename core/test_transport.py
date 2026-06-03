"""Тесты WebSocket транспорта."""

import os
import time
import asyncio
import pytest

from transport import (
    WSConfig, WebSocketClientTransport, WebSocketServerTransport,
    ServerClientSession, make_auth_payload, verify_auth_payload,
    AUTH_CHALLENGE_SIZE, AUTH_TAG_SIZE, AUTH_TIMESTAMP_SIZE,
)


PRESHARED = b"test-preshared-secret-32-bytes!!"


# --- Аутентификация ---

def test_auth_payload_roundtrip():
    payload = make_auth_payload(PRESHARED)
    assert verify_auth_payload(payload, PRESHARED)


def test_auth_wrong_secret():
    payload = make_auth_payload(PRESHARED)
    assert not verify_auth_payload(payload, b"wrong-secret")


def test_auth_tampered_mac():
    payload = bytearray(make_auth_payload(PRESHARED))
    payload[-1] ^= 0xFF
    assert not verify_auth_payload(bytes(payload), PRESHARED)


def test_auth_tampered_challenge():
    payload = bytearray(make_auth_payload(PRESHARED))
    payload[0] ^= 0xFF
    assert not verify_auth_payload(bytes(payload), PRESHARED)


def test_auth_wrong_size():
    assert not verify_auth_payload(b"too-short", PRESHARED)


def test_auth_expired():
    import struct, hmac, hashlib
    # Создаём payload с timestamp из прошлого
    challenge = os.urandom(AUTH_CHALLENGE_SIZE)
    old_ts    = (int(time.time()) - 3600).to_bytes(AUTH_TIMESTAMP_SIZE, "big")
    mac       = hmac.new(PRESHARED, challenge + old_ts, hashlib.sha256).digest()
    payload   = challenge + old_ts + mac
    assert not verify_auth_payload(payload, PRESHARED, max_age_sec=30)


def test_auth_payload_is_unique():
    # Каждый вызов должен давать разный challenge
    p1 = make_auth_payload(PRESHARED)
    p2 = make_auth_payload(PRESHARED)
    assert p1 != p2


# --- Полный round-trip через WebSocket ---

@pytest.mark.asyncio
async def test_websocket_client_server_exchange():
    """
    Поднимаем сервер на случайном порту, клиент подключается,
    обмениваются несколькими сообщениями, закрываются.
    """
    port = _free_port()
    server_received = []
    client_received = []

    async def on_client(session: ServerClientSession):
        # Сервер: читаем 3 сообщения, на каждое отвечаем
        for _ in range(3):
            msg = await session.recv()
            server_received.append(msg)
            await session.send(b"reply-" + msg)

    srv_cfg = WSConfig(
        host="127.0.0.1", port=port, preshared=PRESHARED,
    )
    server = WebSocketServerTransport(srv_cfg, on_client=on_client)
    await server.start()

    try:
        cli_cfg = WSConfig(
            url=f"ws://127.0.0.1:{port}/ws", preshared=PRESHARED,
        )
        client = WebSocketClientTransport(cli_cfg)
        await client.connect()

        try:
            for i in range(3):
                msg = f"hello-{i}".encode()
                await client.send(msg)
                reply = await client.recv()
                client_received.append(reply)
        finally:
            await client.close()

        # Даём серверу время завершить обработку
        await asyncio.sleep(0.1)

    finally:
        await server.stop()

    assert server_received == [b"hello-0", b"hello-1", b"hello-2"]
    assert client_received == [b"reply-hello-0", b"reply-hello-1", b"reply-hello-2"]


@pytest.mark.asyncio
async def test_websocket_rejects_bad_auth():
    """Клиент с неправильным preshared должен быть отклонён."""
    port = _free_port()

    async def on_client(session):
        # Не должен вообще сюда попасть
        pytest.fail("Клиент с неправильным секретом не должен пройти!")

    srv_cfg = WSConfig(
        host="127.0.0.1", port=port, preshared=PRESHARED,
    )
    server = WebSocketServerTransport(srv_cfg, on_client=on_client)
    await server.start()

    try:
        cli_cfg = WSConfig(
            url=f"ws://127.0.0.1:{port}/ws", preshared=b"WRONG-SECRET-32-BYTES-PLEASE!!!!",
        )
        client = WebSocketClientTransport(cli_cfg)

        with pytest.raises((ConnectionError, Exception)):
            await client.connect()

    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_large_payload():
    """Передаём большие пакеты (до 1 MB)."""
    port = _free_port()
    big = os.urandom(1024 * 1024)

    async def on_client(session):
        msg = await session.recv()
        await session.send(msg)  # эхо

    srv_cfg = WSConfig(host="127.0.0.1", port=port, preshared=PRESHARED)
    server  = WebSocketServerTransport(srv_cfg, on_client=on_client)
    await server.start()

    try:
        cli_cfg = WSConfig(url=f"ws://127.0.0.1:{port}/ws", preshared=PRESHARED)
        client  = WebSocketClientTransport(cli_cfg)
        await client.connect()
        try:
            await client.send(big)
            result = await client.recv()
            assert result == big
        finally:
            await client.close()
        await asyncio.sleep(0.05)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_websocket_connection_error_after_close():
    port = _free_port()

    async def on_client(session):
        await asyncio.sleep(0.01)

    srv_cfg = WSConfig(host="127.0.0.1", port=port, preshared=PRESHARED)
    server  = WebSocketServerTransport(srv_cfg, on_client=on_client)
    await server.start()

    try:
        cli_cfg = WSConfig(url=f"ws://127.0.0.1:{port}/ws", preshared=PRESHARED)
        client  = WebSocketClientTransport(cli_cfg)
        await client.connect()
        await client.close()

        with pytest.raises(ConnectionError):
            await client.send(b"after-close")

    finally:
        await server.stop()


# --- Интеграция с crypto_core ---

@pytest.mark.asyncio
async def test_full_stack_ws_crypto():
    """WebSocket + crypto_core: полный зашифрованный канал."""
    from crypto_core import KeyPair, CryptoSession, _derive_keys

    port = _free_port()
    salt = os.urandom(32)
    dh   = os.urandom(32)
    send_k, recv_k = _derive_keys(dh, salt)

    async def on_client(session):
        srv_crypto = CryptoSession(recv_k, send_k, is_initiator=False)
        enc = await session.recv()
        _, _, plaintext = srv_crypto.decrypt(enc)
        reply_enc = srv_crypto.encrypt(1, b"server-says: " + plaintext)
        await session.send(reply_enc)

    srv_cfg = WSConfig(host="127.0.0.1", port=port, preshared=PRESHARED)
    server  = WebSocketServerTransport(srv_cfg, on_client=on_client)
    await server.start()

    try:
        cli_cfg = WSConfig(url=f"ws://127.0.0.1:{port}/ws", preshared=PRESHARED)
        client  = WebSocketClientTransport(cli_cfg)
        await client.connect()

        try:
            cli_crypto = CryptoSession(send_k, recv_k, is_initiator=True)
            enc = cli_crypto.encrypt(1, b"hello server")
            await client.send(enc)

            reply_enc = await client.recv()
            _, _, reply = cli_crypto.decrypt(reply_enc)
            assert reply == b"server-says: hello server"
        finally:
            await client.close()

        await asyncio.sleep(0.05)
    finally:
        await server.stop()


# --- TLS (wss://) ---

@pytest.mark.asyncio
async def test_websocket_over_tls():
    """wss:// с самоподписанным сертификатом, клиент с insecure=True."""
    import tempfile
    from tls_utils import generate_self_signed_cert

    tmp = tempfile.mkdtemp()
    cert = os.path.join(tmp, "cert.pem")
    key  = os.path.join(tmp, "key.pem")
    generate_self_signed_cert(cert, key, common_name="localhost")

    port = _free_port()
    received = []

    async def on_client(session):
        msg = await session.recv()
        received.append(msg)
        await session.send(b"pong-over-tls")

    srv = WebSocketServerTransport(
        WSConfig(host="127.0.0.1", port=port, preshared=PRESHARED,
                 tls_cert=cert, tls_key=key),
        on_client=on_client,
    )
    await srv.start()

    try:
        cli = WebSocketClientTransport(WSConfig(
            url=f"wss://127.0.0.1:{port}/ws",
            preshared=PRESHARED,
            tls_insecure=True,
        ))
        await cli.connect()
        try:
            await cli.send(b"ping-over-tls")
            reply = await cli.recv()
            assert reply == b"pong-over-tls"
            assert received == [b"ping-over-tls"]
        finally:
            await cli.close()
        await asyncio.sleep(0.05)
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_handshake_rejects_mitm_without_preshared():
    """Атакующий без preshared не пройдёт — HMAC привязан к секрету."""
    port = _free_port()

    async def on_client(session):
        pytest.fail("handshake не должен пройти без правильного preshared")

    srv = WebSocketServerTransport(
        WSConfig(host="127.0.0.1", port=port, preshared=PRESHARED),
        on_client=on_client,
    )
    await srv.start()

    try:
        cli = WebSocketClientTransport(WSConfig(
            url=f"ws://127.0.0.1:{port}/ws",
            preshared=b"WRONG-preshared-32-bytes-pls!!!!",
        ))
        with pytest.raises((ConnectionError, Exception)):
            await cli.connect()
    finally:
        await srv.stop()


# --- Утилиты ---

def _free_port() -> int:
    """Находим свободный порт."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
