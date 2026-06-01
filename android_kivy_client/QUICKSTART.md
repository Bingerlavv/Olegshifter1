# 🚀 OlegShifter Android - Быстрый Старт

Выберите способ сборки, который подходит вам лучше всего:

## 📱 Вариант 1: Google Colab (РЕКОМЕНДУЕТСЯ - проще всего)

**Для: Windows, Mac, Linux**
**Требования: Google аккаунт, интернет**
**Время: 15-20 минут**

### Шаги:

1. Откройте https://colab.research.google.com/
2. Нажмите **File** → **Open notebook**
3. Выберите **Upload** → выберите файл `Build_APK_Colab_v2.ipynb`
4. В ячейке 3 (Шаг 3) измените `GITHUB_URL` на ваш репозиторий:
   ```python
   GITHUB_URL = "https://github.com/YOUR_USERNAME/YOUR_REPO.git"
   ```
5. Нажимайте **Shift+Enter** для каждой ячейки по порядку
6. После сборки загрузите APK и установите на Android

### Примеры URL:
- `https://github.com/john/olegshifter.git`
- `https://github.com/mycompany/proxy-app.git`

---

## 💻 Вариант 2: Linux / WSL на Windows

**Для: Linux, Mac, или Windows с WSL2**
**Требования: ~10 GB свободного места, 30+ минут**
**Время: 30-45 минут**

### На Linux/Mac:

```bash
cd android_kivy_client
chmod +x build_apk.sh
./build_apk.sh
```

### На Windows (PowerShell):

```powershell
cd android_kivy_client
.\build_apk.ps1
```

**Требования для Windows:**
- WSL2 с Ubuntu
- Установите: `wsl --install`

---

## 🖥️ Вариант 3: Локальное тестирование перед сборкой

**Для: Все платформы**
**Требования: Python 3.8+, Kivy**
**Время: 5 минут**

Протестируйте приложение перед сборкой APK:

```bash
# Установите зависимости
pip install -r requirements.txt

# Запустите приложение
python run_desktop.py
```

---

## 📥 Установка на Android

### После сборки у вас будет файл: `olegshifter-1.0.0-debug.apk`

### Способ 1: ADB (USB кабель)

**Windows:**
```powershell
# Подключите телефон USB кабелем
# Включите USB Debug: Settings → Developer Options → USB Debugging

adb install olegshifter-1.0.0-debug.apk
```

**Linux/Mac:**
```bash
adb install olegshifter-1.0.0-debug.apk
```

### Способ 2: Прямая установка на телефон

1. Скачайте APK на компьютер
2. Отправьте файл на телефон (Telegram, email, облако)
3. На телефоне откройте файловый менеджер
4. Найдите APK и нажмите на него
5. Нажмите "Установить"

### Способ 3: Через облако

1. Загрузите APK в Google Drive / Dropbox / Яндекс.Диск
2. На телефоне откройте облако
3. Скачайте APK
4. Откройте и установите

---

## ⚙️ Первое использование

1. **Откройте OlegShifter** на телефоне
2. **Введите параметры:**
   - **Host**: IP/домен вашего прокси-сервера
   - **Port**: Порт (обычно 8080, 443, или 9999)
   - **Channels**: 1-10 (рекомендуется 4)
   - **SOCKS5 Port**: 1080 (оставить по умолчанию)
   - **Preshared Secret**: Ключ доступа (если требуется)
   - **Use TLS**: Да/Нет (в зависимости от сервера)

3. **Нажмите "Подключиться"**
4. **Смотрите логи** - должны появиться сообщения о подключении

---

## 🐛 Решение проблем

### Приложение не подключается
- Проверьте, запущен ли ваш прокси-сервер
- Проверьте параметры Host и Port
- Посмотрите логи в приложении

### APK не устанавливается
- Разрешите установку из неизвестных источников:
  Settings → Security → Unknown sources → ✓
- Проверьте свободное место (нужно >= 200 MB)

### Сборка в Colab не работает
- Проверьте интернет-соединение
- Используйте свежий URL GitHub репозитория
- Перезапустите Colab: Runtime → Restart runtime

### Проблемы с ADB на Windows
- Установите Android SDK Platform Tools
- Или используйте WSL для сборки

---

## 📚 Документация

- **README.md** - Полная документация
- **main.py** - Исходный код Kivy приложения
- **buildozer.spec** - Конфиг сборки APK
- **client/** - Модули прокси-клиента

---

## ❓ Вопросы?

Смотрите раздел "Решение проблем" в **README.md**

Удачи! 🎉
