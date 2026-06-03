# Olegshifter

Многоканальный зашифрованный туннель для обхода DPI/ТСПУ.

Браузер → SOCKS5 (localhost:1080) → шифрование → multipath WS+gRPC → сервер → интернет

---

## Как это работает

```
[Браузер]
    │  SOCKS5
    ▼
[Клиент]  ──── WS ch0 ────▶
           ──── WS ch1 ────▶  [Сервер] ──▶ [Интернет]
           ──── gRPC ch2 ──▶
```

- **7 слоёв защиты**: X25519 + ChaCha20-Poly1305, padding, timing randomization, multipath, HMAC-auth, path-based routing, fallback-сайт
- **Forward Secrecy**: сессионные ключи выводятся из эфемерных X25519 handshake'ов каждого канала — при компрометации старого трафика текущие сессии остаются защищены
- **Mixed transports**: WS и gRPC одновременно — DPI видит два разных протокола
- **Авто-реконнект**: упавший канал восстанавливается сам, туннель не рвётся
- **Fallback**: сервер отдаёт nginx-like 404 на неправильный путь — снаружи выглядит как обычный веб-сервер

---

## Быстрый старт

### 1. Установить зависимости

```bash
pip install websockets cryptography grpcio grpcio-tools
# для GUI-клиента:
pip install PyQt6 qasync
# для веб-дашборда сервера:
pip install fastapi uvicorn jinja2
```

### 2. Сгенерировать TLS-сертификат (один раз)

```bash
cd examples
python gen_cert.py
# Создаст server.crt и server.key
```

### 3. Поменять секреты в `examples/shared.py`

```python
PRESHARED = b"ваш-32-байтный-секрет-здесь!!!!"
```

Сгенерировать случайный:
```python
import secrets; print(secrets.token_bytes(32))
```

> Клиент и сервер должны иметь **одинаковый** `PRESHARED`.

### 4. Запустить сервер

```bash
cd examples
python server.py
```

Сервер поднимет 3 WS-порта (8443–8445) и 1 gRPC-порт (50051).

### 5. Запустить клиент

```bash
cd examples
python client.py
```

### 6. Настроить браузер

`SOCKS5 → 127.0.0.1:1080`

В Firefox: Настройки → Дополнительно → Сеть → Настройка соединения → Ручная настройка прокси → SOCKS5, хост `127.0.0.1`, порт `1080`.

Проверить:
```bash
curl --socks5 127.0.0.1:1080 https://2ip.ru
```

---

## GUI-приложения

### Клиент (PyQt6 с иконкой в трее)

```bash
python apps/client_gui.py
```

- Окно настроек: хост сервера, секрет, TLS, SOCKS5-порт
- Цветные индикаторы каналов (зелёный = жив)
- Иконка в трее, сворачивается при закрытии окна
- Конфиг: `~/.olegshifter/client.json`

### Сервер (веб-дашборд)

```bash
python apps/server_web.py
```

Открой ссылку из консоли (`http://127.0.0.1:7070/login?token=...`):

- Старт/стоп одной кнопкой
- Таблица подключённых клиентов
- Редактор настроек
- Живой лог
- Конфиг: `~/.olegshifter/server.json`

---

## Конфигурация

### Ключевые параметры (`examples/shared.py`)

| Параметр | Описание |
|---|---|
| `PRESHARED` | Общий секрет клиента и сервера (32 байта) |
| `NUM_CHANNELS` | Количество WS-каналов (по умолчанию 3) |
| `GRPC_CHANNELS` | Количество gRPC-каналов (по умолчанию 1) |
| `SERVER_HOST` | IP/домен сервера |
| `SERVER_BASE_PORT` | Первый WS-порт (дальше +1 на канал) |
| `GRPC_BASE_PORT` | Первый gRPC-порт |

### TLS в продакшне (Let's Encrypt)

Когда у сервера есть нормальный домен с Let's Encrypt:

```python
# shared.py — сервер
CERT_PATH = "/etc/letsencrypt/live/example.com/fullchain.pem"
KEY_PATH  = "/etc/letsencrypt/live/example.com/privkey.pem"
```

Клиент: TLS включить, CA path оставить пустым (доверяем системным CA), SNI = домен сервера.

### Fallback-сайт

Чтобы сервер при HTTP-запросе на чужой путь проксировал на реальный сайт:

```python
# WSConfig на сервере
WSConfig(
    ...,
    fallback_upstream = ("example.com", 80),
)
```

---

## Тестирование

### Быстрая проверка (всё локально)

```bash
# Терминал 1 — сервер
cd examples && python server.py

# Терминал 2 — клиент
cd examples && python client.py

# Терминал 3 — проверка
curl --socks5 127.0.0.1:1080 http://example.com
curl --socks5 127.0.0.1:1080 https://2ip.ru
```

### Юнит-тесты ядра

```bash
cd core
pip install pytest pytest-asyncio
python -m pytest -v
# Должно быть: 95 passed, 1 skipped
```

### Проверка fallback (сервер выглядит как nginx)

```bash
# Пока сервер запущен:
curl -v http://127.0.0.1:8443/index.html
# Ожидается: HTTP 404, тело с "nginx"
```

### Проверка авто-реконнекта

Запусти клиент, убей один процесс сервера на порту (или временно закрой файрвол на этом порту) — в логе клиента должно появиться "канал N упал → переподключаю..." и через несколько секунд "канал N переподключён".

---

## Сборка .exe

```bash
pip install pyinstaller
python build_exe.py
```

Готовые файлы появятся в `dist/`:
- `dist/olegshifter-client.exe` — клиент
- `dist/olegshifter-server.exe` — сервер (без GUI, веб-дашборд)

Подробнее — см. `build_exe.py`.

---

## Структура проекта

```
core/
  crypto_core.py      — X25519 + ChaCha20-Poly1305 + PFS-ротация ключей
  transport.py        — WebSocket транспорт (TLS, path-routing, fallback)
  grpc_transport.py   — gRPC транспорт
  channel_manager.py  — оркестратор: крипто + multipath + padding
  multipath.py        — разбивка трафика по каналам (STRIPED/REDUNDANT/ADAPTIVE)
  padding.py          — трафик-паддинг + chaff + timing randomization
  socks5.py           — SOCKS5 прокси (клиент) и ExitNode (сервер)
  tls_utils.py        — TLS helpers, генерация self-signed сертификата
  proto/
    tunnel.proto      — gRPC протокол

examples/
  shared.py           — конфигурация
  client.py           — CLI-клиент
  server.py           — CLI-сервер
  gen_cert.py         — генерация dev-сертификата

apps/
  client_gui.py       — PyQt6 GUI-клиент с треем
  server_web.py       — FastAPI веб-дашборд сервера
  app_config.py       — загрузка/сохранение JSON-конфигов
```

---

## Безопасность

- **Не деплой с дефолтным `PRESHARED`** — сразу меняй на случайный секрет
- **Не используй `tls_insecure=True`** в продакшне
- Порты WS/gRPC лучше прятать за nginx с `ssl_preread` — тогда снаружи виден только 443
- `preshared` не передаётся по сети — он только как соль для HKDF

---

## Зависимости

| Пакет | Зачем |
|---|---|
| `websockets` | WS-транспорт |
| `cryptography` | X25519, ChaCha20-Poly1305, HKDF, TLS-утилиты |
| `grpcio` | gRPC-транспорт |
| `grpcio-tools` | Компиляция .proto (нужна один раз) |
| `PyQt6` | GUI-клиент |
| `qasync` | asyncio + Qt event loop |
| `fastapi` + `uvicorn` | Веб-дашборд сервера |
