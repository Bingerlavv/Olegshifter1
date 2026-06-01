[app]

# Основная информация
title = OlegShifter
package.name = olegshifter
package.domain = org.olegshifter
source.dir = .
source.include_exts = py,png,jpg,kv,atlas

# Версионирование
version = 1.0.0
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/main.py

# Ориентация и полноэкранность
orientation = portrait
fullscreen = 0

# Иконка и метаданные
icon.filename = %(source.dir)s/olegshifter.png
presplash.filename = %(source.dir)s/olegshifter.png
presplash.scale = 2

# Android конфигурация
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,CHANGE_NETWORK_STATE,MODIFY_AUDIO_SETTINGS
android.api = 31
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True

# Опции сборки
android.archs = arm64-v8a,armeabi-v7a
android.features = 
android.gradle_dependencies = 

# gRPC и Protocol Buffers для Android
requirements = python3,kivy,cryptography,websockets,protobuf,pyjnius

# Java класс главного приложения
android.entrypoint = org.kivy.android.PythonActivity

# Разрешения для сохранения конфигурации
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE

# Метаданные
meta-data = android.notch_support_flags=default

[buildozer]
log_level = 2
warn_on_root = 1
requirements = python3,kivy,cryptography,websockets,protobuf,pyjnius
