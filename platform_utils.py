"""
Cross-platform helpers for Busy (macOS / Windows / Linux).

Everything the app does that differs between operating systems lives here:
paths, opening files in the file manager, launching subprocesses without
flashing console windows, and finding/installing the external tools
(ffmpeg, yt-dlp, tdl) the app depends on.
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile
import tempfile
import urllib.request
from pathlib import Path

# True inside a PyInstaller bundle, where sys.executable is Busy itself
# rather than a Python interpreter.
FROZEN = getattr(sys, "frozen", False)

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
IS_LINUX = not IS_MAC and not IS_WIN

APP_NAME = "Busy"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def data_dir() -> str:
    """Per-user writable directory for the DB, temp files and bundled tools.

    The app directory itself is not usable: inside a PyInstaller bundle or an
    /Applications install it can be read-only.
    """
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), "AppData", "Local")
        path = os.path.join(base, APP_NAME)
    elif IS_MAC:
        path = os.path.join(Path.home(), "Library", "Application Support", APP_NAME)
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
        path = os.path.join(base, "busy")
    os.makedirs(path, exist_ok=True)
    return path


def bin_dir() -> str:
    """Directory for tools Busy downloads itself (ffmpeg on Windows, etc.)."""
    path = os.path.join(data_dir(), "bin")
    os.makedirs(path, exist_ok=True)
    return path


def default_download_dir() -> str:
    """The user's Downloads folder, in a way that works on every OS."""
    if IS_WIN:
        # Respect a relocated Downloads folder (OneDrive, another drive, ...).
        try:
            import winreg  # noqa: PLC0415

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            winreg.CloseKey(key)
            if value and os.path.isdir(value):
                return value
        except OSError:
            pass
    return os.path.join(Path.home(), "Downloads")


# ---------------------------------------------------------------------------
# PATH
# ---------------------------------------------------------------------------

_EXTRA_PATHS_MAC = [
    "/opt/homebrew/bin",       # Apple Silicon Homebrew
    "/usr/local/bin",          # Intel Homebrew
    "/opt/local/bin",          # MacPorts
    os.path.join(Path.home(), ".local", "bin"),
]

_EXTRA_PATHS_LINUX = [
    "/usr/local/bin",
    "/snap/bin",
    os.path.join(Path.home(), ".local", "bin"),
]


def _extra_paths():
    paths = [bin_dir()]
    if IS_MAC:
        paths += _EXTRA_PATHS_MAC
    elif IS_LINUX:
        paths += _EXTRA_PATHS_LINUX
    else:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            paths.append(os.path.join(local, "Microsoft", "WindowsApps"))
            paths.append(os.path.join(local, "Programs"))
        scoop = os.path.join(Path.home(), "scoop", "shims")
        paths.append(scoop)
        paths.append(r"C:\ProgramData\chocolatey\bin")
    return [p for p in paths if p and os.path.isdir(p)]


def ensure_path():
    """Prepend common tool locations to PATH.

    A GUI app launched from Finder / the Start menu inherits a minimal PATH
    that usually misses Homebrew, Scoop and our own bin directory, so every
    `ffmpeg` lookup would fail even though the tool is installed.
    """
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    for p in _extra_paths():
        if p not in parts:
            parts.insert(0, p)
    os.environ["PATH"] = os.pathsep.join(parts)


ensure_path()


def which(cmd: str):
    """Locate an executable, including ones Busy downloaded itself."""
    found = shutil.which(cmd)
    if found:
        return found
    for d in _extra_paths():
        for name in ([cmd, cmd + ".exe"] if IS_WIN else [cmd]):
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def ffmpeg_path() -> str:
    return which("ffmpeg") or "ffmpeg"


def ffprobe_path() -> str:
    return which("ffprobe") or "ffprobe"


def tdl_path() -> str:
    return which("tdl") or "tdl"


YTDLP_URLS = {
    "win": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "mac": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    "linux": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
}


def ytdlp_cmd():
    """How to invoke yt-dlp on this install.

    Order matters: a binary Busy downloaded itself is always the freshest,
    then the copy inside our own Python environment. In a frozen build
    `python -m yt_dlp` is impossible, so the app re-executes itself with a
    `--ytdlp` switch that hands over to yt-dlp's own entry point.
    """
    downloaded = which("yt-dlp")
    if downloaded and os.path.dirname(downloaded) == bin_dir():
        return [downloaded]
    if FROZEN:
        return [sys.executable, "--ytdlp"]
    try:
        import yt_dlp  # noqa: F401,PLC0415

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return [downloaded or "yt-dlp"]


def download_ytdlp(progress=None) -> str:
    """Fetch the standalone yt-dlp build into Busy's bin directory.

    This is how a packaged app stays current: yt-dlp breaks whenever YouTube
    changes something, and users should not have to reinstall Busy for that.
    """
    key = "win" if IS_WIN else ("mac" if IS_MAC else "linux")
    url = YTDLP_URLS[key]
    target = os.path.join(bin_dir(), "yt-dlp.exe" if IS_WIN else "yt-dlp")
    tmp = target + ".part"

    if progress:
        progress("Скачиваю yt-dlp...")
    with urllib.request.urlopen(url, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(f"Скачиваю yt-dlp... {done * 100 // total}%")

    os.replace(tmp, target)
    if not IS_WIN:
        os.chmod(target, 0o755)
    ensure_path()
    return target


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------

if IS_WIN:
    # Keep console windows from flashing on every yt-dlp / ffmpeg call.
    _CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
else:
    _CREATIONFLAGS = 0


def _decorate(kwargs: dict) -> dict:
    if IS_WIN:
        kwargs.setdefault("creationflags", _CREATIONFLAGS)
        # A windowed build has no console, so the inherited standard handles
        # are invalid and a child process dies with "The handle is invalid"
        # unless every stream is given explicitly.
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        if "stdout" not in kwargs and not kwargs.get("capture_output"):
            kwargs.setdefault("stdout", subprocess.DEVNULL)
            kwargs.setdefault("stderr", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        # Windows consoles are rarely UTF-8; decoding with the locale codepage
        # raises UnicodeDecodeError on yt-dlp/ffmpeg output.
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return kwargs


def run(cmd, **kwargs):
    """subprocess.run with the platform quirks handled."""
    return subprocess.run(cmd, **_decorate(kwargs))


def popen(cmd, **kwargs):
    """subprocess.Popen with the platform quirks handled."""
    return subprocess.Popen(cmd, **_decorate(kwargs))


def kill_pid(pid: int):
    """Terminate a process by pid on any platform."""
    if not pid:
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=_CREATIONFLAGS,
            )
        else:
            import signal  # noqa: PLC0415

            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# File manager integration
# ---------------------------------------------------------------------------


def file_manager_name() -> str:
    if IS_MAC:
        return "Finder"
    if IS_WIN:
        return "Проводнике"
    return "менеджере файлов"


def open_path(path: str) -> bool:
    """Open a file or folder with the OS default handler."""
    try:
        if IS_MAC:
            popen(["open", path])
        elif IS_WIN:
            os.startfile(path)  # noqa: S606  (Windows-only API)
        else:
            popen(["xdg-open", path])
        return True
    except OSError:
        return False


def reveal_path(path: str) -> bool:
    """Show a file (or folder) selected in the OS file manager."""
    try:
        if IS_MAC:
            if os.path.isfile(path):
                popen(["open", "-R", path])
            else:
                popen(["open", path])
        elif IS_WIN:
            if os.path.isfile(path):
                # explorer.exe returns 1 even on success — don't check the code.
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # noqa: S606
        else:
            target = path if os.path.isdir(path) else os.path.dirname(path)
            popen(["xdg-open", target])
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

FFMPEG_WIN_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/"
    "7.1/ffmpeg-7.1-essentials_build.zip"
)


def package_manager():
    """The package manager Busy can install dependencies with, if any."""
    if IS_MAC:
        return "brew" if which("brew") else None
    if IS_WIN:
        # ffmpeg and tdl are downloaded directly; winget only helps yt-dlp.
        return "winget" if which("winget") else None
    for mgr in ("apt-get", "dnf", "pacman"):
        if which(mgr):
            return mgr
    return None


def dep_specs(ytdlp_version=None):
    """Describe the external tools for the onboarding screen.

    `pkg` is what /api/deps/install accepts; a dep with no `pkg` can only be
    installed by hand (the UI then shows `manual_url`).
    """
    mgr = package_manager()
    deps = []

    if IS_MAC:
        deps.append({
            "id": "brew",
            "name": "Homebrew",
            "description": "Менеджер пакетов для macOS. Нужен для установки остальных инструментов.",
            "installed": bool(which("brew")),
            "required": True,
            "manual_url": "https://brew.sh",
            "pkg": None,
        })

    deps.append({
        "id": "ffmpeg",
        "name": "FFmpeg",
        "description": "Обработка видео и аудио. Нужен для скачивания видео и нарезки аудио.",
        "installed": bool(which("ffmpeg")),
        "required": True,
        "pkg": "ffmpeg" if (mgr or IS_WIN) else None,
        "manual_url": "https://ffmpeg.org/download.html",
    })

    deps.append({
        "id": "yt-dlp",
        "name": "yt-dlp",
        "description": "Загрузка видео с YouTube, TikTok, Instagram и 1000+ сайтов.",
        "installed": bool(ytdlp_version),
        "required": True,
        "pkg": "yt-dlp",
        "version": ytdlp_version,
        "manual_url": "https://github.com/yt-dlp/yt-dlp",
    })

    tdl_pkg = None
    if IS_MAC and mgr == "brew":
        tdl_pkg = "telegram-downloader"
    elif IS_WIN:
        tdl_pkg = "tdl"  # Busy downloads it itself — no package manager has it
    deps.append({
        "id": "tdl",
        "name": "TDL (Telegram Downloader)",
        "description": "Скачивание медиа и сообщений из Telegram чатов и каналов.",
        "installed": bool(which("tdl")),
        "required": False,
        "pkg": tdl_pkg,
        "manual_url": "https://github.com/iyear/tdl/releases",
    })

    return deps


def install_hint(dep_id: str) -> str:
    """Copy-pasteable command for installing a dependency by hand."""
    if IS_MAC:
        return {
            "ffmpeg": "brew install ffmpeg",
            "yt-dlp": "brew install yt-dlp",
            "tdl": "brew install telegram-downloader",
        }.get(dep_id, "")
    if IS_WIN:
        return {
            "ffmpeg": "winget install Gyan.FFmpeg",
            "yt-dlp": "winget install yt-dlp.yt-dlp",
            # tdl is in no package manager: the app fetches the release zip.
            # The manual route is the release archive, not a package command.
            "tdl": r"tdl.exe из tdl_Windows_64bit.zip → %LOCALAPPDATA%\Busy\bin",
        }.get(dep_id, "")
    mgr = package_manager() or "apt-get"
    verb = {"apt-get": "sudo apt-get install", "dnf": "sudo dnf install",
            "pacman": "sudo pacman -S"}.get(mgr, "sudo apt-get install")
    return f"{verb} {dep_id}"


def install_commands(pkg: str):
    """Commands that install `pkg`, or None when Busy handles it another way."""
    mgr = package_manager()
    if IS_MAC and mgr == "brew":
        return [["brew", "install", pkg]]
    if IS_WIN and mgr == "winget":
        ids = {"ffmpeg": "Gyan.FFmpeg", "yt-dlp": "yt-dlp.yt-dlp"}
        wid = ids.get(pkg)
        if not wid:
            return None  # not in winget (tdl) — handled by a direct download
        return [["winget", "install", "--id", wid, "-e", "--accept-package-agreements",
                 "--accept-source-agreements"]]
    if IS_LINUX:
        if mgr == "apt-get":
            return [["sudo", "apt-get", "install", "-y", pkg]]
        if mgr == "dnf":
            return [["sudo", "dnf", "install", "-y", pkg]]
        if mgr == "pacman":
            return [["sudo", "pacman", "-S", "--noconfirm", pkg]]
    return None


def download_ffmpeg_windows(progress=None) -> str:
    """Download a static ffmpeg build into Busy's own bin directory.

    Windows has no package manager we can rely on, so the app fetches ffmpeg
    itself — no admin rights, nothing installed system-wide.
    """
    if not IS_WIN:
        raise RuntimeError("Windows only")

    target = bin_dir()
    if progress:
        progress("Скачиваю FFmpeg...")

    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "ffmpeg.zip")
        with urllib.request.urlopen(FFMPEG_WIN_URL, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(archive, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(f"Скачиваю FFmpeg... {done * 100 // total}%")

        if progress:
            progress("Распаковываю FFmpeg...")
        with zipfile.ZipFile(archive) as zf:
            wanted = ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")
            for member in zf.namelist():
                name = os.path.basename(member)
                if name in wanted:
                    with zf.open(member) as src, open(os.path.join(target, name), "wb") as dst:
                        shutil.copyfileobj(src, dst)

    exe = os.path.join(target, "ffmpeg.exe")
    if not os.path.exists(exe):
        raise RuntimeError("FFmpeg не найден в архиве")
    ensure_path()
    return exe


TDL_WIN_URL = "https://github.com/iyear/tdl/releases/latest/download/tdl_Windows_64bit.zip"


def download_tdl_windows(progress=None) -> str:
    """Download the Telegram downloader into Busy's own bin directory.

    tdl is not in winget or scoop, so the only honest options are "download a
    zip from GitHub by hand" or this.
    """
    if not IS_WIN:
        raise RuntimeError("Windows only")

    target = bin_dir()
    if progress:
        progress("Скачиваю tdl...")

    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "tdl.zip")
        with urllib.request.urlopen(TDL_WIN_URL, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(archive, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(f"Скачиваю tdl... {done * 100 // total}%")

        if progress:
            progress("Распаковываю tdl...")
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if os.path.basename(member).lower() == "tdl.exe":
                    with zf.open(member) as src, open(os.path.join(target, "tdl.exe"), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break

    exe = os.path.join(target, "tdl.exe")
    if not os.path.exists(exe):
        raise RuntimeError("tdl.exe не найден в архиве")
    ensure_path()
    return exe


def platform_info():
    """Small summary the frontend uses to adapt its wording."""
    return {
        "os": "mac" if IS_MAC else ("windows" if IS_WIN else "linux"),
        "file_manager": file_manager_name(),
        "package_manager": package_manager(),
        "can_auto_install": bool(package_manager()) or IS_WIN,
    }


def sanitize_filename(name: str) -> str:
    """Strip characters Windows refuses in file names (and trailing dots)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip()
    return name.rstrip(". ") or "file"
