# Проверка окна приложения в тех же условиях, что у обычного пользователя.
#
# Файлы, распакованные из скачанного архива, Windows помечает как «пришедшие
# из интернета» (поток Zone.Identifier). Из-за этой метки .NET отказывался
# грузить Python.Runtime.dll, и нативное окно не открывалось вовсе.
# Здесь метка ставится намеренно — приложение должно с ней справиться.

$ErrorActionPreference = "Stop"

$exe      = "./dist/Busy/Busy.exe"
$dataDir  = Join-Path $env:LOCALAPPDATA "Busy"
$portFile = Join-Path $dataDir "port"

Write-Host "=== Помечаю файлы как скачанные из интернета (Mark of the Web) ==="
$marked = 0
Get-ChildItem ./dist/Busy -Recurse -Include *.dll, *.exe | ForEach-Object {
    Set-Content -Path $_.FullName -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
    $marked++
}
Write-Host "  помечено файлов: $marked"

Remove-Item $portFile -ErrorAction SilentlyContinue

Write-Host "`n=== Запуск в обычном режиме (с окном) ==="
$proc = Start-Process $exe -PassThru -RedirectStandardOutput gui-out.txt -RedirectStandardError gui-err.txt

try {
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        if ($proc.HasExited) { break }
        if (-not (Test-Path $portFile)) { continue }
        $port = (Get-Content $portFile -Raw).Trim()
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }

    $out = (Get-Content gui-out.txt -Raw -ErrorAction SilentlyContinue) + "`n" +
           (Get-Content gui-err.txt -Raw -ErrorAction SilentlyContinue)

    if (-not $ok) {
        Write-Host "--- вывод приложения ---"; Write-Host $out
        if ($proc.HasExited) { throw "Приложение упало (код выхода $($proc.ExitCode))" }
        throw "Приложение не подняло интерфейс"
    }
    Write-Host "  OK: приложение работает с меткой из интернета"

    if ($out -match "Нативное окно недоступно") {
        Write-Host "--- вывод приложения ---"; Write-Host $out
        throw "Нативное окно не открылось — сработал запасной вариант"
    }
    Write-Host "  OK: открылось нативное окно (WebView2), запасной вариант не понадобился"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Запасной путь. На части машин .NET не грузится ("Failed to resolve
# Python.Runtime.Loader.Initialize") — приложение обязано не падать, а
# открыться отдельным окном Edge.
# ---------------------------------------------------------------------------
Write-Host "`n=== Запасной путь: нативное окно недоступно ==="
Remove-Item $portFile -ErrorAction SilentlyContinue
$env:BUSY_NO_WEBVIEW = "1"
$proc = Start-Process $exe -PassThru -RedirectStandardOutput fb-out.txt -RedirectStandardError fb-err.txt

try {
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        if ($proc.HasExited) { break }
        if (-not (Test-Path $portFile)) { continue }
        $port = (Get-Content $portFile -Raw).Trim()
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) {
        Write-Host "--- вывод приложения ---"
        Get-Content fb-out.txt, fb-err.txt -ErrorAction SilentlyContinue
        if ($proc.HasExited) { throw "Приложение упало вместо запасного окна (код $($proc.ExitCode))" }
        throw "Запасное окно не подняло интерфейс"
    }
    $edge = Get-Process msedge, chrome -ErrorAction SilentlyContinue
    if (-not $edge) { throw "Окно Edge/Chrome не открылось" }
    Write-Host "  OK: приложение открылось отдельным окном браузера и продолжает работать"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Get-Process msedge, chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
