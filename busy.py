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
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream, None) is None:
        setattr(sys, _stream, open(os.devnull, "w"))

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
    print(f"\n  {APP_TITLE} → {url}")
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

    _start_server(port)

    if force_browser:
        _wait_for_server(url)
        _run_browser(url)
        return

    try:
        import webview  # noqa: PLC0415
    except ImportError:
        _wait_for_server(url)
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
        print(f"  Нативное окно недоступно ({exc}). Открываю в браузере.")
        _run_browser(url)


if __name__ == "__main__":
    main()
