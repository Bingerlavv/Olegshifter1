# OlegShifter — нативный Android-клиент

Полноценный Android-клиент «из коробки». Два режима (переключатель в UI):

- **Системный VPN (весь трафик)** — по умолчанию. Приложение поднимает `VpnService`, заворачивает весь трафик телефона в TUN, а gvisor/tun2socks внутри Go гонит его через туннель. Работает для всех приложений без настройки.
- **SOCKS5-прокси** — поднимает локальный **SOCKS5 на 127.0.0.1:1080**; приложения с поддержкой прокси (браузер, Telegram) ходят через него.

Жмёшь **Подключиться** — всё поднимается само и держится в foreground-сервисе.

> ⚠️ **Про UDP:** прокси-протокол OlegShifter — TCP-only. В VPN-режиме TCP идёт через туннель, **DNS (UDP:53) автоматически резолвится DNS-over-TCP** внутри Go, а прочий UDP (QUIC и т.п.) отбрасывается — приложения откатываются на TCP. Для веба/мессенджеров этого достаточно.

Протокол — порт ядра OlegShifter на **Go** (X25519 + ChaCha20-Poly1305, multipath WS, padding, авто-реконнект). UI — нативный **Kotlin**. Такой стек надёжно собирается (в отличие от buildozer) и удобен для монетизации (AdMob/Play Billing).

> ✅ Go-ядро проверено end-to-end против Python-сервера из `../examples/`: HTTP и HTTPS проходят сквозь туннель.

## Структура

```
android_native/
├── gocore/                 # Go-ядро протокола (gomobile)
│   ├── client.go           #   публичный API: NewClient/Start/Stop
│   ├── transport.go        #   WS + X25519/HMAC handshake
│   ├── manager.go          #   multipath + ping + реконнект
│   ├── socks5.go           #   локальный SOCKS5
│   ├── tun_bridge.go       #   VPN-режим: gvisor/tun2socks (android/linux)
│   ├── crypto.go / multipath.go / padding.go
│   └── cmd/olegclient/     #   десктоп-обёртка для тестов против сервера
├── app/                    # Android-проект (Kotlin)
│   └── app/src/main/java/org/olegshifter/client/
│       ├── MainActivity.kt   # экран настроек + лог
│       ├── ProxyService.kt   # foreground-сервис (SOCKS5-режим)
│       ├── OlegVpnService.kt # VpnService (режим «весь трафик»)
│       ├── Engine.kt         # владелец Go-клиента
│       └── ProxyConfig.kt    # конфиг + SharedPreferences
├── Build_APK_Colab.ipynb   # сборка APK в Google Colab
└── BUILD.md                # подробная инструкция сборки
```

## Быстрый старт

- **Собрать APK:** открой [Build_APK_Colab.ipynb](Build_APK_Colab.ipynb) в Colab или см. [BUILD.md](BUILD.md).
- **Проверить ядро на десктопе** (без Android):
  ```bash
  cd gocore
  go run ./cmd/olegclient -host 127.0.0.1 -base 8443 -channels 3 \
      -secret "CHANGE-ME-32-bytes-shared-secret" -tls -insecure
  # затем:
  curl --socks5 127.0.0.1:1080 http://example.com
  ```

## Важно про сервер

Сервер из `examples/server.py` запускает exit node только когда получит **все** каналы (`NUM_CHANNELS + GRPC_CHANNELS`). Этот клиент — **WS-only** (gRPC опционален и обычно валит сборку). Поэтому на сервере поставь:

```python
# examples/shared.py
GRPC_CHANNELS = 0
NUM_CHANNELS  = 3   # = числу каналов в приложении
```
