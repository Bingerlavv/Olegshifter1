"""Общая конфигурация и утилиты для примеров."""

import os
import sys
import logging
from typing import Dict

# Добавляем core в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))


# --- КОНФИГ (редактируй под себя) ---

# Единственный общий секрет. Используется:
#  1) для HMAC-аутентификации WS-handshake (привязка к preshared)
#  2) как соль при выводе сессионных ключей из эфемерных X25519 secret'ов
# В продакшне генерируй через `secrets.token_bytes(32)` и храни в отдельном файле.
PRESHARED = b"CHANGE-ME-32-bytes-shared-secret"

# Сколько параллельных WebSocket каналов поднимать
NUM_CHANNELS = 3
# Сколько gRPC каналов добавить поверх WS (0 = только WS)
# gRPC каналы имеют ID NUM_CHANNELS..NUM_CHANNELS+GRPC_CHANNELS-1
GRPC_CHANNELS = 1

# Адрес сервера (сервер слушает, клиент подключается)
SERVER_HOST = "127.0.0.1"
SERVER_BASE_PORT  = 8443   # WS-каналы: 8443, 8444, 8445...
GRPC_BASE_PORT    = 50051  # gRPC-каналы: 50051, 50052...

# TLS: сертификат и ключ (генерится через gen_cert.py)
CERT_PATH = os.path.join(os.path.dirname(__file__), "server.crt")
KEY_PATH  = os.path.join(os.path.dirname(__file__), "server.key")

# Клиент доверяет нашему самоподписанному CA (для dev).
# В проде — CERT_PATH пустой + используется Let's Encrypt.
TLS_INSECURE = False   # True = не проверять сертификат вообще (совсем dev)

# Локальный SOCKS5 прокси на клиенте — сюда будет ходить браузер
SOCKS5_HOST = "127.0.0.1"
SOCKS5_PORT = 1080


# --- Логирование ---

def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{name}] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


# --- Вывод сессионных ключей из handshake'ов ---

def derive_session_keys(transports: Dict[int, object], preshared: bytes):
    """
    Собираем shared_key каждого канала в порядке ch_id и выводим c2s/s2c
    через HKDF с preshared как солью.

    Форвард-секретность: ephemeral X25519 секреты уникальны на запуск,
    атакующий без знания preshared не выведет сессионные ключи даже
    зная весь DH-handshake.
    """
    from crypto_core import _derive_keys

    keys = []
    for ch_id in sorted(transports.keys()):
        t = transports[ch_id]
        sk = getattr(t, "shared_key", None)
        if sk is None or len(sk) != 32:
            raise RuntimeError(
                f"канал {ch_id}: нет shared_key после handshake"
            )
        keys.append(sk)
    combined = b"".join(keys)
    return _derive_keys(combined, preshared, b"session_c2s", b"session_s2c")


def make_crypto(transports: Dict[int, object], is_initiator: bool):
    """
    Собирает CryptoSession из shared_key'ов всех каналов.

    Клиент и сервер независимо выводят одинаковые c2s/s2c (X25519 симметричен
    по определению), и назначают их своим send/recv ролям противоположно.
    """
    from crypto_core import CryptoSession

    c2s, s2c = derive_session_keys(transports, PRESHARED)
    if is_initiator:
        # client: шифрует на c2s, расшифровывает с s2c
        return CryptoSession(c2s, s2c, is_initiator=True)
    else:
        # server: наоборот
        return CryptoSession(s2c, c2s, is_initiator=False)
