# filepath: c:\Olegshifter\android_kivy_client\main.py
"""
OlegShifter Android Client - Kivy GUI
Полноценное приложение для Android с UI для управления прокси
"""

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.clock import Clock

import threading
import asyncio
import logging
import json
import os
from datetime import datetime
from typing import Optional

# Настройка логирования
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Импортируем наш клиент
from client import client as client_module
from client import shared

# Размер окна по умолчанию
Window.size = (420, 800)

# Путь для сохранения конфигурации
CONFIG_PATH = os.path.expanduser('~/.olegshifter')


class ProxyClientApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.title = "OlegShifter"
        self.client_app = None
        self.loop = None
        self.is_running = False
        self.status = "Отключено"
        self.client_thread = None
        
        os.makedirs(CONFIG_PATH, exist_ok=True)
        self.config_file = os.path.join(CONFIG_PATH, 'config.json')
        self.config = self._load_config()
        
    def _load_config(self):
        default_config = {
            'host': shared.SERVER_HOST,
            'port': shared.SERVER_BASE_PORT,
            'channels': shared.NUM_CHANNELS,
            'socks5_port': shared.SOCKS5_PORT,
            'preshared': shared.PRESHARED,
            'use_tls': False,
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded = json.load(f)
                    return {**default_config, **loaded}
            except Exception as e:
                log.warning(f"Не удалось загрузить конфиг: {e}")
                return default_config
        return default_config
    
    def _save_config(self):
        config = {
            'host': self.host_input.text,
            'port': self.port_input.text,
            'channels': self.channels_input.text,
            'socks5_port': self.socks5_port_input.text,
            'preshared': self.preshared_input.text,
            'use_tls': self.tls_spinner.text == 'Yes',
        }
        try:
            os.makedirs(CONFIG_PATH, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            self.log(" Конфиг сохранён", "INFO")
        except Exception as e:
            self.log(f"Ошибка сохранения конфига: {e}", "ERROR")
        
    def build(self):
        main_layout = BoxLayout(orientation='vertical', padding=10, spacing=10)
        main_layout.canvas.before.clear()
        with main_layout.canvas.before:
            Color(0.1, 0.1, 0.15, 1)
            Rectangle(size=main_layout.size, pos=main_layout.pos)
        
        header = Label(
            text='[b]OlegShifter[/b]\nSOCKS5 Proxy Client',
            markup=True,
            size_hint_y=0.15,
            font_size='20sp'
        )
        main_layout.add_widget(header)
        
        config_layout = self._build_config_section()
        main_layout.add_widget(config_layout)
        
        buttons_layout = self._build_buttons_section()
        main_layout.add_widget(buttons_layout)
        
        self.status_label = Label(
            text='[b]Статус:[/b] Отключено',
            markup=True,
            size_hint_y=0.1,
            font_size='14sp'
        )
        main_layout.add_widget(self.status_label)
        
        log_section = self._build_log_section()
        main_layout.add_widget(log_section)
        
        return main_layout
    
    def _build_config_section(self):
        layout = BoxLayout(orientation='vertical', size_hint_y=0.4, spacing=5)
        layout.canvas.before.clear()
        with layout.canvas.before:
            Color(0.15, 0.15, 0.2, 1)
            Rectangle(size=layout.size, pos=layout.pos)
        
        config_title = Label(text='[b]Конфигурация[/b]', markup=True, size_hint_y=0.1, font_size='16sp')
        layout.add_widget(config_title)
        
        form_layout = GridLayout(cols=2, spacing=5, size_hint_y=0.9, padding=5)
        
        form_layout.add_widget(Label(text='Host:', size_hint_x=0.3))
        self.host_input = TextInput(text=self.config.get('host', shared.SERVER_HOST), multiline=False, size_hint_x=0.7)
        form_layout.add_widget(self.host_input)
        
        form_layout.add_widget(Label(text='Port:', size_hint_x=0.3))
        self.port_input = TextInput(text=str(self.config.get('port', shared.SERVER_BASE_PORT)), multiline=False, size_hint_x=0.7, input_filter='int')
        form_layout.add_widget(self.port_input)
        
        form_layout.add_widget(Label(text='Channels:', size_hint_x=0.3))
        self.channels_input = TextInput(text=str(self.config.get('channels', shared.NUM_CHANNELS)), multiline=False, size_hint_x=0.7, input_filter='int')
        form_layout.add_widget(self.channels_input)
        
        form_layout.add_widget(Label(text='SOCKS5 Port:', size_hint_x=0.3))
        self.socks5_port_input = TextInput(text=str(self.config.get('socks5_port', shared.SOCKS5_PORT)), multiline=False, size_hint_x=0.7, input_filter='int')
        form_layout.add_widget(self.socks5_port_input)
        
        form_layout.add_widget(Label(text='Preshared:', size_hint_x=0.3))
        self.preshared_input = TextInput(text=self.config.get('preshared', shared.PRESHARED), multiline=False, password=True, size_hint_x=0.7)
        form_layout.add_widget(self.preshared_input)
        
        form_layout.add_widget(Label(text='Use TLS:', size_hint_x=0.3))
        use_tls = 'Yes' if self.config.get('use_tls', False) else 'No'
        self.tls_spinner = Spinner(text=use_tls, values=('Yes', 'No'), size_hint_x=0.7)
        form_layout.add_widget(self.tls_spinner)
        
        layout.add_widget(form_layout)
        return layout
    
    def _build_buttons_section(self):
        layout = BoxLayout(orientation='vertical', size_hint_y=0.18, spacing=8, padding=5)
        
        buttons_row1 = BoxLayout(orientation='horizontal', size_hint_y=0.5, spacing=10)
        
        self.connect_btn = Button(text=' Подключиться', background_color=(0.2, 0.6, 0.2, 1), size_hint_x=0.5)
        self.connect_btn.bind(on_press=self.on_connect)
        buttons_row1.add_widget(self.connect_btn)
        
        self.disconnect_btn = Button(text=' Отключиться', background_color=(0.6, 0.2, 0.2, 1), size_hint_x=0.5, disabled=True)
        self.disconnect_btn.bind(on_press=self.on_disconnect)
        buttons_row1.add_widget(self.disconnect_btn)
        
        layout.add_widget(buttons_row1)
        
        buttons_row2 = BoxLayout(orientation='horizontal', size_hint_y=0.5, spacing=10)
        
        clear_log_btn = Button(text='Очистить логи', background_color=(0.4, 0.4, 0.6, 1), size_hint_x=0.5)
        clear_log_btn.bind(on_press=self.on_clear_logs)
        buttons_row2.add_widget(clear_log_btn)
        
        save_config_btn = Button(text='Сохранить', background_color=(0.5, 0.5, 0.3, 1), size_hint_x=0.5)
        save_config_btn.bind(on_press=lambda x: self._save_config())
        buttons_row2.add_widget(save_config_btn)
        
        layout.add_widget(buttons_row2)
        
        return layout
    
    def _build_log_section(self):
        layout = BoxLayout(orientation='vertical', size_hint_y=0.3, spacing=5)
        layout.canvas.before.clear()
        with layout.canvas.before:
            Color(0.08, 0.08, 0.1, 1)
            Rectangle(size=layout.size, pos=layout.pos)
        
        log_title = Label(text='[b]Логи[/b]', markup=True, size_hint_y=0.1, font_size='14sp')
        layout.add_widget(log_title)
        
        scroll = ScrollView(size_hint=(1, 0.9))
        self.log_label = Label(text='[Логи появятся здесь]\n', markup=True, size_hint_y=None, text_size=(350, None))
        self.log_label.bind(texture_size=self.log_label.setter('size'))
        scroll.add_widget(self.log_label)
        layout.add_widget(scroll)
        
        return layout
    
    def log(self, message, level='INFO'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        colors = {'INFO': '[color=00FF00]', 'WARNING': '[color=FFFF00]', 'ERROR': '[color=FF0000]', 'SUCCESS': '[color=00FF00]'}
        color = colors.get(level, '[color=FFFFFF]')
        log_msg = f"{color}[{timestamp}] {level}:[/color] {message}\n"
        self.log_label.text += log_msg
        log.info(f"{level}: {message}")
        Clock.schedule_once(lambda dt: setattr(self.log_label.parent, 'scroll_y', 0), 0.1)
    
    def on_clear_logs(self, instance):
        self.log_label.text = '[Логи очищены]\n'
        self.log("Логи очищены", "INFO")
    
    def on_connect(self, instance):
        try:
            host = self.host_input.text.strip()
            port = int(self.port_input.text)
            channels = int(self.channels_input.text)
            socks5_port = int(self.socks5_port_input.text)
            preshared = self.preshared_input.text.strip()
            
            if not host:
                raise ValueError("Host не может быть пустым")
            if port <= 0 or port > 65535:
                raise ValueError("Port должен быть от 1 до 65535")
            if channels <= 0 or channels > 100:
                raise ValueError("Channels должен быть от 1 до 100")
            if socks5_port <= 0 or socks5_port > 65535:
                raise ValueError("SOCKS5 Port должен быть от 1 до 65535")
            if not preshared:
                raise ValueError("Preshared Secret не может быть пустым")
        except ValueError as e:
            self.log(f"Ошибка валидации: {str(e)}", "ERROR")
            self.status_label.text = f'[b]Статус:[/b] [color=FF0000]Ошибка входа[/color]'
            return
        
        self.connect_btn.disabled = True
        self.disconnect_btn.disabled = False
        
        self.log("Инициализация подключения...", "INFO")
        
        shared.SERVER_HOST = host
        shared.SERVER_BASE_PORT = port
        shared.NUM_CHANNELS = channels
        shared.SOCKS5_PORT = socks5_port
        shared.PRESHARED = preshared
        shared.TLS_INSECURE = self.tls_spinner.text != 'Yes'
        
        self._save_config()
        
        self.client_thread = threading.Thread(target=self._run_client_thread, daemon=True)
        self.client_thread.start()
    
    def _run_client_thread(self):
        try:
            self.log("Создание Event Loop...", "INFO")
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            self.log("Инициализация прокси-клиента...", "INFO")
            self.client_app = client_module.ClientApp()
            
            self.log("Подключение к серверу...", "INFO")
            self.loop.run_until_complete(self.client_app.start())
            
            self.is_running = True
            self.status = "Подключено"
            self.status_label.text = '[b]Статус:[/b] [color=00FF00] Подключено[/color]'
            self.log(" Клиент успешно запущен!", "SUCCESS")
            
            self.loop.run_forever()
        except asyncio.CancelledError:
            self.log("Подключение отменено", "WARNING")
        except ConnectionRefusedError:
            self.is_running = False
            self.status = "Ошибка подключения"
            self.status_label.text = '[b]Статус:[/b] [color=FF0000] Ошибка: сервер недоступен[/color]'
            self.log("Ошибка: сервер недоступен (Connection Refused)", "ERROR")
            self.connect_btn.disabled = False
            self.disconnect_btn.disabled = True
        except TimeoutError:
            self.is_running = False
            self.status = "Ошибка подключения"
            self.status_label.text = '[b]Статус:[/b] [color=FF0000] Ошибка: timeout[/color]'
            self.log("Ошибка: истекло время ожидания подключения", "ERROR")
            self.connect_btn.disabled = False
            self.disconnect_btn.disabled = True
        except OSError as e:
            self.is_running = False
            self.status = "Ошибка ОС"
            self.status_label.text = f'[b]Статус:[/b] [color=FF0000] Ошибка: {str(e)[:50]}[/color]'
            self.log(f"Ошибка ОС: {str(e)}", "ERROR")
            self.connect_btn.disabled = False
            self.disconnect_btn.disabled = True
        except Exception as e:
            self.is_running = False
            self.status = "Ошибка"
            error_msg = str(e)[:100]
            self.status_label.text = f'[b]Статус:[/b] [color=FF0000] Ошибка[/color]'
            self.log(f"Ошибка подключения: {error_msg}", "ERROR")
            self.connect_btn.disabled = False
            self.disconnect_btn.disabled = True
        finally:
            if self.loop:
                try:
                    self.loop.close()
                except:
                    pass
                self.loop = None
    
    def on_disconnect(self, instance):
        self.log("Отключение...", "INFO")
        self.connect_btn.disabled = False
        self.disconnect_btn.disabled = True
        
        if self.client_app:
            thread = threading.Thread(target=self._stop_client, daemon=True)
            thread.start()
    
    def _stop_client(self):
        try:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            
            if self.client_app:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.client_app.stop())
                loop.close()
            
            self.is_running = False
            self.status = "Отключено"
            self.status_label.text = '[b]Статус:[/b] [color=FF9900]Отключено[/color]'
            self.log(" Клиент остановлен", "INFO")
            self.client_app = None
        except Exception as e:
            self.log(f"Ошибка при отключении: {str(e)}", "ERROR")


if __name__ == "__main__":
    app = ProxyClientApp()
    app.run()
