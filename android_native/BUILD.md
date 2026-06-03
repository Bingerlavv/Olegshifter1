# Сборка OlegShifter Android (нативный Kotlin + Go-ядро)

Архитектура:

```
app/  (Kotlin, Android Studio)        gocore/  (Go, протокол OlegShifter)
  MainActivity ─ UI                     client.go   ─ публичный API (gomobile)
  ProxyService ─ foreground service     transport   ─ WS + X25519/HMAC handshake
  Engine       ─ владеет Go-клиентом    manager     ─ multipath + ping + реконнект
       │                                socks5      ─ локальный SOCKS5
       │  вызывает                      crypto/padding/multipath
       ▼
  app/libs/olegcore.aar  ◄── gomobile bind из gocore/
```

Поток: `браузер/приложение → SOCKS5 127.0.0.1:1080 → Go-ядро → шифрованный multipath WS → сервер → интернет`.

Go-ядро уже проверено end-to-end против Python-сервера из `examples/` (HTTP и HTTPS через туннель проходят).

---

## Что нужно один раз

| Инструмент | Версия | Зачем |
|---|---|---|
| Go | **1.23+** | компиляция ядра (gvisor/tun2socks требуют 1.23) |
| gomobile | latest | Go → Android `.aar` |
| Android SDK | platform 34 | сборка APK |
| Android NDK | r25c+ | компиляция Go под Android |
| JDK | 17 | Gradle |

---

## Вариант A. Google Colab (рекомендуется — надёжнее buildozer)

Открой `Build_APK_Colab.ipynb` (в этой папке) в https://colab.research.google.com и выполни ячейки по порядку. Ноутбук сам ставит Go, NDK, SDK, gomobile, собирает `.aar` и APK. ~8–12 минут.

Если хочешь руками — те же шаги ниже.

---

## Вариант B. Локально (Linux / WSL / Mac)

### 1. Поставить gomobile

```bash
go install golang.org/x/mobile/cmd/gomobile@latest
go install golang.org/x/mobile/cmd/gobind@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

### 2. Указать Android SDK/NDK

```bash
export ANDROID_HOME=$HOME/Android/Sdk          # путь к SDK
export ANDROID_NDK_HOME=$ANDROID_HOME/ndk/25.2.9519653
gomobile init
```

### 3. Собрать Go-ядро в .aar

```bash
cd gocore
go mod tidy
gomobile bind -target=android -androidapi 21 \
    -o ../app/app/libs/olegcore.aar .
```

Появится `app/app/libs/olegcore.aar` (+ `olegcore-sources.jar`).

### 4. Собрать APK

```bash
cd ../app
gradle wrapper --gradle-version 8.7   # один раз, создаёт ./gradlew
./gradlew assembleDebug
```

Или просто `gradle assembleDebug`, если Gradle стоит в системе.

APK: `app/build/outputs/apk/debug/app-debug.apk`.

### 5. Установить

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

---

## Вариант C. Android Studio (Windows/Mac/Linux)

1. Сначала собери `.aar` (шаги B1–B3 — в WSL на Windows).
2. **File → Open** → выбери папку `android_native/app`.
3. Studio сам поставит нужный Gradle/SDK. NDK не нужен — `.aar` уже готов.
4. **Run** (зелёный треугольник) или **Build → Build APK(s)**.

---

## Сгенерированный Go API (для справки)

После `gomobile bind` Kotlin видит пакет `olegcore`:

```kotlin
val c = olegcore.Olegcore.newClient()
c.start(host, basePort, channels, socksHost, socksPort,
        preshared, useTls, tlsInsecure, sni, logger)  // бросает Exception при ошибке
c.startTun(fd, mtu)   // VPN-режим: fd от VpnService.establish(); бросает Exception
c.stopTun()           // выключить VPN (туннель остаётся)
c.stop()
c.isRunning
```

`Logger` — интерфейс с методом `log(String)`; реализован в `Engine.kt`.
Числа в Go это `int` → в Java `long`, поэтому из Kotlin передаём `.toLong()`.

---

## Монетизация

1. **AdMob (баннеры/межстраничные):**
   - Раскомментируй `play-services-ads` в `app/app/build.gradle`.
   - Вставь свой `APPLICATION_ID` в `AndroidManifest.xml` (есть закомментированный блок).
   - Добавь баннер в `activity_main.xml` / межстраничную при подключении.
2. **Подписки/покупки (убрать рекламу, премиум-серверы):**
   - Раскомментируй `billing-ktx`, подключи Google Play Billing.
3. **Release-подпись для Play Store:**
   ```bash
   keytool -genkey -v -keystore release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias oleg
   ```
   Добавь `signingConfigs` в `app/app/build.gradle` и собери `./gradlew bundleRelease` (AAB для Play).

---

## Частые ошибки

| Симптом | Причина / решение |
|---|---|
| `gomobile: command not found` | `export PATH="$PATH:$(go env GOPATH)/bin"` |
| `no Android NDK found` | поставь NDK и `export ANDROID_NDK_HOME=...`, затем `gomobile init` |
| `olegcore.aar (No such file)` | сначала шаг B3 (bind), потом Gradle |
| APK ставится, но не коннектит | проверь host/port/preshared (совпадает с сервером), TLS-флаг |
| `handshake failed` в логе | неверный preshared или сервер слушает другой порт |
| Сервер не отдаёт трафик | сервер ждёт ВСЕ каналы: на сервере поставь `GRPC_CHANNELS=0`, а `NUM_CHANNELS` = числу каналов в приложении |
