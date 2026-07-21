@echo off
chcp 65001 >nul
rem Обновление VoiceInputRU.exe: закрывает старый (он запущен от администратора,
rem поэтому скрипт сам поднимает права через UAC), подменяет exe и запускает новый.
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Нужны права администратора — подтверди запрос UAC...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist "dist_new\VoiceInputRU.exe" (
    echo [x] dist_new\VoiceInputRU.exe не найден — сначала пересборка (pyinstaller).
    pause
    exit /b 1
)

echo Останавливаю VoiceInputRU...
taskkill /f /im VoiceInputRU.exe >nul 2>&1
timeout /t 2 >nul

echo Подменяю exe...
move /y "dist_new\VoiceInputRU.exe" "dist\VoiceInputRU.exe"
if errorlevel 1 (
    echo [x] Не удалось заменить exe. Закрой VoiceInputRU вручную и запусти скрипт снова.
    pause
    exit /b 1
)
rd /s /q dist_new 2>nul

echo Запускаю обновлённый VoiceInputRU...
call "dist\VoiceInputRU-launcher.bat"
echo Готово: новый exe запущен, автозагрузка использует его же.
timeout /t 3 >nul
