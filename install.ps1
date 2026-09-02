# ═══════════════════════════════════════════════════════════════
#  Busy — установка из исходников (Windows 10/11)
#
#  Запустите в PowerShell:
#  irm https://raw.githubusercontent.com/YFrtn/busy/main/install.ps1 | iex
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

$Repo       = if ($env:BUSY_REPO) { $env:BUSY_REPO } else { "https://github.com/YFrtn/busy.git" }
$InstallDir = Join-Path $env:LOCALAPPDATA "Busy\src"
$BinDir     = Join-Path $env:LOCALAPPDATA "Busy\bin"

function Say($text, $color = "White") { Write-Host "  $text" -ForegroundColor $color }

Write-Host ""
Say "╔══════════════════════════════════╗" Cyan
Say "║        Busy — установка          ║" Cyan
Say "╚══════════════════════════════════╝" Cyan
Write-Host ""

# ---------------------------------------------------------------- 1. Python
Say "[1/5] Проверяю Python..."
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cmd.Source --version 2>&1
        # "Python 3.13.1" -> 3.13
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 9) { $python = $cmd.Source; break }
    }
}

if (-not $python) {
    Say "Python 3.9+ не найден — устанавливаю через winget..." Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
    if (-not $python) {
        Say "Установите Python вручную: https://www.python.org/downloads/" Red
        Say "(обязательно отметьте галочку 'Add python.exe to PATH')" Red
        exit 1
    }
}
Say "✓ Python: $python" Green

# ---------------------------------------------------------------- 2. git / код
Say "[2/5] Скачиваю Busy..."
New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null

if (Get-Command git -ErrorAction SilentlyContinue) {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        git -C $InstallDir pull --quiet origin main
    } else {
        if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
        git clone --quiet --depth 1 $Repo $InstallDir
    }
} else {
    # git может отсутствовать — тогда качаем zip-архив ветки main
    $zipUrl = $Repo.Replace(".git", "") + "/archive/refs/heads/main.zip"
    $zip    = Join-Path $env:TEMP "busy-main.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    $tmp = Join-Path $env:TEMP "busy-extract"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp
    Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $InstallDir
    Remove-Item $zip -Force
}
Say "✓ Код в $InstallDir" Green

# ---------------------------------------------------------------- 3. venv
Say "[3/5] Настраиваю Python-окружение..."
$venv = Join-Path $InstallDir "venv"
if (-not (Test-Path $venv)) { & $python -m venv $venv }
$venvPy = Join-Path $venv "Scripts\python.exe"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt")
Say "✓ Python-пакеты установлены" Green

# ---------------------------------------------------------------- 4. ffmpeg
Say "[4/5] Проверяю FFmpeg..."
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg -and -not (Test-Path (Join-Path $BinDir "ffmpeg.exe"))) {
    Say "Скачиваю FFmpeg (около 80 МБ)..." Yellow
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $ffZip = Join-Path $env:TEMP "ffmpeg.zip"
    $ffUrl = "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip"
    Invoke-WebRequest -Uri $ffUrl -OutFile $ffZip
    $ffTmp = Join-Path $env:TEMP "ffmpeg-extract"
    if (Test-Path $ffTmp) { Remove-Item -Recurse -Force $ffTmp }
    Expand-Archive -Path $ffZip -DestinationPath $ffTmp
    Get-ChildItem -Path $ffTmp -Recurse -Include ffmpeg.exe, ffprobe.exe |
        ForEach-Object { Copy-Item $_.FullName -Destination $BinDir -Force }
    Remove-Item $ffZip, $ffTmp -Recurse -Force
    Say "✓ FFmpeg установлен в $BinDir" Green
} else {
    Say "✓ FFmpeg уже есть" Green
}

# ---------------------------------------------------------------- 5. ярлыки
Say "[5/5] Создаю ярлыки..."
$pythonw  = Join-Path $venv "Scripts\pythonw.exe"   # без чёрного окна консоли
$target   = Join-Path $InstallDir "busy.py"
$icon     = Join-Path $InstallDir "assets\icon.ico"

function New-BusyShortcut($path) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath       = $pythonw
    $sc.Arguments        = "`"$target`""
    $sc.WorkingDirectory = $InstallDir
    $sc.IconLocation     = $icon
    $sc.Description      = "Busy — скачивание видео и аудио"
    $sc.Save()
}

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Busy.lnk"
New-BusyShortcut $startMenu
New-BusyShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Busy.lnk")
Say "✓ Ярлыки созданы (рабочий стол и меню «Пуск»)" Green

Write-Host ""
Say "╔══════════════════════════════════╗" Cyan
Say "║  Busy установлен!                ║" Cyan
Say "║  Запуск: ярлык Busy на рабочем   ║" Cyan
Say "║  столе или «Пуск» → Busy         ║" Cyan
Say "╚══════════════════════════════════╝" Cyan
Write-Host ""

$answer = Read-Host "  Запустить сейчас? [Y/n]"
if ($answer -notmatch "^[Nn]") { Start-Process $pythonw -ArgumentList "`"$target`"" -WorkingDirectory $InstallDir }
