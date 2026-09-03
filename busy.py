"""
Busy — cross-platform desktop launcher.

Starts the local Flask backend and shows it in a native window:
WKWebView on macOS, WebView2 (Edge) on Windows, WebKitGTK on Linux — all
through pywebview. If no webview is available the app falls back to the
default browser, so it always starts somewhere.

    python busy.py              # native window
    python busy.py --browser    # force the browser
"""

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A windowed build (pythonw.exe / --noconsole) has no stdout at all; anything
# that prints would then blow up deep inside a library.
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is None:
        # utf-8 explicitly: the default here is the system codepage (cp1251 on
        # a Russian Windows), and printing "→" would raise UnicodeEncodeError.
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
        continue
    # A Windows console defaults to a legacy codepage (cp1252 / cp866), where
    # printing "→" or Cyrillic raises UnicodeEncodeError and kills the app.
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import platform_utils as plat  # noqa: E402  (must run before anything spawns tools)


def _ytdlp_shim():
    """Act as the yt-dlp CLI when re-executed as `Busy --ytdlp <args>`.

    A packaged build has no separate Python to run `python -m yt_dlp` with,
    so the app re-executes itself and hands control to yt-dlp's entry point.
    """
    from yt_dlp import main as ytdlp_main

    sys.exit(ytdlp_main(sys.argv[2:]))


if len(sys.argv) > 1 and sys.argv[1] == "--ytdlp":
    _ytdlp_shim()

APP_TITLE = "Busy"
WINDOW_W, WINDOW_H = 520, 680


def _unblock_bundled_files():
    """Strip the "downloaded from the internet" mark from bundled libraries.

    Windows tags every file extracted from a downloaded .zip with a
    Zone.Identifier stream, and .NET then refuses to load the assemblies that
    the native window needs (pythonnet / WebView2) — the app dies with
    "Failed to resolve Python.Runtime.Loader.Initialize". Removing the stream
    is exactly what the "Unblock" checkbox in file properties does.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not (plat.IS_WIN and base):
        return
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name.lower().endswith((".dll", ".exe")):
                try:
                    os.remove(os.path.join(root, name) + ":Zone.Identifier")
                except OSError:
                    pass  # no mark on this file — the normal case


def _app_window_browsers():
    """Chromium browsers that can show a page as a standalone window."""
    if plat.IS_WIN:
        candidates = []
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if not root:
                continue
            candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        return [p for p in candidates if os.path.isfile(p)]
    if plat.IS_MAC:
        return [p for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ) if os.path.isfile(p)]
    return [p for p in (plat.which("google-chrome"), plat.which("chromium"),
                        plat.which("microsoft-edge")) if p]


def _run_app_window(url: str) -> bool:
    """Show the app in a chromeless browser window (no toolbars, own icon).

    The fallback when the native webview cannot start: on Windows Edge is
    always present, so the user still gets something that looks like an app
    rather than a browser tab.
    """
    profile = os.path.join(plat.data_dir(), "window-profile")
    for exe in _app_window_browsers():
        try:
            proc = plat.popen([
                exe,
                f"--app={url}",
                f"--user-data-dir={profile}",
                f"--window-size={WINDOW_W},{WINDOW_H}",
                "--no-first-run",
                "--no-default-browser-check",
            ])
        except OSError:
            continue
        proc.wait()  # the app lives as long as its window
        return True
    return False


def _free_port(preferred: int = 8899) -> int:
    """Use the preferred port when it is free, otherwise any free one."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


SPLASH_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  :root { --bg:#f4f1eb; --fg:#3a3a38; --accent:#e85d2a; --muted:#9c9889; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1a1a1d; --fg:#e4e2dd; --accent:#f0743a; --muted:#777772; }
  }
  * { margin:0; }
  body { font-family:ui-monospace,'SF Mono','Cascadia Mono',Menlo,Consolas,monospace;
         background:var(--bg); color:var(--fg); height:100vh; display:flex;
         flex-direction:column; align-items:center; justify-content:center; gap:16px; }
  h1 { font-family:Georgia,'Times New Roman',serif; font-size:3.2rem; font-weight:400;
       font-style:italic; color:var(--accent); }
  .spin { width:24px; height:24px; border:3px solid var(--muted);
          border-top-color:var(--accent); border-radius:50%;
          animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  p { font-size:.75rem; color:var(--muted); letter-spacing:.04em; text-transform:uppercase; }
</style></head>
<body><h1>Busy</h1><div class="spin"></div><p>Loading...</p></body></html>
"""


def _start_server(port: int):
    """Run Flask in a background thread."""
    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from app import app

    def _run():
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _run_browser(url: str):
    print(f"\n  {APP_TITLE} -> {url}")
    print(f"  Загрузки: {os.environ.get('BUSY_DOWNLOAD_DIR') or plat.default_download_dir()}")
    print("  Закройте это окно, чтобы выйти.\n")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    force_browser = "--browser" in sys.argv or os.environ.get("BUSY_NO_GUI") == "1"

    port = _free_port(int(os.environ.get("PORT", 8899)))
    os.environ["PORT"] = str(port)
    url = f"http://127.0.0.1:{port}"

    # Windows reserves random port ranges (Hyper-V), so 8899 is not guaranteed.
    # Leave the port we actually took where anything else can find it.
    try:
        with open(os.path.join(plat.data_dir(), "port"), "w") as fh:
            fh.write(str(port))
    except OSError:
        pass

    _start_server(port)

    if force_browser:
        _wait_for_server(url)
        _run_browser(url)
        return

    _unblock_bundled_files()

    try:
        import webview  # noqa: PLC0415
    except Exception as exc:  # ImportError, or .NET failing to load on Windows
        print(f"  Нативное окно недоступно ({exc}).")
        _wait_for_server(url)
        if not _run_app_window(url):
            _run_browser(url)
        return

    window = webview.create_window(
        APP_TITLE,
        html=SPLASH_HTML,
        width=WINDOW_W,
        height=WINDOW_H,
        min_size=(400, 500),
        text_select=True,
    )

    def _load_when_ready():
        if _wait_for_server(url):
            window.load_url(url)

    threading.Thread(target=_load_when_ready, daemon=True).start()

    try:
        # private_mode=False keeps localStorage (theme, onboarding state) between runs.
        webview.start(private_mode=False)
    except Exception as exc:
        # No WebView2 runtime on Windows, no WebKitGTK on Linux, ...
        print(f"  Нативное окно недоступно ({exc}).")
        if not _run_app_window(url):
            _run_browser(url)


if __name__ == "__main__":
    main()
