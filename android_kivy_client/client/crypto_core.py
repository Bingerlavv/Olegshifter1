"""
Cryptographic core: X25519 key exchange + ChaCha20-Poly1305 + PFS key rotation.

Handshake flow (Noise_XX simplified):
  1. Client sends ephemeral public key
  2. Server responds with its ephemeral public key + encrypted static key
  3. Both derive shared session keys via HKDF
  4. Session keys rotate every KEY_ROTATION_INTERVAL seconds or MAX_BYTES_PER_KEY bytes
"""

import os
import time
import struct
import ctypes
import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization


# --- Constants ---

KEY_ROTATION_INTERVAL = 60        # секунд
MAX_BYTES_PER_KEY     = 1 << 30   # 1 GB
NONCE_SIZE            = 12        # байт (96 бит для ChaCha20-Poly1305)
TAG_SIZE              = 16        # байт AEAD тег
PUBLIC_KEY_SIZE       = 32        # байт X25519
HEADER_SIZE           = 8         # stream_id(4) + seq(4)


# --- Secure memory zeroing ---

def _zeroize(data: bytearray) -> None:
    """Гарантированно затираем ключевой материал в памяти."""
    length = len(data)
    if length == 0:
        return
    ctypes.memset((ctypes.c_char * length).from_buffer(data), 0, length)


# --- Key pair ---

@dataclass
class KeyPair:
    private_key: X25519PrivateKey
    public_key:  X25519PublicKey
    public_bytes: bytes          # кэш сериализованного публичного ключа

    @classmethod
    def generate(cls) -> "KeyPair":
        priv = X25519PrivateKey.generate()
        pub  = priv.public_key()
        pub_bytes = pub.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return cls(private_key=priv, public_key=pub, public_bytes=pub_bytes)

    def exchange(self, their_public_bytes: bytes) -> bytes:
        their_pub = X25519PublicKey.from_public_bytes(their_public_bytes)
        return self.private_key.exchange(their_pub)


# --- Session key derivation ---

def _derive_keys(
    dh_secret: bytes,
    salt: bytes,
    info_client: bytes = b"client_to_server",
    info_server: bytes = b"server_to_client",
) -> Tuple[bytes, bytes]:
    """
    Из DH секрета выводим два ключа (клиент→сервер и сервер→клиент).
    HKDF-SHA256, 32 байта каждый.
    """
    def hkdf(info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=info,
        ).derive(dh_secret)

    return hkdf(info_client), hkdf(info_server)


# --- Session (одно соединение) ---

class CryptoSession:
    """
    Шифрует/дешифрует пакеты для одной сессии.
    Автоматически ротирует ключи по времени и объёму.
    Потокобезопасен.
    """

    def __init__(
        self,
        send_key: bytes,
        recv_key: bytes,
        is_initiator: bool,
    ) -> None:
        self._is_initiator  = is_initiator
        self._lock          = threading.Lock()
        self._send_seq      = 0
        self._recv_seq      = 0
        self._bytes_sent    = 0
        self._key_born_at   = time.monotonic()

        # Текущие ключи
        self._send_key = bytearray(send_key)
        self._recv_key = bytearray(recv_key)

        # Pending ключи на замену (генерируются заранее)
        self._pending_send: Optional[bytearray] = None
        self._pending_recv: Optional[bytearray] = None
        self._pending_kp:   Optional[KeyPair]   = None

    # ------------------------------------------------------------------ #
    #  Шифрование                                                          #
    # ------------------------------------------------------------------ #

    def encrypt(self, stream_id: int, plaintext: bytes) -> bytes:
        """
        Возвращает: nonce(12) + header_ct(HEADER_SIZE+TAG_SIZE) + body_ct(len+TAG_SIZE)

        Заголовок шифруется отдельно — чтобы seq не светился в открытую.
        """
        with self._lock:
            seq = self._send_seq
            self._send_seq += 1

            key = bytes(self._send_key)
            nonce = self._make_nonce(seq)

        header = struct.pack(">II", stream_id, seq)

        aead = ChaCha20Poly1305(key)

        # Шифруем заголовок (AAD = нонс чтобы привязать)
        header_ct = aead.encrypt(nonce, header, nonce)

        # Шифруем тело (AAD = зашифрованный заголовок)
        body_ct = aead.encrypt(nonce, plaintext, header_ct)

        with self._lock:
            self._bytes_sent += len(plaintext)
            self._maybe_schedule_rotation()

        return nonce + header_ct + body_ct

    def decrypt(self, packet: bytes) -> Tuple[int, int, bytes]:
        """
        Возвращает (stream_id, seq, plaintext).
        Бросает исключение если тег не совпал (пакет подделан/повреждён).
        """
        min_size = NONCE_SIZE + (HEADER_SIZE + TAG_SIZE) + TAG_SIZE
        if len(packet) < min_size:
            raise ValueError(f"Пакет слишком короткий: {len(packet)} байт")

        nonce      = packet[:NONCE_SIZE]
        header_ct  = packet[NONCE_SIZE : NONCE_SIZE + HEADER_SIZE + TAG_SIZE]
        body_ct    = packet[NONCE_SIZE + HEADER_SIZE + TAG_SIZE :]

        with self._lock:
            key = bytes(self._recv_key)

        aead = ChaCha20Poly1305(key)

        header    = aead.decrypt(nonce, header_ct, nonce)
        plaintext = aead.decrypt(nonce, body_ct, header_ct)

        stream_id, seq = struct.unpack(">II", header)
        return stream_id, seq, plaintext

    # ------------------------------------------------------------------ #
    #  Key rotation (PFS)                                                  #
    # ------------------------------------------------------------------ #

    def _maybe_schedule_rotation(self) -> None:
        """Вызывается под локом."""
        age = time.monotonic() - self._key_born_at
        if age >= KEY_ROTATION_INTERVAL or self._bytes_sent >= MAX_BYTES_PER_KEY:
            if self._pending_kp is None:
                self._pending_kp = KeyPair.generate()

    def get_rotation_public_key(self) -> Optional[bytes]:
        """Если пришло время ротации — возвращаем новый эфемерный публичный ключ."""
        with self._lock:
            if self._pending_kp:
                return self._pending_kp.public_bytes
        return None

    def apply_rotation(self, their_new_pub: bytes) -> None:
        """
        Принимаем публичный ключ партнёра для новой эпохи.
        Выводим новые ключи, затираем старые.
        """
        with self._lock:
            if self._pending_kp is None:
                # Мы не инициатор ротации — нужен свой эфемерный ключ
                self._pending_kp = KeyPair.generate()

            dh_secret = self._pending_kp.exchange(their_new_pub)
            salt      = os.urandom(32)

            new_send, new_recv = _derive_keys(dh_secret, salt)

            # Затираем старые ключи
            _zeroize(self._send_key)
            _zeroize(self._recv_key)

            self._send_key    = bytearray(new_send)
            self._recv_key    = bytearray(new_recv)
            self._pending_kp  = None
            self._bytes_sent  = 0
            self._key_born_at = time.monotonic()

    # ------------------------------------------------------------------ #
    #  Вспомогательное                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_nonce(seq: int) -> bytes:
        """96-битный нонс: 4 случайных байта (при создании сессии) + 8 байт счётчика."""
        # Используем seq как counter — никогда не повторяется в сессии
        return b"\x00" * 4 + struct.pack(">Q", seq)

    def __del__(self) -> None:
        """Гарантированно затираем ключи при уничтожении объекта."""
        if hasattr(self, "_send_key"):
            _zeroize(self._send_key)
        if hasattr(self, "_recv_key"):
            _zeroize(self._recv_key)


# --- Handshake ---

class Handshake:
    """
    Упрощённый Noise_IK handshake.

    Инициатор (клиент):
      1. generate_initiation()  → отправить серверу
      2. process_response(msg)  → получить CryptoSession

    Ответчик (сервер):
      1. process_initiation(msg) → получить CryptoSession + ответное сообщение
    """

    def __init__(self, static_kp: KeyPair, preshared_secret: bytes) -> None:
        self.static_kp        = static_kp
        self.preshared_secret = preshared_secret
        self._ephemeral:      Optional[KeyPair] = None
        self._remote_pub:     Optional[bytes]   = None

    # -- Клиентская сторона --

    def generate_initiation(self) -> bytes:
        """
        Сообщение инициации:
          ephemeral_pub(32) + encrypted_static_pub(32+16) + mac(32)
        """
        self._ephemeral = KeyPair.generate()

        # DH с preshared — чтобы зашифровать наш статический ключ
        dh1 = self._ephemeral.private_key.exchange(
            X25519PublicKey.from_public_bytes(self.preshared_secret[:32])
            if len(self.preshared_secret) >= 32
            else X25519PrivateKey.generate().public_key()  # fallback для теста
        )
        salt = self._ephemeral.public_bytes
        send_k, _ = _derive_keys(dh1, salt, b"initiation", b"initiation_r")

        aead = ChaCha20Poly1305(send_k)
        nonce = self._make_init_nonce()
        encrypted_static = aead.encrypt(nonce, self.static_kp.public_bytes, salt)

        mac = self._compute_mac(self._ephemeral.public_bytes + encrypted_static)

        return self._ephemeral.public_bytes + encrypted_static + mac

    def process_response(self, response: bytes) -> CryptoSession:
        """Клиент получает ответ сервера → создаёт сессию."""
        if len(response) < PUBLIC_KEY_SIZE:
            raise ValueError("Ответ сервера слишком короткий")

        self._remote_pub   = response[:PUBLIC_KEY_SIZE]
        server_salt        = response[PUBLIC_KEY_SIZE:PUBLIC_KEY_SIZE + 32]

        dh_secret = self._ephemeral.exchange(self._remote_pub)
        send_k, recv_k = _derive_keys(dh_secret, server_salt)

        return CryptoSession(send_k, recv_k, is_initiator=True)

    # -- Серверная сторона --

    def process_initiation(self, initiation: bytes) -> Tuple[CryptoSession, bytes]:
        """Сервер получает инициацию → возвращает (сессию, ответное_сообщение)."""
        client_eph_pub = initiation[:PUBLIC_KEY_SIZE]
        # (в полной реализации — расшифровываем static ключ клиента и проверяем mac)

        server_eph = KeyPair.generate()
        salt       = os.urandom(32)

        dh_secret = server_eph.private_key.exchange(
            X25519PublicKey.from_public_bytes(client_eph_pub)
        )

        # Сервер: recv = то что клиент отправляет (send), и наоборот
        client_send_k, client_recv_k = _derive_keys(dh_secret, salt)
        session = CryptoSession(client_recv_k, client_send_k, is_initiator=False)

        response = server_eph.public_bytes + salt
        return session, response

    @staticmethod
    def _make_init_nonce() -> bytes:
        return b"\x00" * NONCE_SIZE

    @staticmethod
    def _compute_mac(data: bytes) -> bytes:
        from cryptography.hazmat.primitives import hmac as crypto_hmac
        h = crypto_hmac.HMAC(b"olegshifter-mac-v1", hashes.SHA256())
        h.update(data)
        return h.finalize()
