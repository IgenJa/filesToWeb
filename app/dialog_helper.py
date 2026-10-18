"""Open a native file dialog in its own process.

Tkinter needs the main thread of a process. Running it here keeps the web
server free to keep reporting progress.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "files"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    media = (
        ("Images and videos", "*.jpg *.jpeg *.png *.webp *.tif *.tiff *.avif *.heic *.heif "
         "*.gif *.bmp *.mp4 *.m4v *.mov *.mkv *.webm *.avi *.mpeg *.mpg *.wmv *.flv *.ts *.mts"),
        ("All files", "*.*"),
    )
    if mode == "files":
        selected = filedialog.askopenfilenames(title="Select images and videos", filetypes=media)
        paths = list(selected)
    elif mode == "folder":
        selected = filedialog.askdirectory(title="Select a folder of images and videos")
        paths = [selected] if selected else []
    elif mode == "output":
        selected = filedialog.askdirectory(title="Select an output folder")
        paths = [selected] if selected else []
    else:
        paths = []

    root.destroy()
    print(json.dumps(paths))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
