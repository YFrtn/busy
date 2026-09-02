@echo off
REM Busy — запуск из папки с исходниками (Windows).
REM Создаёт venv при первом запуске, дальше просто открывает приложение.

cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo Первый запуск: настраиваю окружение...
    python -m venv venv || (echo Не найден Python 3. Установите его с python.org & pause & exit /b 1)
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

start "" "venv\Scripts\pythonw.exe" busy.py
