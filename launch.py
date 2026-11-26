"""Desktop launcher: starts the TerraTruth server and opens it in its own app window.

Double-click TerraTruth.bat (Windows) or run:  python launch.py
Closing this process (or the console window) stops the server.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = os.getenv("TT_HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
URL = f"http://{HOST}:{PORT}"


def healthy(timeout=1.5):
    try:
        with urllib.request.urlopen(f"{URL}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def open_window():
    """Prefer a chromeless app window (Edge/Chrome --app); fall back to the default browser."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        shutil.which("microsoft-edge") or "", shutil.which("google-chrome") or "", shutil.which("chromium") or "",
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1400,900"])
            return
    webbrowser.open(URL)


def wait_then_open():
    for _ in range(240):  # first start may compile the contract / download solc
        if healthy():
            open_window()
            return
        time.sleep(0.5)
    print("Server did not become ready; check the console output above.")


def main():
    if healthy():
        print(f"TerraTruth already running at {URL}; opening window.")
        open_window()
        return
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Starting TerraTruth Oracle at {URL} ... (close this window to stop)")
    threading.Thread(target=wait_then_open, daemon=True).start()
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
