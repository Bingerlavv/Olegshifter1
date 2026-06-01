#!/usr/bin/env python3
"""
OlegShifter Project Checker
Проверка всех необходимых компонентов перед сборкой
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Цвета для консоли
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

def print_header(text):
    print(f"\n{BLUE}{BOLD}{'=' * 60}{RESET}")
    print(f"{BLUE}{BOLD}{text}{RESET}")
    print(f"{BLUE}{BOLD}{'=' * 60}{RESET}\n")

def check_success(item):
    print(f"{GREEN}✓{RESET} {item}")

def check_fail(item, error=""):
    print(f"{RED}✗{RESET} {item}")
    if error:
        print(f"  {YELLOW}→ {error}{RESET}")

def check_warning(item, msg=""):
    print(f"{YELLOW}⚠{RESET} {item}")
    if msg:
        print(f"  {YELLOW}→ {msg}{RESET}")

def run_command(cmd, shell=False):
    """Запустить команду и вернуть результат"""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "Timeout"
    except Exception as e:
        return False, "", str(e)

def check_python():
    """Проверить Python"""
    print_header("Python")
    
    success, out, err = run_command([sys.executable, "--version"])
    if success or out:
        version = out.split()[1] if out else "unknown"
        check_success(f"Python: {version}")
        
        # Проверить версию
        major, minor = map(int, version.split('.')[:2])
        if major < 3 or (major == 3 and minor < 8):
            check_warning("Требуется Python 3.8+", f"Текущая версия: {version}")
            return False
        return True
    else:
        check_fail("Python не найден")
        return False

def check_kivy():
    """Проверить Kivy"""
    print_header("Kivy Framework")
    
    try:
        import kivy
        check_success(f"Kivy: {kivy.__version__}")
        return True
    except ImportError:
        check_fail("Kivy не установлен")
        print(f"  {YELLOW}→ Установите: pip install kivy{RESET}")
        return False

def check_dependencies():
    """Проверить зависимости Python"""
    print_header("Python Dependencies")
    
    required = {
        'cryptography': 'Шифрование',
        'websockets': 'WebSocket клиент',
        'protobuf': 'Protocol Buffers',
        'kivy': 'GUI фреймворк',
        'pyjnius': 'Java интеграция для Android',
    }
    
    all_ok = True
    for pkg, desc in required.items():
        try:
            mod = __import__(pkg)
            version = getattr(mod, '__version__', 'unknown')
            check_success(f"{pkg}: {version} ({desc})")
        except ImportError:
            check_fail(f"{pkg} не установлен ({desc})")
            all_ok = False
    
    return all_ok

def check_files():
    """Проверить наличие необходимых файлов"""
    print_header("Project Files")
    
    current_dir = Path(__file__).parent
    
    required_files = {
        'main.py': 'Главное приложение Kivy',
        'buildozer.spec': 'Конфиг сборки APK',
        'requirements.txt': 'Python зависимости',
        'Build_APK_Colab_v2.ipynb': 'Colab ноутбук (v2)',
        'README.md': 'Документация',
        'client/client.py': 'Прокси-клиент',
        'client/socks5.py': 'SOCKS5 сервер',
        'client/shared.py': 'Общие конфиги',
    }
    
    all_ok = True
    for file, desc in required_files.items():
        path = current_dir / file
        if path.exists():
            size = path.stat().st_size / 1024  # KB
            check_success(f"{file} ({size:.1f} KB) - {desc}")
        else:
            check_fail(f"{file} не найден - {desc}")
            all_ok = False
    
    return all_ok

def check_client_modules():
    """Проверить модули клиента"""
    print_header("Client Modules")
    
    current_dir = Path(__file__).parent
    client_dir = current_dir / 'client'
    
    modules = [
        'client.py',
        'shared.py',
        'transport.py',
        'socks5.py',
        'channel_manager.py',
        'crypto_core.py',
        'multipath.py',
        'padding.py',
        'tls_utils.py',
        'grpc_transport.py',
    ]
    
    all_ok = True
    for module in modules:
        path = client_dir / module
        if path.exists():
            check_success(f"{module}")
        else:
            check_fail(f"{module} не найден")
            all_ok = False
    
    return all_ok

def check_buildozer_config():
    """Проверить buildozer.spec"""
    print_header("Buildozer Configuration")
    
    current_dir = Path(__file__).parent
    spec_file = current_dir / 'buildozer.spec'
    
    if not spec_file.exists():
        check_fail("buildozer.spec не найден")
        return False
    
    with open(spec_file, 'r') as f:
        content = f.read()
    
    checks = {
        'package.name = olegshifter': 'Имя пакета',
        'android.permissions = INTERNET': 'Internet разрешение',
        'android.api = 31': 'Android API уровень',
        'android.ndk = 25b': 'NDK версия',
        'requirements =': 'Зависимости',
    }
    
    all_ok = True
    for check, desc in checks.items():
        if check in content:
            check_success(desc)
        else:
            check_warning(f"{desc} - не совсем правильно конфигурирован")
    
    return True

def check_colab_notebook():
    """Проверить Colab ноутбук"""
    print_header("Google Colab Setup")
    
    current_dir = Path(__file__).parent
    notebook_v2 = current_dir / 'Build_APK_Colab_v2.ipynb'
    
    if notebook_v2.exists():
        check_success(f"Build_APK_Colab_v2.ipynb (улучшенная версия)")
    else:
        check_warning("Build_APK_Colab_v2.ipynb не найден")
    
    notebook_v1 = current_dir / 'Build_APK_Colab.ipynb'
    if notebook_v1.exists():
        check_success(f"Build_APK_Colab.ipynb (оригинальная версия)")
    else:
        check_warning("Build_APK_Colab.ipynb не найден")
    
    return notebook_v2.exists() or notebook_v1.exists()

def check_platform_tools():
    """Проверить платформ-специфичные инструменты"""
    print_header("Platform Tools")
    
    # Проверить git
    success, out, _ = run_command(['git', '--version'])
    if success or out:
        version = out.split()[-1] if out else 'unknown'
        check_success(f"Git: {version}")
    else:
        check_warning("Git не найден (нужен для Colab)")
    
    # Проверить ADB (опционально)
    success, out, _ = run_command(['adb', 'version'], shell=True)
    if success or out:
        check_success("ADB: установлен (для установки на Android)")
    else:
        check_warning("ADB не найден", "Нужен для установки APK через USB")

def generate_report():
    """Генерировать итоговый отчет"""
    print_header("📋 Итоговый Отчет")
    
    print(f"Проверка выполнена: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Платформа: {sys.platform}")
    print()
    
    print("Статус компонентов:")
    print(f"  {GREEN}✓ - Компонент найден и готов{RESET}")
    print(f"  {YELLOW}⚠ - Компонент найден, но есть замечания{RESET}")
    print(f"  {RED}✗ - Компонент не найден{RESET}")
    print()
    
    print("Дальше:")
    print(f"  1. Тестирование локально: {BLUE}python run_desktop.py{RESET}")
    print(f"  2. Сборка на Colab: Загрузите {BLUE}Build_APK_Colab_v2.ipynb{RESET}")
    print(f"  3. Локальная сборка (Linux/WSL): {BLUE}./build_apk.sh{RESET}")
    print(f"  4. Локальная сборка (Windows): {BLUE}.\\build_apk.ps1{RESET}")
    print()

def main():
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}{BLUE}OlegShifter Project Checker{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print()
    
    # Проверки
    results = {}
    
    results['Python'] = check_python()
    results['Kivy'] = check_kivy()
    results['Dependencies'] = check_dependencies()
    results['Files'] = check_files()
    results['Client Modules'] = check_client_modules()
    results['Buildozer Config'] = check_buildozer_config()
    results['Colab Notebook'] = check_colab_notebook()
    check_platform_tools()
    
    generate_report()
    
    # Итоговый статус
    if all(results.values()):
        print(f"\n{GREEN}{BOLD}✓ Все компоненты готовы к сборке!{RESET}\n")
        return 0
    else:
        print(f"\n{YELLOW}{BOLD}⚠ Есть проблемы, но можно попробовать собрать{RESET}\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
