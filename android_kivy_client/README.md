# 📱 OlegShifter Android Kivy Client

Полнофункциональное Android-приложение для подключения к SOCKS5-прокси OlegShifter.

## ✨ Возможности

- ✅ **Красивый Kivy GUI** - Интуитивный интерфейс на Python
- ✅ **Конфигурация прокси** - Host, Port, Channels, SOCKS5 Port, Preshared Secret, TLS
- ✅ **Быстрое подключение** - One-click connect/disconnect
- ✅ **Локальный SOCKS5 сервер** - Другие приложения могут использовать прокси
- ✅ **Живое логирование** - Real-time логи в UI с цветовой подсветкой
- ✅ **Статус подключения** - Визуальный индикатор (Подключено/Отключено/Ошибка)
- ✅ **Сохранение конфигурации** - Параметры сохраняются между сеансами
- ✅ **Многоканальная поддержка** - Параллельные каналы для лучшей производительности
- ✅ **Шифрование** - ChaCha20-Poly1305 для безопасности

## 📁 Структура проекта

```
android_kivy_client/
├── main.py                    # Главный Kivy-интерфейс (главный файл)
├── run_desktop.py             # Скрипт для локального тестирования
├── requirements.txt           # Python зависимости
├── buildozer.spec             # Конфиг для сборки APK
├── Build_APK_Colab_v2.ipynb   # Улучшенный Jupyter ноутбук (Google Colab)
├── Build_APK_Colab.ipynb      # Оригинальный ноутбук (для совместимости)
├── build_apk.sh               # Bash-скрипт для Linux/WSL
├── olegshifter.png            # Иконка приложения
├── README.md                  # Этот файл
└── client/                    # Копия модулей прокси-клиента
    ├── client.py              # Главный класс ClientApp
    ├── shared.py              # Общие конфигурации
    ├── transport.py           # WebSocket транспорт
    ├── socks5.py              # SOCKS5 сервер
    ├── channel_manager.py     # Управление каналами
    ├── crypto_core.py         # ChaCha20-Poly1305 шифрование
    ├── multipath.py           # Фрейминг для multi-path
    ├── padding.py             # Трафик padding
    ├── grpc_transport.py       # gRPC fallback
    └── tls_utils.py           # TLS утилиты
```

## 🚀 Быстрый старт

### Вариант 1: Google Colab (Рекомендуется для Windows/Mac)

**Преимущества:**
- Никаких требований к системе
- Полностью автоматизированная сборка
- Бесплатно (используется Google инфраструктура)
- Сборка занимает 10-15 минут

**Шаги:**

1. Откройте https://colab.research.google.com/
2. **File** → **Open notebook** → **Upload** → выберите `Build_APK_Colab_v2.ipynb`
3. Измените URL GitHub репозитория в ячейке 3 (Шаг 3)
4. Выполните ячейки по порядку (Shift+Enter)
5. После сборки загрузите APK и установите на Android

### Вариант 2: Локальная сборка (Linux/WSL на Windows)

**Требования:**
- Ubuntu/Debian (или WSL2 на Windows)
- Python 3.8+
- Java 17 JDK
- ~10 GB свободного места

**Установка:**

```bash
# 1. Установите системные зависимости
sudo apt update
sudo apt install -y build-essential git python3-dev python3-pip \
    openjdk-17-jdk unzip wget

# 2. Установите Python tools
pip install --upgrade pip
pip install buildozer cython kivy

# 3. Перейдите в папку проекта
cd android_kivy_client

# 4. Соберите APK
buildozer -v android debug

# 5. Результат в:
ls -la bin/
```

**На Windows используйте WSL:**

```powershell
# PowerShell (от администратора):
wsl --install Ubuntu

# Затем в WSL терминале выполните команды bash выше
```

### Вариант 3: Локальное тестирование (все платформы)

Перед сборкой APK протестируйте приложение локально:

```bash
# На Windows (PowerShell):
python run_desktop.py

# На Linux/Mac:
python3 run_desktop.py
```

**Требования для локального тестирования:**

```bash
pip install kivy cryptography websockets protobuf pyjnius cython cffi
```

## 🔧 Конфигурация приложения

### main.py - Главный интерфейс

**Основные компоненты:**

| Компонент | Назначение |
|-----------|-----------|
| ProxyClientApp | Главный класс приложения (наследует App) |
| build() | Построение UI (главный метод) |
| _build_config_section() | Форма параметров прокси |
| _build_buttons_section() | Кнопки управления (Connect/Disconnect/Clear/Save) |
| _build_log_section() | Область логирования |
| _run_client_thread() | Запуск прокси в отдельном потоке |
| _stop_client() | Остановка прокси |

**Сохранение конфигурации:**

- Конфиг сохраняется в `~/.olegshifter/config.json`
- Автоматически загружается при запуске
- Экспортируется в `shared` для клиента

### buildozer.spec - Конфигурация сборки

**Ключевые параметры:**

```ini
[app]
package.name = olegshifter          # Имя пакета (для Android)
package.domain = org.olegshifter    # Доменное имя (обратный порядок)
android.permissions = INTERNET,ACCESS_NETWORK_STATE,...
android.api = 31                    # API уровень (минимум 21)
android.minapi = 21                 # Минимальный API уровень
android.ndk = 25b                   # NDK версия
requirements = python3,kivy,cryptography,websockets,protobuf,pyjnius
```

## 🔌 Использование приложения

### Первый запуск

1. **Установите APK** на Android устройство
2. **Откройте приложение** OlegShifter
3. **Введите параметры сервера:**

| Параметр | Описание | Пример |
|----------|---------|--------|
| Host | IP или домен прокси-сервера | example.com или 192.168.1.100 |
| Port | Порт сервера | 8080, 443, 9999 |
| Channels | Количество параллельных каналов | 1-10 (рекомендуется 4) |
| SOCKS5 Port | Локальный SOCKS5 порт | 1080 (по умолчанию) |
| Preshared Secret | Ключ доступа (если требуется) | your_secret_key |
| Use TLS | Включить TLS | Yes / No |

4. **Нажмите "Подключиться"**
5. **Смотрите логи** - должны появиться сообщения подключения

### Конфигурация других приложений

После подключения OlegShifter вы можете настроить другие приложения использовать SOCKS5:

**Браузер (Firefox, Chrome):**
- Settings → Network → SOCKS proxy
- Host: localhost, Port: 1080, SOCKS5 выбрать

**Telegram:**
- Settings → Advanced → Proxy settings
- SOCKS5, localhost:1080

**Другие приложения:**
- Смотрите документацию приложения для SOCKS5 конфигурации

## 📊 Информация о логах

**Цветовая кодировка:**

- 🟢 **INFO** (зелёный) - Обычная информация
- 🟡 **WARNING** (жёлтый) - Предупреждения
- 🔴 **ERROR** (красный) - Ошибки
- 🟢 **SUCCESS** (зелёный) - Успешные события

**Типичные сообщения:**

```
[10:30:45] INFO: Инициализация подключения...
[10:30:46] INFO: Создание Event Loop...
[10:30:47] INFO: Инициализация прокси-клиента...
[10:30:48] INFO: Подключение к серверу...
[10:31:05] SUCCESS: ✓ Клиент успешно запущен!
[10:31:05] INFO: ✓ Конфиг сохранён
```

## 🔍 Решение проблем

### Приложение не подключается

**Проблема:** Кнопка "Подключиться" нажимается, но подключение не происходит

**Решение:**

1. Проверьте сервер:
   ```bash
   ping host_вашего_сервера
   telnet host 8080  # Проверьте доступность портра
   ```

2. Проверьте логи в приложении:
   - ERROR: Connection refused → Сервер не слушает порт
   - ERROR: timeout → Сервер недоступен (firewall/сеть)
   - ERROR: Authentication failed → Неправильный Preshared Secret

3. Проверьте параметры:
   - Host правильный?
   - Port правильный?
   - Preshared Secret совпадает с сервером?

### APK не устанавливается

**Проблема:** "App not installed" при попытке установить APK

**Решение:**

1. На телефоне разрешите установку из неизвестных источников:
   - Settings → Security → Unknown sources → ✓

2. Проверьте свободное место:
   - APK занимает ~50-80 MB
   - На диске должно быть >= 200 MB

3. Убедитесь, что версия Android совместима:
   - Минимум: Android 5.0 (API 21)
   - Рекомендуется: Android 8.0+ (API 26+)

4. Попробуйте удалить старую версию:
   - Settings → Apps → OlegShifter → Uninstall
   - Затем установите новую

### Проблемы с ADB (установка через USB)

**Проблема:** `adb: command not found` или `device not found`

**Решение:**

**Windows (PowerShell):**
```powershell
# Установите ADB
winget install Google.AndroidStudio

# Или вручную из Android SDK Platform Tools:
# https://developer.android.com/tools/releases/platform-tools

# Включите USB Debug на телефоне:
# Settings → Developer Options → USB Debugging

# Перезапустите ADB:
adb kill-server
adb start-server

# Проверьте подключение:
adb devices

# Установите APK:
adb install olegshifter-1.0.0-debug.apk
```

**Linux:**
```bash
sudo apt install android-tools-adb

# Включите USB Debug на телефоне (как выше)

adb devices
adb install olegshifter-1.0.0-debug.apk
```

**Mac:**
```bash
brew install android-platform-tools

adb devices
adb install olegshifter-1.0.0-debug.apk
```

### Сборка APK не работает в Colab

**Проблема:** Ошибка при выполнении `buildozer android debug`

**Типичные ошибки:**

1. **"git not found"** → Git не установлен
   - Решение: `!apt-get install git` в первой ячейке

2. **"No such file or directory: build.xml"** → Buildozer не инициализирован
   - Решение: Удалите папку `.buildozer` и попробуйте снова

3. **"JAVA_HOME not set"** → Java не найдена
   - Решение: Убедитесь, что JDK установлена

4. **"Memory error"** → Недостаточно памяти в Colab
   - Решение: Перезапустите Colab (Runtime → Restart runtime)

## 📚 Документация компонентов

### client/client.py - Главный клиент

```python
class ClientApp:
    """Главное приложение прокси-клиента"""
    
    async def start(self):
        """Запустить клиента и SOCKS5 сервер"""
    
    async def stop(self):
        """Остановить клиента"""
```

### client/socks5.py - SOCKS5 сервер

```python
class SOCKS5Server:
    """SOCKS5 сервер для локальных подключений"""
    
    async def start(self, host='127.0.0.1', port=1080):
        """Запустить SOCKS5 сервер"""
```

### client/transport.py - WebSocket транспорт

```python
class WebSocketTransport:
    """Подключение к удаленному серверу через WebSocket"""
    
    async def connect(self, url: str):
        """Подключиться к серверу"""
```

### client/shared.py - Общие настройки

```python
SERVER_HOST = 'localhost'          # Хост сервера
SERVER_BASE_PORT = 8080            # Порт сервера
NUM_CHANNELS = 4                   # Количество каналов
SOCKS5_PORT = 1080                 # Локальный SOCKS5 порт
PRESHARED = 'password'             # Preshared secret
TLS_INSECURE = False               # Игнорировать TLS ошибки
```

## 🐛 Отладка

**Включение отладочного режима:**

Отредактируйте `main.py`:

```python
# Измените уровень логирования
logging.basicConfig(level=logging.DEBUG)

# Добавьте больше логов в нужные места
log.debug(f"Variable: {value}")
```

**Логи хранятся в:**

- Linux/Mac: `~/.olegshifter/` и `~/.local/share/olegshifter/`
- Windows: `C:\Users\YOUR_USER\.olegshifter\`
- Android: `/data/data/org.olegshifter/files/`

## 🔐 Безопасность

- ✅ ChaCha20-Poly1305 шифрование
- ✅ X25519 key exchange
- ✅ TLS поддержка
- ✅ Padding против traffic анализа
- ✅ Multi-channel для anonymity

## 🎨 Кастомизация UI

**Изменение цветов:**

В `main.py` найдите `Color(r, g, b, a)` и измените значения:

```python
# Более яркие цвета
Color(0.2, 0.7, 0.2, 1)  # Более яркий зелёный
Color(0.8, 0.2, 0.2, 1)  # Более яркий красный
```

**RGB значения:** 0.0-1.0 (0-255 в стандартной нотации, делить на 255)

## 📦 Сборка Release APK

Для распространения на Google Play требуется Release签名:

```bash
# 1. Генерируйте keystore (один раз)
keytool -genkey -v -keystore release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias release

# 2. Отредактируйте buildozer.spec
android.release_artifact = aab  # или apk

# 3. Соберите Release
buildozer -v android release
```

**⚠️ Важно:** Сохраняйте `release.jks` в безопасном месте!

## 🤝 Содействие

Нашли баг? Есть идея? Откройте Issue или Pull Request на GitHub!

## 📄 Лицензия

OlegShifter - распределённое ПО. Смотрите LICENSE файл в корне репозитория.
- `package.name` = olegshifter
- `package.domain` = org.olegshifter
- `android.permissions` = INTERNET, ACCESS_NETWORK_STATE
- `android.api` = 31
- `requirements` = python3, kivy, cryptography, websockets, protobuf

### requirements.txt
```
kivy==2.3.1
cryptography>=42.0.0
websockets>=12.0
protobuf>=4.25.0
cffi>=1.16.0
cython>=3.0.0
```

## Использование приложения

### На Android

1. **Откройте OlegShifter**
2. **Введите параметры подключения:**
   - **Host:** IP или домен сервера прокси
   - **Port:** Базовый порт WS (например, 8443)
   - **Channels:** Количество каналов (1-8)
   - **SOCKS5 Port:** Локальный порт для SOCKS5 (обычно 1080)
   - **Preshared:** Общий секрет с сервером

3. **Нажмите "Подключиться"**
   - Появится статус "Подключено" (зелёный)
   - В логе увидите информацию о подключённых каналах
   - SOCKS5 будет доступен на `127.0.0.1:1080`

4. **Используйте SOCKS5**
   - Настройте браузер или приложение на использование SOCKS5
   - Адрес: `localhost:1080` (или `127.0.0.1:1080`)

5. **Отключитесь**
   - Нажмите "Отключиться"
   - Статус изменится на "Отключено" (оранжевый)

## Установка на Android вручную

### Способ 1: Через ADB
```powershell
# Windows PowerShell
adb connect <IP_ANDROID>
adb install olegshifter-1.0.0-debug.apk
```

### Способ 2: Передача файла
1. Скопируйте APK на Android-устройство
2. Откройте встроенный файл-менеджер
3. Найдите APK
4. Нажмите на него → "Установить"
5. При необходимости разрешите неизвестные источники

### Способ 3: В Termux
```bash
pkg install wget
wget https://your-url/olegshifter-1.0.0-debug.apk
pm install olegshifter-1.0.0-debug.apk
```

## Троблшутинг

### Ошибка: "Cannot determine what C.ALooper_pollAll refers to"
→ Используйте Go 1.21 или ниже, обновите Android NDK до r21/r22

### Ошибка при подключении: "handshake failed"
→ Проверьте:
- IP/домен сервера (Host)
- Порт сервера (Port)
- Preshared Secret совпадает на сервере и клиенте

### APK не собирается
→ Проверьте:
- Java 17 JDK установлена (`java -version`)
- Buildozer > 1.4.0
- Достаточно свободного места (~15 GB)

### SOCKS5 не работает
→ Убедитесь:
- Приложение показывает "Подключено"
- Лог содержит "SOCKS5 listening on 127.0.0.1:PORT"
- Браузер/приложение настроено на правильный порт

## Разработка

### Изменение UI
Отредактируйте методы в `main.py`:
```python
def _build_config_section(self) -> BoxLayout:
    # Добавьте свои поля
    pass
```

### Добавление функционала
1. Импортируйте нужный модуль в `main.py`
2. Добавьте кнопку/поле в UI
3. Подключите обработчик события

### Сборка debug vs release
- **Debug:** `buildozer android debug`
- **Release:** `buildozer android release`

Release требует подписи (keystore).

## Лицензия
Сохраняет лицензию основного проекта OlegShifter.

## Контакты / Поддержка
Откройте Issue на GitHub.
