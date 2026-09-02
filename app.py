import os
import re
import uuid
import glob
import json
import time
import base64
import sqlite3
import sys
import socket
import struct
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file, render_template

import platform_utils as plat

# When packaged with PyInstaller the templates live in the unpacked bundle
# directory (sys._MEIPASS), not next to this file.
_RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(_RESOURCE_DIR, "templates"),
    static_folder=os.path.join(_RESOURCE_DIR, "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Writable per-user directory (Application Support / %LOCALAPPDATA% / ~/.local).
# Never write next to the source: an installed app can sit in a read-only place.
DATA_DIR = plat.data_dir()
TMP_DIR = os.path.join(DATA_DIR, "tmp")
os.makedirs(TMP_DIR, exist_ok=True)

DOWNLOAD_DIR = (
    os.environ.get("BUSY_DOWNLOAD_DIR")
    or os.environ.get("RECLIP_DOWNLOAD_DIR")
    or plat.default_download_dir()
)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "history.db")

# Carry over history from older versions that kept the DB beside the code.
_legacy_db = os.path.join(APP_DIR, "history.db")
if os.path.exists(_legacy_db) and not os.path.exists(DB_PATH):
    try:
        shutil.copy2(_legacy_db, DB_PATH)
    except OSError:
        pass
MAX_JOBS = 200
CLEANUP_INTERVAL = 600  # 10 min
FILE_MAX_AGE = 3600  # 1 hour


# ---------------------------------------------------------------------------
# yt-dlp invocation
# Prefer the yt_dlp installed in THIS interpreter's environment (the app's
# venv) over a bare `yt-dlp` on PATH — the latter can be an older Homebrew
# build or a stale/relocated shim, which is exactly what caused downloads to
# fail. `python -m yt_dlp` is identical to the CLI but always the right copy.
# ---------------------------------------------------------------------------
YTDLP = plat.ytdlp_cmd()


def _refresh_ytdlp_cmd():
    """Re-resolve yt-dlp after an install/update swapped it out."""
    global YTDLP
    YTDLP = plat.ytdlp_cmd()
    return YTDLP

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
jobs = {}  # job_id -> dict
jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Rate limiter (simple token bucket per IP)
# ---------------------------------------------------------------------------
_rate_buckets = {}
_rate_lock = threading.Lock()
RATE_LIMIT = 30  # requests per minute
RATE_WINDOW = 60


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.get(ip, [])
        bucket = [t for t in bucket if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            return False
        bucket.append(now)
        _rate_buckets[ip] = bucket
    return True


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
_PRIVATE_RANGES = [
    ("127.0.0.0", "127.255.255.255"),
    ("10.0.0.0", "10.255.255.255"),
    ("172.16.0.0", "172.31.255.255"),
    ("192.168.0.0", "192.168.255.255"),
    ("169.254.0.0", "169.254.255.255"),
    ("0.0.0.0", "0.255.255.255"),
]


def _ip_to_int(ip: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip))[0]


_PRIVATE_INT = [(_ip_to_int(lo), _ip_to_int(hi)) for lo, hi in _PRIVATE_RANGES]


def _is_private_ip(ip: str) -> bool:
    try:
        n = _ip_to_int(ip)
    except (OSError, struct.error):
        return True
    return any(lo <= n <= hi for lo, hi in _PRIVATE_INT)


def validate_url(url: str) -> str | None:
    """Return error message or None if URL is valid."""
    try:
        p = urlparse(url)
    except Exception:
        return "Invalid URL"
    if p.scheme not in ("http", "https"):
        return "Only http/https URLs are allowed"
    host = p.hostname or ""
    if not host:
        return "No host in URL"
    if host in ("localhost", "localhost.localdomain"):
        return "Localhost URLs are not allowed"
    try:
        addr = socket.gethostbyname(host)
        if _is_private_ip(addr):
            return "Private/internal URLs are not allowed"
    except socket.gaierror:
        pass
    return None


# ---------------------------------------------------------------------------
# SQLite history
# ---------------------------------------------------------------------------
def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            filename TEXT,
            format TEXT,
            file_size INTEGER,
            duration REAL,
            thumbnail TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    return conn


def _add_history(url, title, filename, fmt, file_size, duration=None, thumbnail=None):
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO history (url, title, filename, format, file_size, duration, thumbnail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, title, filename, fmt, file_size, duration, thumbnail),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cleanup thread
# ---------------------------------------------------------------------------
def _cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            now = time.time()
            tmp_dir = os.path.join(os.path.dirname(__file__), "downloads")
            if os.path.isdir(tmp_dir):
                for f in os.listdir(tmp_dir):
                    fp = os.path.join(tmp_dir, f)
                    if os.path.isfile(fp) and now - os.path.getmtime(fp) > FILE_MAX_AGE:
                        try:
                            os.remove(fp)
                        except OSError:
                            pass
            with jobs_lock:
                if len(jobs) > MAX_JOBS:
                    removable = [
                        (k, v)
                        for k, v in jobs.items()
                        if v.get("status") in ("done", "error")
                    ]
                    removable.sort(key=lambda x: x[1].get("created_at", 0))
                    for k, _ in removable[: len(jobs) - MAX_JOBS]:
                        del jobs[k]
        except Exception:
            pass


_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------
AUDIO_FORMATS = {"mp3", "wav", "flac", "aac", "m4a", "opus"}
VIDEO_FORMATS = {"mp4", "webm", "mkv"}
AUDIO_QUALITIES = {"64k", "128k", "192k", "256k", "320k"}


def _ytdlp_version():
    try:
        r = plat.run([*YTDLP, "--version"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _extract_ytdlp_error(lines):
    """Pick the most meaningful line from yt-dlp output for the user."""
    for line in reversed(lines):
        if line.startswith("ERROR"):
            # Drop the leading "ERROR:" and yt-dlp's noisy prefixes
            return line.split("ERROR:", 1)[-1].strip() or line
    return lines[-1] if lines else "Download failed"


def _sanitize_filename(title: str, ext: str) -> str:
    if not title:
        return f"download{ext}"
    safe = plat.sanitize_filename(title)[:80].strip()
    return f"{safe}{ext}" if safe else f"download{ext}"


def run_download(job_id, url, format_choice, format_id, audio_format, audio_quality):
    job = jobs[job_id]
    tmp_dir = TMP_DIR
    out_template = os.path.join(tmp_dir, f"{job_id}.%(ext)s")

    cmd = [*YTDLP, "--no-playlist", "-o", out_template, "--no-warnings"]

    # Point yt-dlp at the ffmpeg we found (Busy may have downloaded its own).
    _ffmpeg = plat.which("ffmpeg")
    if _ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(_ffmpeg)]

    is_audio = format_choice == "audio"
    target_ext = None

    if is_audio:
        af = audio_format if audio_format in AUDIO_FORMATS else "mp3"
        aq = audio_quality if audio_quality in AUDIO_QUALITIES else "192k"
        cmd += ["-x", "--audio-format", af, "--audio-quality", aq]
        target_ext = f".{af}"
    else:
        if format_id:
            cmd += ["-f", f"{format_id}+bestaudio/best"]
        else:
            cmd += ["-f", "bestvideo+bestaudio/best"]
        cmd += ["--merge-output-format", "mp4"]
        target_ext = ".mp4"

    # Progress to file for polling
    progress_file = os.path.join(tmp_dir, f"{job_id}.progress")
    cmd += ["--newline", "--progress-template",
            "%(progress._percent_str)s %(progress._speed_str)s %(progress._eta_str)s"]

    cmd.append(url)

    try:
        process = plat.popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        job["pid"] = process.pid

        recent_lines = []  # keep last non-progress lines to surface real errors
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            if "%" in line:
                try:
                    with open(progress_file, "w") as pf:
                        pf.write(line)
                except Exception:
                    pass
            else:
                recent_lines.append(line)
                if len(recent_lines) > 20:
                    recent_lines.pop(0)

        process.wait(timeout=300)

        if process.returncode != 0:
            job["status"] = "error"
            job["error"] = _extract_ytdlp_error(recent_lines)
            return

        files = glob.glob(os.path.join(tmp_dir, f"{job_id}.*"))
        files = [f for f in files if not f.endswith(".progress")]
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        target = [f for f in files if f.endswith(target_ext)]
        chosen = target[0] if target else files[0]

        title = job.get("title", "").strip()
        ext = os.path.splitext(chosen)[1]
        final_name = _sanitize_filename(title, ext)
        final_path = os.path.join(DOWNLOAD_DIR, final_name)

        if os.path.exists(final_path):
            base, extension = os.path.splitext(final_name)
            final_name = f"{base}_{job_id[:6]}{extension}"
            final_path = os.path.join(DOWNLOAD_DIR, final_name)

        # Downloads may live on another volume — os.rename would fail there.
        shutil.move(chosen, final_path)

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass
        try:
            os.remove(progress_file)
        except OSError:
            pass

        file_size = os.path.getsize(final_path)
        job["status"] = "done"
        job["file"] = final_path
        job["filename"] = final_name
        job["file_size"] = file_size

        _add_history(
            url=job.get("url", url),
            title=title,
            filename=final_name,
            fmt=f"audio/{audio_format}" if is_audio else "video/mp4",
            file_size=file_size,
            duration=job.get("duration"),
            thumbnail=job.get("thumbnail"),
        )

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    if not _check_rate_limit(request.remote_addr):
        return jsonify({"error": "Rate limit exceeded. Try again in a minute."}), 429

    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    err = validate_url(url)
    if err:
        return jsonify({"error": err}), 400

    cmd = [*YTDLP, "--no-playlist", "-j", "--no-warnings", url]
    try:
        result = plat.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            errmsg = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else "Failed to fetch info"
            return jsonify({"error": errmsg}), 400

        info = json.loads(result.stdout)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist():
    if not _check_rate_limit(request.remote_addr):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    err = validate_url(url)
    if err:
        return jsonify({"error": err}), 400

    cmd = [*YTDLP, "--flat-playlist", "-j", "--no-warnings", url]
    try:
        result = plat.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return jsonify({"error": "Failed to fetch playlist"}), 400

        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entries.append({
                    "url": entry.get("url") or entry.get("webpage_url", ""),
                    "title": entry.get("title", "Untitled"),
                    "duration": entry.get("duration"),
                })
            except json.JSONDecodeError:
                continue

        return jsonify({"entries": entries, "count": len(entries)})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    if not _check_rate_limit(request.remote_addr):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.json or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    audio_format = data.get("audio_format", "mp3")
    audio_quality = data.get("audio_quality", "192k")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    err = validate_url(url)
    if err:
        return jsonify({"error": err}), 400

    if format_id and not re.match(r"^[a-zA-Z0-9+\-]+$", format_id):
        return jsonify({"error": "Invalid format ID"}), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "status": "downloading",
            "url": url,
            "title": title,
            "created_at": time.time(),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
        }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_choice, format_id, audio_format, audio_quality),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    progress = ""
    progress_file = os.path.join(os.path.dirname(__file__), "downloads", f"{job_id}.progress")
    try:
        if os.path.exists(progress_file):
            with open(progress_file) as pf:
                progress = pf.read().strip()
    except Exception:
        pass

    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "file_size": job.get("file_size"),
        "output_path": job.get("file"),
        "progress": progress,
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    filepath = job.get("file", "")
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found on disk"}), 404

    return send_file(filepath, as_attachment=True, download_name=job.get("filename", "download"))


@app.route("/api/history")
def get_history():
    try:
        conn = _get_db()
        rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 200").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/<int:hid>", methods=["DELETE"])
def delete_history(hid):
    try:
        conn = _get_db()
        conn.execute("DELETE FROM history WHERE id = ?", (hid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/clear", methods=["POST"])
def clear_history():
    try:
        conn = _get_db()
        conn.execute("DELETE FROM history")
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-folder")
def open_folder():
    try:
        plat.open_path(DOWNLOAD_DIR)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-url", methods=["POST"])
def open_url():
    """Open an external link in the real browser (not inside the app window)."""
    url = (request.json or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400
    import webbrowser
    webbrowser.open(url)
    return jsonify({"ok": True})


@app.route("/api/deps/check")
def deps_check():
    """Report which external tools are present on this machine."""
    deps = plat.dep_specs(ytdlp_version=_ytdlp_version())
    for d in deps:
        d["hint"] = plat.install_hint(d["id"])
        # Older frontends read brew_pkg; keep both keys working.
        d["brew_pkg"] = d.get("pkg")
    ready = all(d["installed"] for d in deps if d["required"])
    return jsonify({"deps": deps, "ready": ready, "platform": plat.platform_info()})


_deps_install_jobs = {}


@app.route("/api/deps/install", methods=["POST"])
def deps_install():
    """Install one dependency with whatever mechanism this OS offers."""
    data = request.json or {}
    pkg = data.get("pkg", "").strip()

    # Whitelist — never pass user input to a package manager unchecked.
    allowed = {"ffmpeg", "yt-dlp", "telegram-downloader", "tdl"}
    if pkg not in allowed:
        return jsonify({"error": "Package not allowed"}), 400

    job_id = uuid.uuid4().hex[:12]
    _deps_install_jobs[job_id] = {"status": "installing", "pkg": pkg, "output": ""}

    def _run():
        job = _deps_install_jobs[job_id]
        try:
            # A packaged build has no pip: fetch the standalone yt-dlp build.
            if pkg == "yt-dlp" and plat.FROZEN:
                plat.download_ytdlp(progress=lambda m: job.__setitem__("output", m))
                _refresh_ytdlp_cmd()
                job["status"] = "done"
                job["version"] = _ytdlp_version()
                return
            # yt-dlp lives in our own Python environment — pip owns it.
            if pkg == "yt-dlp" and YTDLP[0] == sys.executable:
                cmds = [[sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]]
            # Windows: download ffmpeg ourselves — no package manager needed,
            # no UAC prompt hiding behind the app window.
            elif pkg == "ffmpeg" and plat.IS_WIN:
                plat.download_ffmpeg_windows(progress=lambda m: job.__setitem__("output", m))
                job["status"] = "done"
                return
            else:
                cmds = plat.install_commands(pkg)

            if not cmds:
                job["status"] = "error"
                job["error"] = "Автоустановка недоступна — установите вручную: " + plat.install_hint(pkg)
                return

            last_code = 1
            for cmd in cmds:
                proc = plat.popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        job["output"] = line[:200]
                proc.wait(timeout=900)
                last_code = proc.returncode
                if last_code == 0:
                    break

            plat.ensure_path()
            job["status"] = "done" if last_code == 0 else "error"
            if last_code != 0:
                job["error"] = "Установка не удалась. Вручную: " + plat.install_hint(pkg)
        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = "Превышено время ожидания"
        except FileNotFoundError:
            job["status"] = "error"
            job["error"] = "Менеджер пакетов не найден. Вручную: " + plat.install_hint(pkg)
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/deps/install/status/<job_id>")
def deps_install_status(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    job = _deps_install_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "output": job.get("output", ""),
        "version": job.get("version"),
    })


def _resolve_ytdlp_python():
    """Find the exact python interpreter that owns the yt-dlp on PATH.

    Handles version-manager shims (pyenv/asdf/rbenv) where `which yt-dlp`
    returns a dispatcher script rather than the real console-script, so a
    bare `python3 -m pip` would target the wrong environment.
    """
    if plat.IS_WIN:
        # Windows console-scripts are .exe wrappers with no shebang to read.
        return None
    which = shutil.which("yt-dlp")
    if not which:
        return None
    candidates = [which]
    try:
        with open(which, "rb") as fh:
            head = fh.read(2048).decode("utf-8", "replace")
    except OSError:
        head = ""
    if "/shims/" in which or "PYENV_ROOT" in head:
        pyenv_root = os.path.dirname(os.path.dirname(which))
        m = re.search(r'exec\s+"([^"]*/(?:pyenv|asdf|rbenv))"', head)
        mgr_bin = (m.group(1) if m else None) or shutil.which("pyenv")
        if mgr_bin and os.path.exists(mgr_bin):
            try:
                env = {**os.environ, "PYENV_ROOT": pyenv_root}
                r = plat.run(
                    [mgr_bin, "which", "yt-dlp"],
                    capture_output=True, text=True, timeout=8, env=env,
                )
                if r.returncode == 0 and r.stdout.strip():
                    candidates.insert(0, r.stdout.strip())
            except (subprocess.SubprocessError, OSError):
                pass
    # The console-script's shebang names the interpreter that owns yt-dlp.
    for path in candidates:
        try:
            real = os.path.realpath(path)
            with open(real, "rb") as fh:
                first = fh.readline(512).decode("utf-8", "replace").strip()
        except OSError:
            continue
        if first.startswith("#!"):
            interp = first[2:].lstrip().split()[0] if first[2:].strip() else ""
            if interp and "python" in os.path.basename(interp) and os.path.exists(interp):
                return interp
    return None


def _ytdlp_update_cmds():
    """Ordered list of update commands to try, based on how yt-dlp was installed."""
    cmds = []
    if YTDLP[0] == sys.executable:
        # yt-dlp runs as a module in our venv — update it with our own pip
        cmds.append([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    else:
        # Bare PATH binary — figure out how it was installed
        real = os.path.realpath(plat.which("yt-dlp") or "")
        if "/Cellar/" in real or "/Caskroom/" in real:
            cmds.append(["brew", "upgrade", "yt-dlp"])
        elif plat.IS_WIN and plat.which("winget"):
            cmds.append(["winget", "upgrade", "--id", "yt-dlp.yt-dlp", "-e",
                         "--accept-package-agreements", "--accept-source-agreements"])
        py = _resolve_ytdlp_python()
        if py:
            cmds.append([py, "-m", "pip", "install", "-U", "yt-dlp"])
        cmds.append([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
        if not plat.IS_WIN:
            cmds.append(["python3", "-m", "pip", "install", "-U", "yt-dlp"])
            cmds.append(["pip3", "install", "-U", "yt-dlp"])
    cmds.append([*YTDLP, "-U"])  # standalone-binary self-update fallback
    return cmds


@app.route("/api/deps/update", methods=["POST"])
def deps_update():
    """Update yt-dlp in place. Reuses /api/deps/install/status for polling."""
    data = request.json or {}
    pkg = data.get("pkg", "yt-dlp").strip()
    if pkg != "yt-dlp":
        return jsonify({"error": "Only yt-dlp can be updated"}), 400

    before = _ytdlp_version()
    job_id = uuid.uuid4().hex[:12]
    _deps_install_jobs[job_id] = {"status": "installing", "pkg": pkg, "output": ""}

    def _run():
        job = _deps_install_jobs[job_id]
        last_err = ""
        # Packaged build: swap in a freshly downloaded standalone binary.
        uses_own_binary = os.path.dirname(os.path.abspath(YTDLP[0])) == plat.bin_dir()
        if plat.FROZEN or uses_own_binary:
            try:
                plat.download_ytdlp(progress=lambda m: job.__setitem__("output", m))
                _refresh_ytdlp_cmd()
                job["version"] = _ytdlp_version()
                job["status"] = "done"
                return
            except Exception as e:
                last_err = str(e)

        for cmd in _ytdlp_update_cmds():
            try:
                proc = plat.popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                for line in proc.stdout:
                    job["output"] = line.strip()[:200]
                proc.wait(timeout=600)
                if proc.returncode == 0:
                    # Set version before status so pollers never see done-without-version
                    _refresh_ytdlp_cmd()
                    job["version"] = _ytdlp_version()
                    job["status"] = "done"
                    return
                last_err = job.get("output") or "Обновление не удалось"
            except FileNotFoundError:
                last_err = f"{cmd[0]} не найден"
                continue
            except subprocess.TimeoutExpired:
                job["status"] = "error"
                job["error"] = "Превышено время ожидания"
                return
            except Exception as e:
                last_err = str(e)
                continue
        # All commands failed — but check whether the version moved anyway
        after = _ytdlp_version()
        if after and after != before:
            job["version"] = after
            job["status"] = "done"
            return
        job["status"] = "error"
        job["error"] = last_err or "Не удалось обновить yt-dlp"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/reveal", methods=["POST"])
def reveal_path():
    """Reveal a file or directory in the OS file manager. Restricted to DOWNLOAD_DIR."""
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "No path"}), 400

    # Security: only allow paths within DOWNLOAD_DIR (normcase: Windows paths
    # differ only by case and slash direction).
    abs_path = os.path.normcase(os.path.abspath(path))
    abs_download_dir = os.path.normcase(os.path.abspath(DOWNLOAD_DIR))
    if not abs_path.startswith(abs_download_dir):
        return jsonify({"error": "Path outside downloads dir"}), 403

    try:
        if os.path.exists(abs_path):
            plat.reveal_path(os.path.abspath(path))
        else:
            return jsonify({"error": "Path not found"}), 404
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-file/<int:hid>")
def open_file(hid):
    try:
        conn = _get_db()
        row = conn.execute("SELECT filename FROM history WHERE id = ?", (hid,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        filepath = os.path.join(DOWNLOAD_DIR, row["filename"])
        if os.path.isfile(filepath):
            plat.reveal_path(filepath)
            return jsonify({"ok": True})
        return jsonify({"error": "File not found on disk"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config")
def get_config():
    return jsonify({
        "download_dir": DOWNLOAD_DIR,
        "platform": plat.platform_info(),
        "tdl_hint": plat.install_hint("tdl"),
    })


# ---------------------------------------------------------------------------
# TDL (Telegram) integration
# ---------------------------------------------------------------------------
_tdl_login_proc = None
_tdl_login_lock = threading.Lock()
_tdl_jobs = {}  # job_id -> dict
_tdl_jobs_lock = threading.Lock()


def _tdl_installed() -> bool:
    if not plat.which("tdl"):
        return False
    try:
        plat.run([plat.tdl_path(), "version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def _tdl_logged_in() -> bool:
    # Quick check: if session file doesn't exist, not logged in
    session_file = os.path.join(Path.home(), ".tdl", "data", "default")
    if not os.path.exists(session_file):
        return False
    try:
        result = plat.run(
            [plat.tdl_path(), "chat", "ls", "-o", "json"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "not authorized" not in result.stderr.lower()
    except Exception:
        return False


@app.route("/api/tdl/status")
def tdl_status():
    installed = _tdl_installed()
    logged_in = _tdl_logged_in() if installed else False
    return jsonify({"installed": installed, "logged_in": logged_in})


@app.route("/api/tdl/login", methods=["POST"])
def tdl_login():
    global _tdl_login_proc
    with _tdl_login_lock:
        if _tdl_login_proc and _tdl_login_proc.poll() is None:
            _tdl_login_proc.terminate()
            try:
                _tdl_login_proc.wait(timeout=3)
            except Exception:
                pass
            _tdl_login_proc = None

        proc = plat.popen(
            [plat.tdl_path(), "login", "-T", "qr"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        _tdl_login_proc = proc

    # Read output in a thread with timeout to capture QR
    output_lines = []
    qr_ready = threading.Event()

    def _reader():
        for line in proc.stdout:
            clean = line.rstrip("\n")
            # Strip ANSI escape sequences (cursor movement etc.)
            clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", clean)
            output_lines.append(clean)
            # QR contains block chars like █ ▄ ▀
            if "\u2588" in clean or "\u2580" in clean or "\u2584" in clean:
                pass  # keep collecting QR lines
            # After QR block, TDL prints empty line or waits
            if len(output_lines) > 5 and any(
                "\u2588" in l for l in output_lines
            ):
                # Check if we seem to have a complete QR (ends with █ row or ▀ row)
                if clean.startswith("\u2580") or clean.startswith("\u2588") or clean == "":
                    qr_ready.set()

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Wait up to 15 seconds for QR to appear
    qr_ready.wait(timeout=15)
    # Give a tiny bit more time for trailing lines
    time.sleep(0.3)

    # Filter to only QR lines (contain block characters)
    qr_lines = [l for l in output_lines if "\u2588" in l or "\u2580" in l or "\u2584" in l]

    if not qr_lines:
        # Maybe process already exited with error
        if proc.poll() is not None:
            all_output = "\n".join(output_lines)
            return jsonify({"error": all_output or "Login failed"}), 400
        return jsonify({"error": "QR code did not appear. Try again."}), 400

    qr_text = "\n".join(qr_lines)

    return jsonify({
        "status": "waiting",
        "qr_text": qr_text,
        "message": "Scan QR code with Telegram app",
    })


@app.route("/api/tdl/login/poll")
def tdl_login_poll():
    global _tdl_login_proc
    with _tdl_login_lock:
        if _tdl_login_proc is None:
            return jsonify({"status": "no_session"})

        ret = _tdl_login_proc.poll()
        if ret is None:
            return jsonify({"status": "waiting"})
        elif ret == 0:
            _tdl_login_proc = None
            return jsonify({"status": "success"})
        else:
            _tdl_login_proc = None
            return jsonify({"status": "error", "message": "Login failed"})


@app.route("/api/tdl/logout", methods=["POST"])
def tdl_logout():
    import shutil
    try:
        # TDL stores session in ~/.tdl/data/ (BoltDB files)
        tdl_data = os.path.join(Path.home(), ".tdl", "data")
        if os.path.isdir(tdl_data):
            shutil.rmtree(tdl_data, ignore_errors=True)
            os.makedirs(tdl_data, exist_ok=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tdl/chats")
def tdl_chats():
    try:
        result = plat.run(
            [plat.tdl_path(), "chat", "ls", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "Failed to list chats"
            if "not authorized" in err.lower():
                return jsonify({"error": "Not logged in", "code": "not_authorized"}), 401
            return jsonify({"error": err}), 400

        chats = json.loads(result.stdout)
        return jsonify(chats)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid response from tdl"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out listing chats"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tdl/download", methods=["POST"])
def tdl_download():
    if not _check_rate_limit(request.remote_addr):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.json or {}
    chat_id = data.get("chat_id", "").strip()
    chat_name = data.get("chat_name", "").strip()
    action = data.get("action", "media")
    # Actions: all, media, video, audio, photo, docs, text

    if not chat_id:
        return jsonify({"error": "No chat_id provided"}), 400

    # Validate chat_id (alphanumeric, underscores, or numeric)
    if not re.match(r"^[\w@.+-]+$", chat_id):
        return jsonify({"error": "Invalid chat ID"}), 400

    # Create channel folder structure
    safe_name = plat.sanitize_filename(chat_name or chat_id)[:60] or chat_id
    channel_dir = os.path.join(DOWNLOAD_DIR, safe_name)
    os.makedirs(channel_dir, exist_ok=True)

    # Subfolder by action type
    _ACTION_SUBFOLDER = {
        "video": "video",
        "audio": "audio",
        "photo": "photo",
        "docs": "documents",
        "text": "text",
        "media": "media",
        "all": "media",
    }
    subfolder = _ACTION_SUBFOLDER.get(action, "other")
    target_dir = os.path.join(channel_dir, subfolder)
    os.makedirs(target_dir, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    with _tdl_jobs_lock:
        _tdl_jobs[job_id] = {
            "status": "downloading",
            "created_at": time.time(),
            "progress": "",
            "chat_id": chat_id,
            "action": action,
        }

    # Filter maps for include-only downloads
    _INCLUDE_FILTERS = {
        "video": "mp4,mkv,mov,avi,webm",
        "audio": "mp3,m4a,flac,wav,ogg,opus,aac",
        "photo": "jpg,jpeg,png,gif,webp,svg",
        "docs": "pdf,doc,docx,xls,xlsx,ppt,pptx,zip,rar,7z,txt,csv",
    }

    def _strip_ansi(text):
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[?[0-9]*[A-Za-z]", "", text).strip()

    def _run_tdl_job():
        job = _tdl_jobs[job_id]
        tmp_dir = TMP_DIR
        export_file = os.path.join(tmp_dir, f"{job_id}-export.json")

        try:
            # --- Step 1: Export chat messages to get downloadable links ---
            if action == "text":
                # Text only: export with content, no media download
                job["progress"] = "Exporting messages..."
                out_file = os.path.join(target_dir, f"messages.json")
                cmd = [
                    plat.tdl_path(), "chat", "export",
                    "-c", chat_id,
                    "-o", out_file,
                    "--all", "--with-content",
                ]
                proc = plat.popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                job["pid"] = proc.pid
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        job["progress"] = _strip_ansi(line)
                proc.wait(timeout=600)

                if proc.returncode != 0:
                    job["status"] = "error"
                    job["error"] = job.get("progress", "Export failed")
                    return

                job["status"] = "done"
                job["filename"] = os.path.basename(out_file)
                job["file_size"] = os.path.getsize(out_file) if os.path.exists(out_file) else 0
                job["output_path"] = out_file
                return

            # --- Step 2: For media actions, export then download ---
            job["progress"] = "Indexing chat messages..."
            export_cmd = [
                plat.tdl_path(), "chat", "export",
                "-c", chat_id,
                "-o", export_file,
            ]
            export_proc = plat.popen(
                export_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            job["pid"] = export_proc.pid
            for line in export_proc.stdout:
                line = line.strip()
                if line:
                    job["progress"] = _strip_ansi(line)
            export_proc.wait(timeout=300)

            if export_proc.returncode != 0:
                job["status"] = "error"
                job["error"] = "Failed to index chat: " + job.get("progress", "")
                return

            if not os.path.exists(export_file):
                job["status"] = "error"
                job["error"] = "Export file not created"
                return

            # --- Step 3: Download media from exported file ---
            job["progress"] = "Downloading files..."
            dl_cmd = [
                plat.tdl_path(), "dl",
                "-f", export_file,
                "--dir", target_dir,
                "--restart",
                "--skip-same",
            ]

            # Apply include filter based on action (both lower and UPPER case)
            if action in _INCLUDE_FILTERS:
                exts = _INCLUDE_FILTERS[action]
                upper_exts = ",".join(e.upper() for e in exts.split(","))
                dl_cmd += ["-i", exts + "," + upper_exts]
            # "media" and "all" download everything (no filter)

            dl_proc = plat.popen(
                dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            job["pid"] = dl_proc.pid
            for line in dl_proc.stdout:
                line = line.strip()
                if line:
                    job["progress"] = _strip_ansi(line)
            dl_proc.wait(timeout=600)

            # Clean up temp export file
            try:
                os.remove(export_file)
            except OSError:
                pass

            if dl_proc.returncode != 0:
                job["status"] = "error"
                job["error"] = job.get("progress", "Download failed")
                return

            job["status"] = "done"
            job["output_path"] = target_dir

            # For "all" action, also export text
            if action == "all":
                job["progress"] = "Exporting messages text..."
                text_dir = os.path.join(channel_dir, "text")
                os.makedirs(text_dir, exist_ok=True)
                text_file = os.path.join(text_dir, "messages.json")
                text_cmd = [
                    plat.tdl_path(), "chat", "export",
                    "-c", chat_id,
                    "-o", text_file,
                    "--all", "--with-content",
                ]
                text_proc = plat.run(
                    text_cmd, capture_output=True, text=True, timeout=300,
                )
                if text_proc.returncode == 0:
                    job["filename"] = os.path.basename(text_file)
                    job["file_size"] = os.path.getsize(text_file) if os.path.exists(text_file) else 0
                # Show channel folder for "all" so user sees both media + text
                job["output_path"] = channel_dir

            job["status"] = "done"

        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = "Operation timed out (10 min limit)"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    thread = threading.Thread(target=_run_tdl_job, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/tdl/stop/<job_id>", methods=["POST"])
def tdl_stop(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    job = _tdl_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    pid = job.get("pid")
    if pid and job.get("status") == "downloading":
        plat.kill_pid(pid)
        job["status"] = "stopped"
        job["error"] = "Stopped by user"
    return jsonify({"ok": True})


@app.route("/api/tdl/status/<job_id>")
def tdl_job_status(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400

    job = _tdl_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "progress": job.get("progress", ""),
        "filename": job.get("filename"),
        "file_size": job.get("file_size"),
        "output_path": job.get("output_path"),
    })


# ---------------------------------------------------------------------------
# Split audio into ~20-minute parts
# Drops in a file, ffprobe detects its length, we split it into N parts so each
# part is roughly <= 20 minutes. Uses ffmpeg filter_complex + atrim +
# asetpts=PTS-STARTPTS (never -c copy) so Voice Memos / .m4a don't get wrong
# timecodes, leading silence, or bad durations.
# ---------------------------------------------------------------------------
_split_jobs = {}
_split_lock = threading.Lock()

SPLIT_UPLOAD = TMP_DIR
SPLIT_TARGET_SEC = 20 * 60  # aim for parts up to ~20 minutes


def _probe_duration(path):
    """Return media duration in seconds (float), or None."""
    try:
        r = plat.run(
            [plat.ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _fmt_sec(x):
    """Format seconds for ffmpeg: drop trailing zeros (1020.0 -> '1020')."""
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _split_part_count(total_sec):
    """Number of parts so each is ~<= 20 min: <=20->1, 20-40->2, 40-60->3 ..."""
    import math
    return max(1, math.ceil(total_sec / SPLIT_TARGET_SEC))


@app.route("/api/split", methods=["POST"])
def split_file():
    if not _check_rate_limit(request.remote_addr):
        return jsonify({"error": "Rate limit exceeded"}), 429

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400

    os.makedirs(SPLIT_UPLOAD, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]

    safe_name = plat.sanitize_filename(f.filename)[:80] or "audio"
    base = os.path.splitext(safe_name)[0]
    input_path = os.path.join(SPLIT_UPLOAD, f"{job_id}_in_{safe_name}")
    f.save(input_path)
    input_size = os.path.getsize(input_path)

    with _split_lock:
        _split_jobs[job_id] = {
            "status": "splitting",
            "progress": "",
            "input_name": safe_name,
            "input_size": input_size,
            "created_at": time.time(),
        }

    def _run_split():
        job = _split_jobs[job_id]
        try:
            total = _probe_duration(input_path)
            if not total or total <= 0:
                job["status"] = "error"
                job["error"] = "Не удалось определить длительность аудио"
                return

            n = _split_part_count(total)
            part = total / n
            job["duration"] = total
            job["parts"] = n

            # Build a single filter_complex command producing N outputs
            filters = []
            map_args = []
            output_names = []
            for i in range(n):
                start = i * part
                if i < n - 1:
                    end = (i + 1) * part
                    trim = f"atrim=start={_fmt_sec(start)}:end={_fmt_sec(end)}"
                else:
                    trim = f"atrim=start={_fmt_sec(start)}"  # last part: to the end
                filters.append(f"[0:a]{trim},asetpts=PTS-STARTPTS[a{i + 1}]")
                out_name = f"{base}_part_{i + 1}.m4a"
                out_path = os.path.join(DOWNLOAD_DIR, out_name)
                output_names.append(out_name)
                map_args += ["-map", f"[a{i + 1}]", "-c:a", "aac", "-b:a", "128k", out_path]

            cmd = [plat.ffmpeg_path(), "-hide_banner", "-y", "-i", input_path,
                   "-filter_complex", ";".join(filters)] + map_args

            proc = plat.popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            job["pid"] = proc.pid

            time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                m = time_re.search(clean)
                if m:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    job["pct"] = min(99, round(secs / total * 100)) if total else 0
                job["progress"] = clean

            proc.wait(timeout=1800)

            try:
                os.remove(input_path)
            except OSError:
                pass

            if proc.returncode != 0:
                job["status"] = "error"
                job["error"] = job.get("progress", "Нарезка не удалась")
                return

            files = []
            for name in output_names:
                p = os.path.join(DOWNLOAD_DIR, name)
                if os.path.exists(p):
                    files.append({"name": name, "size": os.path.getsize(p)})
            if not files:
                job["status"] = "error"
                job["error"] = "Файлы не созданы"
                return

            job["status"] = "done"
            job["pct"] = 100
            job["files"] = files
            job["output_path"] = DOWNLOAD_DIR

        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["error"] = "Превышено время ожидания (30 мин)"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=_run_split, daemon=True).start()
    return jsonify({"job_id": job_id, "input_size": input_size, "filename": safe_name})


@app.route("/api/split/status/<job_id>")
def split_status(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    job = _split_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "progress": job.get("progress", ""),
        "pct": job.get("pct", 0),
        "duration": job.get("duration"),
        "parts": job.get("parts"),
        "files": job.get("files"),
        "output_path": job.get("output_path"),
    })


@app.route("/api/split/stop/<job_id>", methods=["POST"])
def split_stop(job_id):
    if not re.match(r"^[a-f0-9]{12}$", job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    job = _split_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    pid = job.get("pid")
    if pid and job.get("status") == "splitting":
        plat.kill_pid(pid)
        job["status"] = "stopped"
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Busy running at http://{host}:{port}")
    print(f"  Downloads: {DOWNLOAD_DIR}\n")
    app.run(host=host, port=port, debug=False)
