# OlegShifter Android APK Builder для Windows (PowerShell)
# Использование: .\build_apk.ps1

Write-Host "╔════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║  OlegShifter Android APK Builder       ║" -ForegroundColor Green
Write-Host "║  Windows PowerShell Edition            ║" -ForegroundColor Green
Write-Host "╚════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

# Проверка WSL
Write-Host "[INFO] Проверка WSL..." -ForegroundColor Green

$wslCheck = wsl --list 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] WSL не установлена!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Установите WSL2 с Ubuntu:"
    Write-Host "  1. Откройте PowerShell (администратор)"
    Write-Host "  2. Выполните: wsl --install"
    Write-Host "  3. Перезагрузитесь"
    Write-Host "  4. Откройте Ubuntu и завершите установку"
    Write-Host ""
    exit 1
}

Write-Host "[✓] WSL найдена" -ForegroundColor Green
Write-Host ""

# Получение пути к проекту
$projectPath = Get-Location
$androidPath = Join-Path $projectPath "android_kivy_client"

if (!(Test-Path (Join-Path $androidPath "main.py"))) {
    Write-Host "[ERROR] main.py не найден в текущей папке!" -ForegroundColor Red
    Write-Host "Запустите скрипт из папки android_kivy_client"
    exit 1
}

Write-Host "[✓] Проект найден: $androidPath" -ForegroundColor Green
Write-Host ""

# Конвертация пути для WSL
$wslPath = ($androidPath -replace '\\', '/') -replace ':', ';'
$wslPath = "/mnt/c/$wslPath"

Write-Host "[INFO] Запуск сборки в WSL..." -ForegroundColor Green
Write-Host ""

# Запуск сборки в WSL
wsl bash -c "cd '$wslPath' && bash build_apk.sh"

Write-Host ""

# Проверка результата
$apkPath = Join-Path $androidPath "bin" "olegshifter-1.0.0-debug.apk"

if (Test-Path $apkPath) {
    $apkSize = (Get-Item $apkPath).length / 1MB
    
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
    Write-Host "✓✓✓ APK УСПЕШНО СОБРАН ✓✓✓" -ForegroundColor Green
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
    Write-Host ""
    Write-Host "📦 Результат:" -ForegroundColor Green
    Write-Host "   Файл: $apkPath" -ForegroundColor White
    Write-Host "   Размер: $([Math]::Round($apkSize, 1)) MB" -ForegroundColor White
    Write-Host ""
    Write-Host "🚀 Установка на Android:" -ForegroundColor Green
    Write-Host "   adb install `"$apkPath`"" -ForegroundColor White
    Write-Host ""
    Write-Host "📋 Альтернативно:" -ForegroundColor Green
    Write-Host "   1. Откройте: $apkPath" -ForegroundColor White
    Write-Host "   2. Скопируйте на Android устройство" -ForegroundColor White
    Write-Host "   3. Откройте файл и установите" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "[ERROR] APK не был создан!" -ForegroundColor Red
    Write-Host "Проверьте логи выше для ошибок" -ForegroundColor Red
    exit 1
}

Write-Host "[✓] Готово!" -ForegroundColor Green
