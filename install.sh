#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Busy — установка из исходников (macOS и Linux)
#
#  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/YFrtn/busy/main/install.sh)"
# ═══════════════════════════════════════════════════════════════
set -e

REPO="${BUSY_REPO:-https://github.com/YFrtn/busy.git}"
INSTALL_DIR="$HOME/.busy"
APP_NAME="Busy.app"

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="mac" ;;
    Linux)  PLATFORM="linux" ;;
    *)      echo "  Эта система не поддерживается: $OS"; exit 1 ;;
esac

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║        Busy — установка          ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------- 1. зависимости
echo "  [1/4] Проверяю системные зависимости..."

if [ "$PLATFORM" = "mac" ]; then
    if ! command -v brew &>/dev/null; then
        echo ""
        echo "  ⚠ Homebrew не найден. Установите его одной командой:"
        echo ""
        echo '     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        echo ""
        echo "  Потом запустите этот установщик снова."
        exit 1
    fi
    missing=""
    command -v python3 &>/dev/null || missing="$missing python3"
    command -v ffmpeg  &>/dev/null || missing="$missing ffmpeg"
    if [ -n "$missing" ]; then
        echo "  Устанавливаю:$missing"
        brew install $missing
    fi
    # tdl нужен только для вкладки Telegram — не критично, если не поставится
    command -v tdl &>/dev/null || brew install telegram-downloader || true
else
    if ! command -v python3 &>/dev/null || ! command -v ffmpeg &>/dev/null; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip ffmpeg \
                libgirepository1.0-dev gir1.2-webkit2-4.1 python3-gi python3-gi-cairo
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y python3 python3-pip ffmpeg python3-gobject webkit2gtk4.1
        else
            echo "  Установите вручную: python3, ffmpeg"
            exit 1
        fi
    fi
fi
echo "  ✓ Системные зависимости готовы"

# ---------------------------------------------------------------- 2. код
echo "  [2/4] Скачиваю Busy..."
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --quiet origin main || true
else
    rm -rf "$INSTALL_DIR"
    git clone --quiet --depth 1 "$REPO" "$INSTALL_DIR"
fi
echo "  ✓ Код в $INSTALL_DIR"

# ---------------------------------------------------------------- 3. python
echo "  [3/4] Настраиваю Python-окружение..."
cd "$INSTALL_DIR"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "  ✓ Python-пакеты установлены"

# ---------------------------------------------------------------- 4. ярлык
echo "  [4/4] Создаю ярлык..."
if [ "$PLATFORM" = "mac" ]; then
    rm -rf "/Applications/$APP_NAME"
    cp -R "$INSTALL_DIR/packaging/macos/$APP_NAME" "/Applications/$APP_NAME"
    chmod +x "/Applications/$APP_NAME/Contents/MacOS/launch"
    /System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
        -f "/Applications/$APP_NAME" 2>/dev/null || true
    echo "  ✓ Busy.app установлен в /Applications"
    echo ""
    echo "  Запуск: ⌘+Space → Busy"
    echo ""
    read -p "  Запустить сейчас? [Y/n] " -n 1 -r; echo ""
    [[ $REPLY =~ ^[Nn]$ ]] || open "/Applications/$APP_NAME"
else
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/busy.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Busy
Comment=Скачивание видео и аудио
Exec=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/busy.py
Icon=$INSTALL_DIR/assets/icon.png
Terminal=false
Categories=AudioVideo;Network;
DESKTOP
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "  ✓ Ярлык Busy добавлен в меню приложений"
    echo ""
    echo "  Запуск также возможен командой:"
    echo "     $INSTALL_DIR/venv/bin/python $INSTALL_DIR/busy.py"
    echo ""
fi
