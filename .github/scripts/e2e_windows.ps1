# Сквозная проверка собранного Busy.exe на Windows.
#
# Запускает приложение так же, как его запустит пользователь (окно без
# консоли), и через его же HTTP-API проверяет то, ради чего его скачивают:
# зависимости, скачивание файла, конвертацию в MP3, историю и нарезку.

$ErrorActionPreference = "Stop"

$exe      = "./dist/Busy/Busy.exe"
$dataDir  = Join-Path $env:LOCALAPPDATA "Busy"
$portFile = Join-Path $dataDir "port"
$binDir   = Join-Path $dataDir "bin"
$failed   = @()

function Step($name) { Write-Host "`n=== $name ===" -ForegroundColor Cyan }
function Pass($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Fail($msg)   { Write-Host "  FAIL: $msg" -ForegroundColor Red; $script:failed += $msg }

# Приложение должно быть чистым: как у пользователя, который поставил его впервые
Remove-Item $portFile -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dataDir "history.db") -ErrorAction SilentlyContinue

$env:BUSY_NO_GUI = "1"                       # без окна: проверяем сервер
$env:BUSY_DOWNLOAD_DIR = Join-Path $PWD "e2e-downloads"
New-Item -ItemType Directory -Force -Path $env:BUSY_DOWNLOAD_DIR | Out-Null

Step "Запуск приложения"
$proc = Start-Process $exe -PassThru -RedirectStandardOutput e2e-out.txt -RedirectStandardError e2e-err.txt

try {
    $base = $null
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        if (-not (Test-Path $portFile)) { continue }
        $port = (Get-Content $portFile -Raw).Trim()
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $base = "http://127.0.0.1:$port"; break }
        } catch { }
    }
    if (-not $base) {
        Write-Host "--- stdout ---"; Get-Content e2e-out.txt -ErrorAction SilentlyContinue
        Write-Host "--- stderr ---"; Get-Content e2e-err.txt -ErrorAction SilentlyContinue
        throw "Приложение не подняло интерфейс"
    }
    Pass "интерфейс отвечает: $base"

    # ---------------------------------------------------------------- система
    Step "Определение системы"
    $cfg = Invoke-RestMethod "$base/api/config"
    if ($cfg.platform.os -ne "windows") { Fail "ОС определена как '$($cfg.platform.os)'" }
    else { Pass "ОС: windows, файловый менеджер: $($cfg.platform.file_manager)" }
    Pass "папка загрузок: $($cfg.download_dir)"

    # ---------------------------------------------------------------- зависимости
    Step "Проверка зависимостей"
    $deps = Invoke-RestMethod "$base/api/deps/check"
    foreach ($d in $deps.deps) {
        Write-Host ("  {0,-10} установлено={1} команда='{2}'" -f $d.id, $d.installed, $d.hint)
    }
    $ytdlp = $deps.deps | Where-Object { $_.id -eq "yt-dlp" }
    if (-not $ytdlp.installed) { Fail "yt-dlp не работает внутри сборки" }
    else { Pass "yt-dlp внутри exe работает, версия $($ytdlp.version)" }

    # ---------------------------------------------------------------- ffmpeg
    Step "Автоустановка FFmpeg (Busy качает его сам)"
    Remove-Item (Join-Path $binDir "ffmpeg.exe") -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $binDir "ffprobe.exe") -ErrorAction SilentlyContinue
    $job = Invoke-RestMethod "$base/api/deps/install" -Method Post -ContentType "application/json" `
        -Body (@{ pkg = "ffmpeg" } | ConvertTo-Json)
    $status = $null
    foreach ($i in 1..90) {
        Start-Sleep -Seconds 2
        $status = Invoke-RestMethod "$base/api/deps/install/status/$($job.job_id)"
        Write-Host "  $($status.status): $($status.output)"
        if ($status.status -ne "installing") { break }
    }
    if ($status.status -ne "done") { Fail "установка FFmpeg: $($status.error)" }
    elseif (-not (Test-Path (Join-Path $binDir "ffmpeg.exe"))) { Fail "ffmpeg.exe не появился в $binDir" }
    else { Pass "FFmpeg скачан в $binDir без прав администратора" }

    # ---------------------------------------------------------------- скачивание
    Step "Скачивание файла и конвертация в MP3"
    $srcUrl = "https://raw.githubusercontent.com/$env:GITHUB_REPOSITORY/$env:GITHUB_SHA/.github/testdata/tone.mp3"
    $job = Invoke-RestMethod "$base/api/download" -Method Post -ContentType "application/json" -Body (@{
        url = $srcUrl; format = "audio"; audio_format = "mp3"; audio_quality = "128k"; title = "e2e proverka"
    } | ConvertTo-Json)
    $status = $null
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 2
        $status = Invoke-RestMethod "$base/api/status/$($job.job_id)"
        if ($status.status -ne "downloading") { break }
    }
    if ($status.status -ne "done") {
        Fail "скачивание: $($status.error)"
    } else {
        $file = Join-Path $env:BUSY_DOWNLOAD_DIR $status.filename
        if (-not (Test-Path $file)) { Fail "файл не найден: $file" }
        else { Pass "скачано и сконвертировано: $($status.filename), $([math]::Round($status.file_size/1KB)) КБ" }
    }

    # ---------------------------------------------------------------- история
    Step "История"
    $history = Invoke-RestMethod "$base/api/history"
    if ($history.Count -lt 1) { Fail "запись не попала в историю" }
    else { Pass "в истории $($history.Count) запись(и): $($history[0].filename)" }

    # ---------------------------------------------------------------- нарезка
    Step "Нарезка аудио (ffmpeg + ffprobe)"
    $curl = "curl.exe"
    $splitJson = & $curl -s -F "file=@.github/testdata/tone.mp3" "$base/api/split"
    $split = $splitJson | ConvertFrom-Json
    $status = $null
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        $status = Invoke-RestMethod "$base/api/split/status/$($split.job_id)"
        if ($status.status -ne "splitting") { break }
    }
    if ($status.status -ne "done") { Fail "нарезка: $($status.error)" }
    else { Pass "нарезано частей: $($status.files.Count) ($($status.files[0].name))" }

} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host "ПРОВЕРКА НЕ ПРОЙДЕНА:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "Все проверки пройдены." -ForegroundColor Green
