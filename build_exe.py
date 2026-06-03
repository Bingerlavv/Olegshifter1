"""
Сборка standalone .exe через PyInstaller.

Запуск:
    pip install pyinstaller
    python build_exe.py

Результат:
    dist/olegshifter-client.exe   — GUI-клиент (PyQt6 + трей)
    dist/olegshifter-server.exe   — сервер (FastAPI веб-дашборд)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

ROOT  = Path(__file__).parent
CORE  = ROOT / "core"
APPS  = ROOT / "apps"
DIST  = ROOT / "dist"


def run(args: list) -> None:
    print("▶", " ".join(str(a) for a in args))
    result = subprocess.run(args, check=True)


def build(
    script: Path,
    name: str,
    *,
    windowed: bool = False,
    extra_data: list = None,
    extra_imports: list = None,
) -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--clean",
        f"--name={name}",
        f"--distpath={DIST}",
        f"--workpath={ROOT / 'build' / name}",
        f"--specpath={ROOT / 'build' / name}",
        # core/ добавляем в путь поиска модулей
        f"--paths={CORE}",
        f"--paths={APPS}",
    ]

    if windowed:
        cmd.append("--windowed")   # без консоли (для GUI)

    # Если установлены оба PyQt — исключаем лишний, иначе PyInstaller падает
    cmd += [
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide2",
        "--exclude-module", "PySide6",
    ]

    # proto-stub'ы нужны как данные
    cmd += [
        "--add-data", f"{CORE / 'proto'}{os.pathsep}proto",
    ]

    # grpc нужен явный импорт (динамическая загрузка плагинов)
    for mod in (extra_imports or []):
        cmd += ["--hidden-import", mod]

    cmd.append(str(script))
    run(cmd)
    print(f"✓ {DIST / (name + '.exe')}\n")


def main() -> None:
    DIST.mkdir(exist_ok=True)

    # --- Клиент ---
    print("=" * 50)
    print("Собираю клиент...")
    print("=" * 50)
    build(
        APPS / "client_gui.py",
        name      = "olegshifter-client",
        windowed  = True,   # без чёрного окна консоли
        extra_imports = [
            "PyQt6.QtCore",
            "PyQt6.QtGui",
            "PyQt6.QtWidgets",
            "qasync",
            "grpc",
            "grpc._cython.cygrpc",
            "proto.tunnel_pb2",
            "proto.tunnel_pb2_grpc",
        ],
    )

    # --- Сервер ---
    print("=" * 50)
    print("Собираю сервер...")
    print("=" * 50)
    build(
        APPS / "server_web.py",
        name      = "olegshifter-server",
        windowed  = False,  # консоль нужна для логов
        extra_imports = [
            "uvicorn.lifespan.on",
            "uvicorn.lifespan.off",
            "uvicorn.protocols.http.h11_impl",
            "uvicorn.protocols.http.httptools_impl",
            "uvicorn.protocols.websockets.websockets_impl",
            "uvicorn.protocols.websockets.wsproto_impl",
            "uvicorn.logging",
            "grpc",
            "grpc._cython.cygrpc",
            "proto.tunnel_pb2",
            "proto.tunnel_pb2_grpc",
        ],
    )

    print("=" * 50)
    print("Готово!")
    print(f"  {DIST / 'olegshifter-client.exe'}")
    print(f"  {DIST / 'olegshifter-server.exe'}")
    print("=" * 50)


if __name__ == "__main__":
    # Проверяем PyInstaller
    try:
        import PyInstaller
    except ImportError:
        print("Не найден PyInstaller. Установи: pip install pyinstaller")
        sys.exit(1)

    main()
