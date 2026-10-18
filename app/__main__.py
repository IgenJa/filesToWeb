"""Start the local optimizer on 127.0.0.1 and open a browser tab."""

from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"


def find_port(host: str, start: int) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free local port was found between {start} and {start + 29}.")


def main() -> None:
    try:
        requested = int(os.environ.get("PORT", "8765"))
    except ValueError:
        requested = 8765
    if requested < 1 or requested > 65535:
        requested = 8765
    port = find_port(HOST, requested)
    url = f"http://{HOST}:{port}"
    print(f"Optimize for Web is running at {url}", flush=True)
    print("Processing stays on this computer. Close this window to stop.", flush=True)
    if os.environ.get("OPEN_BROWSER") == "1":
        timer = threading.Timer(0.7, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run("app.main:app", host=HOST, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
