# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds Busy.app on macOS and Busy.exe on Windows/Linux.

    pyinstaller Busy.spec --noconfirm
"""
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

datas = [
    ("templates", "templates"),
    ("static", "static"),
]

a = Analysis(
    ["busy.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "yt_dlp",
        "app",
        "platform_utils",
        # Flask pulls these in lazily
        "werkzeug.middleware",
        "jinja2.ext",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "test"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Busy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico" if IS_WIN else ("assets/AppIcon.icns" if IS_MAC else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Busy",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="Busy.app",
        icon="assets/AppIcon.icns",
        bundle_identifier="com.busyapp.busy",
        info_plist={
            "CFBundleName": "Busy",
            "CFBundleDisplayName": "Busy",
            "CFBundleShortVersionString": "2.0.0",
            "CFBundleVersion": "2.0.0",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
