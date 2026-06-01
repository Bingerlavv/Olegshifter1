#!/bin/bash
# OlegShifter Android APK Builder - Bash версия
# Для Linux и WSL на Windows
# Использование: ./build_apk.sh

set -e

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Функции для вывода
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

# Заголовок
echo ""
echo "╔════════════════════════════════════════╗"
echo "║  OlegShifter Android APK Builder       ║"
echo "║  Linux/WSL Edition                     ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Проверка требований
print_info "Проверка требований..."

# Проверка Python
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 не найден"
    echo "Установите: sudo apt install python3-dev python3-pip"
    exit 1
fi
print_success "Python 3 найден"

# Проверка Java
if ! command -v java &> /dev/null; then
    print_error "Java не найдена"
    echo "Установите: sudo apt install openjdk-17-jdk"
    exit 1
fi
JAVA_VERSION=$(java -version 2>&1 | grep version | cut -d'"' -f2)
print_success "Java найдена (версия: $JAVA_VERSION)"

# Проверка Git
if ! command -v git &> /dev/null; then
    print_error "Git не найден"
    echo "Установите: sudo apt install git"
    exit 1
fi
print_success "Git найден"

# Проверка текущей папки
if [ ! -f "main.py" ]; then
    print_error "Файл main.py не найден в текущей папке"
    echo "Запустите скрипт из папки android_kivy_client"
    exit 1
fi
print_success "Проект найден"

echo ""

# Установка зависимостей Python
print_info "Установка зависимостей Python..."
pip install -q --upgrade pip setuptools wheel
pip install -q buildozer cython kivy

print_success "Python зависимости установлены"

echo ""

# Установка системных зависимостей (если нужны)
if [ ! -d ".buildozer" ]; then
    print_info "Это выглядит как первый запуск."
    print_info "Убедитесь, что установлены системные зависимости:"
    echo "  sudo apt install -y build-essential"
    echo ""
    read -p "Продолжить? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""

# Сборка APK
print_info "Начинаю сборку APK..."
print_warn "Это может занять 10-20 минут..."
echo ""

START_TIME=$(date +%s)

buildozer -v android debug

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo ""

# Проверка результата
if [ -f "bin/olegshifter-1.0.0-debug.apk" ]; then
    print_success "APK успешно собран!"
    APK_SIZE=$(du -h bin/olegshifter-1.0.0-debug.apk | cut -f1)
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📦 Результат:"
    echo "   Файл: bin/olegshifter-1.0.0-debug.apk"
    echo "   Размер: $APK_SIZE"
    echo "   Время: ${MINUTES}m ${SECONDS}s"
    echo ""
    echo "🚀 Установка на Android:"
    echo "   adb install bin/olegshifter-1.0.0-debug.apk"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    print_error "APK не был создан"
    print_error "Проверьте логи выше"
    exit 1
fi

echo ""
print_success "Готово!"
