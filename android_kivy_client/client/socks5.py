"""
SOCKS5 сервер (клиентская сторона) + exit node (серверная сторона).

Клиентская сторона (SOCKS5Server):
  - Слушает localhost:1080
  - Браузер подключается, шлёт CONNECT host:port
  - Для каждого соединения — новый stream_id в ChannelManager
  - Первый пакет в стриме = control header с target addr
  - Остальные пакеты = сырые байты TCP

Серверная сторона (ExitNode):
  - Получает новый стрим
  - Читает control header → открывает TCP к target
  - Пайпит данные в обе стороны
  - FIN в любом направлении → закрытие

Стековая интеграция:
  Browser ─SOCKS5─► SOCKS5Server ──► ChannelManager ══► ChannelManager ──► ExitNode ──► Internet
                                     (зашифрованный multipath)
"""

import struct
import socket
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional

from channel_manager import ChannelManager


log = logging.getLogger(__name__)


# --- SOCKS5 константы ---

SOCKS_VERSION = 0x05

AUTH_NONE     = 0x00
AUTH_FAIL     = 0xFF

CMD_CONNECT   = 0x01
CMD_BIND      = 0x02
CMD_UDP       = 0x03

ATYP_IPV4     = 0x01
ATYP_DOMAIN   = 0x03
ATYP_IPV6     = 0x04

REP_SUCCESS                = 0x00
REP_SERVER_FAILURE         = 0x01
REP_CONN_NOT_ALLOWED       = 0x02
REP_NETWORK_UNREACHABLE    = 0x03
REP_HOST_UNREACHABLE       = 0x04
REP_CONN_REFUSED           = 0x05
REP_TTL_EXPIRED            = 0x06
REP_CMD_NOT_SUPPORTED      = 0x07
REP_ADDR_TYPE_NOT_SUPPORTED= 0x08


# --- Control header (наш протокол между SOCKS5Server и ExitNode) ---
#
# [1 byte version=1] [1 byte atyp] [addr] [2 bytes port]
# addr формат:
#   ATYP_IPV4   — 4 байта
#   ATYP_IPV6   — 16 байт
#   ATYP_DOMAIN — 1 байт длины + домен

CONTROL_VERSION = 0x01


def encode_control(atyp: int, addr: bytes, port: int) -> bytes:
    if atyp == ATYP_DOMAIN:
        addr_bytes = bytes([len(addr)]) + addr
    else:
        addr_bytes = addr
    return struct.pack(">BB", CONTROL_VERSION, atyp) + addr_bytes + struct.pack(">H", port)


def decode_control(data: bytes) -> tuple:
    """Возвращает (atyp, addr_str, port, consumed_bytes)."""
    if len(data) < 4:
        raise ValueError("control header слишком короткий")

    version, atyp = data[0], data[1]
    if version != CONTROL_VERSION:
        raise ValueError(f"неверная версия control: {version}")

    offset = 2
    if atyp == ATYP_IPV4:
        if len(data) < offset + 4 + 2:
            raise ValueError("IPv4 адрес обрезан")
        addr = socket.inet_ntop(socket.AF_INET, data[offset:offset+4])
        offset += 4
    elif atyp == ATYP_IPV6:
        if len(data) < offset + 16 + 2:
            raise ValueError("IPv6 адрес обрезан")
        addr = socket.inet_ntop(socket.AF_INET6, data[offset:offset+16])
        offset += 16
    elif atyp == ATYP_DOMAIN:
        dlen = data[offset]
        offset += 1
        if len(data) < offset + dlen + 2:
            raise ValueError("домен обрезан")
        addr = data[offset:offset+dlen].decode("ascii", errors="replace")
        offset += dlen
    else:
        raise ValueError(f"неизвестный atyp: {atyp}")

    port = struct.unpack(">H", data[offset:offset+2])[0]
    offset += 2
    return atyp, addr, port, offset


# --- SOCKS5 Server (клиентская сторона) ---

@dataclass
class SOCKS5Config:
    host: str = "127.0.0.1"
    port: int = 1080


class SOCKS5Server:
    """
    Принимает браузерные SOCKS5 соединения и туннелит их через ChannelManager.
    """

    def __init__(self, manager: ChannelManager, config: SOCKS5Config = SOCKS5Config()) -> None:
        self._mgr  = manager
        self._cfg  = config
        self._server: Optional[asyncio.Server] = None
        self._next_stream_id = 100   # стримы 0-99 зарезервированы под служебку
        self._stream_lock    = asyncio.Lock()
        # stream_id → (writer, event) — куда класть входящие данные
        self._streams: Dict[int, tuple] = {}

        self._mgr.set_on_data(self._on_tunnel_data)
        self._mgr.set_on_stream_end(self._on_tunnel_end)

    # --- жизненный цикл ---

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._cfg.host, self._cfg.port,
        )
        log.info("SOCKS5 слушает %s:%d", self._cfg.host, self._cfg.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # --- обработка одного браузера ---

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            # 1. Handshake: [ver=5][nmethods][methods...]
            data = await reader.readexactly(2)
            ver, nmethods = data[0], data[1]
            if ver != SOCKS_VERSION:
                await self._fail(writer, f"неверная версия SOCKS: {ver}")
                return
            await reader.readexactly(nmethods)   # читаем список методов — игнорим

            # Отвечаем: NO AUTH
            writer.write(bytes([SOCKS_VERSION, AUTH_NONE]))
            await writer.drain()

            # 2. Request: [ver=5][cmd][rsv=0][atyp][addr][port]
            header = await reader.readexactly(4)
            ver, cmd, _rsv, atyp = header

            if cmd != CMD_CONNECT:
                await self._send_socks_reply(writer, REP_CMD_NOT_SUPPORTED)
                return

            if atyp == ATYP_IPV4:
                addr_raw = await reader.readexactly(4)
                addr_str = socket.inet_ntop(socket.AF_INET, addr_raw)
            elif atyp == ATYP_IPV6:
                addr_raw = await reader.readexactly(16)
                addr_str = socket.inet_ntop(socket.AF_INET6, addr_raw)
            elif atyp == ATYP_DOMAIN:
                dlen     = (await reader.readexactly(1))[0]
                addr_raw = await reader.readexactly(dlen)
                addr_str = addr_raw.decode("ascii", errors="replace")
            else:
                await self._send_socks_reply(writer, REP_ADDR_TYPE_NOT_SUPPORTED)
                return

            port = struct.unpack(">H", await reader.readexactly(2))[0]

            log.info("SOCKS5 CONNECT %s %s:%d", peer, addr_str, port)

            # 3. Выделяем stream_id и отправляем control header на exit node
            stream_id = await self._alloc_stream(writer)

            control = encode_control(atyp, addr_raw, port)
            await self._mgr.send(stream_id=stream_id, data=control, fin=False)

            # 4. Отвечаем браузеру что соединение готово
            #    (в реальности стоит ждать ACK от exit node, но для простоты сразу)
            await self._send_socks_reply(writer, REP_SUCCESS)

            # 5. Пайпим байты browser → tunnel
            await self._pipe_to_tunnel(reader, stream_id)

        except asyncio.IncompleteReadError:
            pass
        except Exception:
            log.exception("ошибка в SOCKS5 handler")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _pipe_to_tunnel(self, reader: asyncio.StreamReader, stream_id: int) -> None:
        """Читаем из браузера и шлём в туннель."""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    await self._mgr.send(stream_id=stream_id, data=b"", fin=True)
                    break
                await self._mgr.send(stream_id=stream_id, data=data, fin=False)
        except Exception:
            pass
        finally:
            async with self._stream_lock:
                self._streams.pop(stream_id, None)

    # --- приём данных из туннеля → в браузер ---

    def _on_tunnel_data(self, stream_id: int, data: bytes) -> None:
        entry = self._streams.get(stream_id)
        if entry is None:
            return
        writer, _ = entry
        try:
            writer.write(data)
        except Exception:
            pass

    def _on_tunnel_end(self, stream_id: int) -> None:
        entry = self._streams.pop(stream_id, None)
        if entry is None:
            return
        writer, _ = entry
        try:
            writer.close()
        except Exception:
            pass

    # --- Утилиты ---

    async def _alloc_stream(self, writer: asyncio.StreamWriter) -> int:
        async with self._stream_lock:
            sid = self._next_stream_id
            self._next_stream_id += 1
            self._streams[sid] = (writer, None)
            return sid

    async def _send_socks_reply(self, writer: asyncio.StreamWriter, rep: int) -> None:
        # Отвечаем нулевым bind addr — большинство клиентов не обращают внимание
        reply = struct.pack(">BBBBIH", SOCKS_VERSION, rep, 0, ATYP_IPV4, 0, 0)
        writer.write(reply)
        await writer.drain()

    async def _fail(self, writer: asyncio.StreamWriter, reason: str) -> None:
        log.warning("SOCKS5 fail: %s", reason)
        try:
            writer.close()
        except Exception:
            pass


# --- Exit Node (серверная сторона) ---

class ExitNode:
    """
    Серверная сторона: принимает стримы от ChannelManager,
    первый пакет — control header с target addr, дальше — сырые данные.
    Открывает TCP к target и пайпит данные в обе стороны.
    """

    def __init__(self, manager: ChannelManager) -> None:
        self._mgr = manager
        # stream_id → TargetConnection (установленные)
        self._connections: Dict[int, "TargetConnection"] = {}
        # stream_id → asyncio.Queue (буфер данных, пока открывается соединение)
        self._pending: Dict[int, asyncio.Queue] = {}
        self._pending_fin: set = set()

        self._mgr.set_on_data(self._on_tunnel_data)
        self._mgr.set_on_stream_end(self._on_tunnel_end)

    def _on_tunnel_data(self, stream_id: int, data: bytes) -> None:
        conn = self._connections.get(stream_id)
        if conn is not None:
            conn.write(data)
            return

        # Соединение ещё не готово
        if stream_id in self._pending:
            # Уже открываем — кладём в очередь
            self._pending[stream_id].put_nowait(data)
            return

        # Первый пакет → control header, начинаем открывать
        q: asyncio.Queue = asyncio.Queue()
        self._pending[stream_id] = q
        asyncio.create_task(self._open_connection(stream_id, data, q))

    def _on_tunnel_end(self, stream_id: int) -> None:
        conn = self._connections.pop(stream_id, None)
        if conn:
            conn.close()
            return

        if stream_id in self._pending:
            self._pending_fin.add(stream_id)

    async def _open_connection(self, stream_id: int, control_data: bytes, pending_q: asyncio.Queue) -> None:
        try:
            atyp, addr, port, consumed = decode_control(control_data)
            log.info("ExitNode открывает %s:%d (stream=%d)", addr, port, stream_id)
        except ValueError as e:
            log.warning("Плохой control header: %s", e)
            self._pending.pop(stream_id, None)
            await self._mgr.send(stream_id=stream_id, data=b"", fin=True)
            return

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(addr, port),
                timeout=10.0,
            )
        except Exception as e:
            log.warning("Не смогли подключиться к %s:%d — %s", addr, port, e)
            self._pending.pop(stream_id, None)
            await self._mgr.send(stream_id=stream_id, data=b"", fin=True)
            return

        conn = TargetConnection(stream_id, reader, writer, self._mgr)

        # Если в первом пакете после control были ещё данные — пишем в target
        leftover = control_data[consumed:]
        if leftover:
            conn.write(leftover)

        # Сливаем буфер накопленных пакетов (пришедших пока открывали соединение)
        while not pending_q.empty():
            buffered = pending_q.get_nowait()
            conn.write(buffered)

        # Регистрируем готовое соединение
        self._connections[stream_id] = conn
        self._pending.pop(stream_id, None)

        # Если за время открытия прилетел FIN — закрываем
        if stream_id in self._pending_fin:
            self._pending_fin.discard(stream_id)
            conn.close()
            self._connections.pop(stream_id, None)
            return

        # Читаем из target → отправляем в туннель
        asyncio.create_task(conn.pump_to_tunnel())


class TargetConnection:
    """Одно реальное TCP-соединение с внешним хостом."""

    def __init__(
        self,
        stream_id: int,
        reader:    asyncio.StreamReader,
        writer:    asyncio.StreamWriter,
        manager:   ChannelManager,
    ) -> None:
        self._stream_id = stream_id
        self._reader    = reader
        self._writer    = writer
        self._mgr       = manager
        self._closed    = False

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._writer.write(data)
        except Exception:
            pass

    async def pump_to_tunnel(self) -> None:
        try:
            while not self._closed:
                data = await self._reader.read(4096)
                if not data:
                    await self._mgr.send(stream_id=self._stream_id, data=b"", fin=True)
                    break
                await self._mgr.send(stream_id=self._stream_id, data=data, fin=False)
        except Exception:
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            pass
