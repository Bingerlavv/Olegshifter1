#!/usr/bin/env python3
"""
OlegShifter Desktop Runner - для тестирования Kivy GUI локально
Позволяет тестировать приложение перед сборкой APK
"""

import sys
import os

# Добавляем текущую папку в path
sys.path.insert(0, os.path.dirname(__file__))

# Импортируем главное приложение
from main import ProxyClientApp

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 OlegShifter Desktop Client (Kivy)")
    print("=" * 60)
    print()
    print("Это локальный запуск приложения для тестирования")
    print("перед сборкой APK для Android.")
    print()
    print("💡 Подсказки:")
    print("   • Изменяйте параметры в форме Конфигурация")
    print("   • Нажмите 'Подключиться' для запуска прокси")
    print("   • Смотрите логи в разделе 'Логи'")
    print("   • Нажмите 'Отключиться' для остановки")
    print()
    print("=" * 60)
    print()

    app = ProxyClientApp()
    app.run()
