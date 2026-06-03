"""
Olegshifter — клиент с GUI (PyQt6 + qasync).

Запуск:
    python apps/client_gui.py

Зависимости: PyQt6, qasync. См. apps/requirements.txt.

Что делает:
  - Окно настроек (сервер, каналы, SOCKS5, TLS).
  - Кнопка Подключить / Отключить.
  - Индикаторы каналов в реальном времени.
  - Системный трей — окно прячется в трей при закрытии.
  - Конфиг лежит в ~/.olegshifter/client.json.
"""

import os
import sys
import asyncio
from datetime import datetime
from typing import Dict, Optional

# --- путь до core/ ---
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon, QAction, QPixmap, QColor, QPainter
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QSpinBox, QCheckBox, QTextEdit,
        QSystemTrayIcon, QMenu, QTabWidget, QFormLayout, QGroupBox,
        QStatusBar, QMessageBox,
    )
    import qasync
except ImportError as e:
    print("Не хватает зависимостей:", e)
    print("Установи: pip install -r apps/requirements.txt")
    sys.exit(1)

from transport import WebSocketClientTransport, WSConfig, Transport
from channel_manager import ChannelManager, ChannelManagerConfig
from multipath import SplitMode
from padding import PaddingConfig, PaddingStrategy
from socks5 import SOCKS5Server, SOCKS5Config
from crypto_core import CryptoSession, _derive_keys

def _make_crypto(transports, preshared: str):
    c2s, s2c = _derive_keys(
        b"".join(transports[k].shared_key for k in sorted(transports)),
        preshared.encode(),
        b"session_c2s", b"session_s2c",
    )
    return CryptoSession(c2s, s2c, is_initiator=True)

from app_config import load_client_config, save_client_config


# --- мини-иконка (кружок нужного цвета) ---

def make_icon(color: str, size: int = 64) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(8, 8, size - 16, size - 16)
    painter.end()
    return QIcon(pix)


# --- движок (async-часть) ---

_RECONNECT_BASE = 1.0
_RECONNECT_CAP  = 60.0


class ClientEngine:
    """Поднимает транспорты → крипто → multipath → SOCKS5. Авто-реконнект каналов."""

    def __init__(self, cfg: dict, on_log, on_channel) -> None:
        self._cfg = cfg
        self._on_log = on_log
        self._on_channel = on_channel
        self._transports: Dict[int, Transport] = {}
        self._manager: Optional[ChannelManager] = None
        self._socks5: Optional[SOCKS5Server] = None
        self._reconnect_tasks: Dict[int, asyncio.Task] = {}

    def _make_ws_config(self, ch_id: int) -> WSConfig:
        cfg    = self._cfg
        use_tls = cfg["use_tls"]
        scheme = "wss" if use_tls else "ws"
        host   = cfg["server_host"].strip()
        port   = cfg["server_port"] + ch_id
        path   = cfg.get("ws_path", "/ws").strip()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{scheme}://{host}:{port}{path}"
        sni = (cfg["tls_server_name"] or "").strip() or None
        return WSConfig(
            url                 = url,
            preshared           = cfg["preshared"].encode(),
            tls_verify_ca       = (cfg["tls_ca_path"] or "").strip() or None,
            tls_insecure        = cfg["tls_insecure"],
            tls_server_hostname = sni if use_tls else None,
        )

    async def start(self) -> None:
        cfg = self._cfg
        n   = cfg["num_channels"]

        try:
            for ch_id in range(n):
                wsc = self._make_ws_config(ch_id)
                self._on_log(f"канал {ch_id} → {wsc.url}", "info")
                t = WebSocketClientTransport(wsc)
                await t.connect()
                self._transports[ch_id] = t
                self._on_channel(ch_id, True)
                self._on_log(f"канал {ch_id} подключён", "info")

            crypto = _make_crypto(self._transports, cfg["preshared"])

            mgr_cfg = ChannelManagerConfig(
                split_mode          = SplitMode.ADAPTIVE,
                chunk_size          = 1024,
                padding_cfg         = PaddingConfig(strategy=PaddingStrategy.BIMODAL),
                ping_interval_sec   = 15.0,
                channel_timeout_sec = 60.0,
            )
            self._manager = ChannelManager(crypto, self._transports, mgr_cfg)
            self._manager.set_on_channel_down(self._on_channel_down)
            await self._manager.start()

            self._socks5 = SOCKS5Server(
                self._manager,
                SOCKS5Config(host=cfg["socks5_host"], port=cfg["socks5_port"]),
            )
            await self._socks5.start()
            self._on_log(
                f"SOCKS5 слушает {cfg['socks5_host']}:{cfg['socks5_port']}", "info",
            )
        except Exception:
            await self._cleanup()
            raise

    def _on_channel_down(self, ch_id: int, reason: str) -> None:
        self._on_log(f"канал {ch_id} упал: {reason} — переподключаю...", "warn")
        self._on_channel(ch_id, False)
        task = self._reconnect_tasks.get(ch_id)
        if task is None or task.done():
            self._reconnect_tasks[ch_id] = asyncio.create_task(
                self._reconnect_loop(ch_id)
            )

    async def _reconnect_loop(self, ch_id: int) -> None:
        delay = _RECONNECT_BASE
        while self._manager is not None:
            await asyncio.sleep(delay)
            try:
                t = WebSocketClientTransport(self._make_ws_config(ch_id))
                await t.connect()
                await self._manager.replace_channel(ch_id, t)
                self._on_channel(ch_id, True)
                self._on_log(f"канал {ch_id} переподключён", "info")
                return
            except Exception as e:
                self._on_log(f"канал {ch_id}: реконнект неудачен ({e}), жду {delay:.0f}s", "warn")
                delay = min(delay * 2, _RECONNECT_CAP)

    async def stop(self) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        for t in self._reconnect_tasks.values():
            t.cancel()
        self._reconnect_tasks.clear()
        if self._socks5:
            try:
                await self._socks5.stop()
            except Exception:
                pass
            self._socks5 = None
        if self._manager:
            try:
                await self._manager.close()
            except Exception:
                pass
            self._manager = None
        for t in self._transports.values():
            try:
                await t.close()
            except Exception:
                pass
        self._transports.clear()


# --- окно ---

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Olegshifter — клиент")
        self.resize(680, 560)
        self.cfg = load_client_config()
        self.engine: Optional[ClientEngine] = None
        self._busy = False

        self._channel_labels = []
        self._build_ui()
        self._build_tray()

    # --- UI ---

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        tabs = QTabWidget()
        tabs.addTab(self._build_connection_tab(), "Подключение")
        tabs.addTab(self._build_log_tab(), "Лог")
        layout.addWidget(tabs)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Отключено")

    def _build_connection_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        srv = QGroupBox("Сервер")
        srv_form = QFormLayout(srv)
        self.e_host = QLineEdit(self.cfg["server_host"])
        self.e_port = QSpinBox(); self.e_port.setRange(1, 65535)
        self.e_port.setValue(self.cfg["server_port"])
        self.e_channels = QSpinBox(); self.e_channels.setRange(1, 16)
        self.e_channels.setValue(self.cfg["num_channels"])
        self.e_preshared = QLineEdit(self.cfg["preshared"])
        self.e_preshared.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_ws_path = QLineEdit(self.cfg.get("ws_path", "/ws"))
        srv_form.addRow("Хост:", self.e_host)
        srv_form.addRow("Базовый порт:", self.e_port)
        srv_form.addRow("Каналов:", self.e_channels)
        srv_form.addRow("Preshared:", self.e_preshared)
        srv_form.addRow("WS path:", self.e_ws_path)
        layout.addWidget(srv)

        tls = QGroupBox("TLS (wss://)")
        tls_form = QFormLayout(tls)
        self.c_tls = QCheckBox("Включить TLS")
        self.c_tls.setChecked(self.cfg["use_tls"])
        self.c_tls_insecure = QCheckBox("Не проверять сертификат (dev)")
        self.c_tls_insecure.setChecked(self.cfg["tls_insecure"])
        self.e_tls_ca = QLineEdit(self.cfg["tls_ca_path"])
        self.e_tls_sni = QLineEdit(self.cfg["tls_server_name"])
        tls_form.addRow(self.c_tls)
        tls_form.addRow(self.c_tls_insecure)
        tls_form.addRow("CA path:", self.e_tls_ca)
        tls_form.addRow("SNI:", self.e_tls_sni)
        layout.addWidget(tls)

        socks = QGroupBox("Локальный SOCKS5 (для браузера)")
        socks_form = QFormLayout(socks)
        self.e_socks_host = QLineEdit(self.cfg["socks5_host"])
        self.e_socks_port = QSpinBox(); self.e_socks_port.setRange(1, 65535)
        self.e_socks_port.setValue(self.cfg["socks5_port"])
        socks_form.addRow("Хост:", self.e_socks_host)
        socks_form.addRow("Порт:", self.e_socks_port)
        layout.addWidget(socks)

        self.channels_box = QGroupBox("Каналы")
        self.channels_layout = QHBoxLayout(self.channels_box)
        self._rebuild_channel_indicators(self.cfg["num_channels"])
        layout.addWidget(self.channels_box)

        btns = QHBoxLayout()
        self.btn_connect = QPushButton("Подключить")
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        self.btn_save = QPushButton("Сохранить настройки")
        self.btn_save.clicked.connect(self._on_save_clicked)
        btns.addWidget(self.btn_connect)
        btns.addWidget(self.btn_save)
        btns.addStretch()
        layout.addLayout(btns)
        layout.addStretch()
        return w

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return w

    def _rebuild_channel_indicators(self, n: int) -> None:
        while self.channels_layout.count():
            item = self.channels_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._channel_labels = []
        for i in range(n):
            lbl = QLabel(f"● ch{i}")
            lbl.setStyleSheet("color: #888; font-size: 16pt;")
            self.channels_layout.addWidget(lbl)
            self._channel_labels.append(lbl)
        self.channels_layout.addStretch()

    def _set_channel_state(self, ch_id: int, ok: bool) -> None:
        if 0 <= ch_id < len(self._channel_labels):
            color = "#2ecc71" if ok else "#e74c3c"
            self._channel_labels[ch_id].setStyleSheet(
                f"color: {color}; font-size: 16pt;"
            )

    # --- tray ---

    def _build_tray(self) -> None:
        self.icon_off = make_icon("#888")
        self.icon_on  = make_icon("#2ecc71")
        self.tray = QSystemTrayIcon(self.icon_off, self)
        self.tray.setToolTip("Olegshifter — отключено")

        menu = QMenu()
        act_show = QAction("Открыть окно", self)
        act_show.triggered.connect(self._show_window)
        menu.addAction(act_show)
        self.act_toggle = QAction("Подключить", self)
        self.act_toggle.triggered.connect(self._on_connect_clicked)
        menu.addAction(self.act_toggle)
        menu.addSeparator()
        act_quit = QAction("Выход", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def _show_window(self) -> None:
        self.show()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, event) -> None:
        # сворачиваемся в трей вместо выхода
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "Olegshifter",
            "Приложение свёрнуто в трей. Правый клик → Выход чтобы закрыть.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _quit(self) -> None:
        async def _shutdown():
            if self.engine:
                try:
                    await self.engine.stop()
                except Exception:
                    pass
            QApplication.instance().quit()
        asyncio.ensure_future(_shutdown())

    # --- действия ---

    def _gather_cfg(self) -> None:
        self.cfg = {
            "server_host":     self.e_host.text().strip(),
            "server_port":     self.e_port.value(),
            "num_channels":    self.e_channels.value(),
            "preshared":       self.e_preshared.text(),
            "ws_path":         self.e_ws_path.text().strip() or "/ws",
            "use_tls":         self.c_tls.isChecked(),
            "tls_insecure":    self.c_tls_insecure.isChecked(),
            "tls_ca_path":     self.e_tls_ca.text().strip(),
            "tls_server_name": self.e_tls_sni.text().strip(),
            "socks5_host":     self.e_socks_host.text().strip(),
            "socks5_port":     self.e_socks_port.value(),
        }

    def _on_save_clicked(self) -> None:
        self._gather_cfg()
        save_client_config(self.cfg)
        self._append_log("настройки сохранены", "info")

    def _on_connect_clicked(self) -> None:
        if self._busy:
            return
        if self.engine is None:
            asyncio.ensure_future(self._start())
        else:
            asyncio.ensure_future(self._stop())

    async def _start(self) -> None:
        self._busy = True
        self._gather_cfg()
        save_client_config(self.cfg)
        self._rebuild_channel_indicators(self.cfg["num_channels"])
        self._set_busy_ui(True, "Подключение...")

        try:
            self.engine = ClientEngine(
                self.cfg, self._append_log, self._set_channel_state,
            )
            await self.engine.start()
            self._set_connected(True)
        except Exception as e:
            self._append_log(f"ошибка: {e}", "error")
            self.engine = None
            self._set_connected(False)
            QMessageBox.critical(self, "Ошибка подключения", str(e))
        finally:
            self._busy = False
            self._set_busy_ui(False)

    async def _stop(self) -> None:
        self._busy = True
        self._set_busy_ui(True, "Отключение...")
        try:
            if self.engine:
                await self.engine.stop()
        except Exception as e:
            self._append_log(f"ошибка при отключении: {e}", "error")
        finally:
            self.engine = None
            self._set_connected(False)
            self._busy = False
            self._set_busy_ui(False)

    def _set_busy_ui(self, busy: bool, text: Optional[str] = None) -> None:
        self.btn_connect.setEnabled(not busy)
        self.act_toggle.setEnabled(not busy)
        if busy and text:
            self.btn_connect.setText(text)

    def _set_connected(self, on: bool) -> None:
        if on:
            self.btn_connect.setText("Отключить")
            self.act_toggle.setText("Отключить")
            self.tray.setIcon(self.icon_on)
            self.tray.setToolTip(
                f"Olegshifter — SOCKS5 {self.cfg['socks5_host']}:{self.cfg['socks5_port']}"
            )
            self.status.showMessage(
                f"Подключено • SOCKS5 {self.cfg['socks5_host']}:{self.cfg['socks5_port']}"
            )
        else:
            self.btn_connect.setText("Подключить")
            self.act_toggle.setText("Подключить")
            self.tray.setIcon(self.icon_off)
            self.tray.setToolTip("Olegshifter — отключено")
            self.status.showMessage("Отключено")
            for lbl in self._channel_labels:
                lbl.setStyleSheet("color: #888; font-size: 16pt;")

    def _append_log(self, msg: str, level: str = "info") -> None:
        t = datetime.now().strftime("%H:%M:%S")
        colors = {"info": "#2c3e50", "error": "#c0392b", "warn": "#d35400"}
        color = colors.get(level, "#2c3e50")
        self.log_view.append(
            f'<span style="color:#888;">{t}</span> '
            f'<span style="color:{color};">{msg}</span>'
        )


# --- запуск ---

def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None, "Трей не доступен",
            "Система не поддерживает иконку в трее — приложение будет работать только в окне.",
        )

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow()
    win.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
