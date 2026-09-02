#!/bin/bash
# Busy — запуск из папки с исходниками (macOS / Linux).
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "  Первый запуск: настраиваю окружение..."
    python3 -m venv venv
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
fi

exec ./venv/bin/python busy.py "$@"
