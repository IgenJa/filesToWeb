# Optimize for Web

A small local tool that turns large photos and 4K videos into files you can put on a website. A 25 MB photo can become a much smaller WebP or AVIF image. A large 4K video can become an H.264 MP4 (or VP9 WebM) that starts playing before the whole file has downloaded.

Everything runs on your computer. Files are not uploaded, and the app does not ask for an account or an API key.

## What you need

- Python 3.10 or newer. On Windows, install it from [python.org](https://www.python.org/downloads/) and leave the `py` launcher enabled.
- FFmpeg on your PATH if you want to convert video. Photos still work without it.

## Install FFmpeg

Windows:

```text
winget install Gyan.FFmpeg
```

Close the terminal after installing so the new PATH is picked up, then check:

```text
ffmpeg -version
```

macOS: `brew install ffmpeg`

Linux: install `ffmpeg` from your package manager. The build needs `libx264`, AAC, and, for WebM, `libvpx` and `libopus`. The Gyan full build used by the winget command includes those encoders.

## Start the app

On Windows, double-click `start.bat`. You can also run:

```text
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The script creates `.venv`, installs the Python packages, and opens the page in your browser. The address is printed in the window, normally `http://127.0.0.1:8765`. If that port is busy, the next free port is used.

The server listens only on `127.0.0.1`. Close the window, or press Ctrl+C, to stop it.

To start it yourself:

```text
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m app
```

Set `OPEN_BROWSER=1` if you want that command to open a browser tab. Set `PORT` to change the first port it tries.

On macOS or Linux, use `python3` instead of `py -3`, and the virtualenv interpreter is `.venv/bin/python`.

## Use it

1. Click **Select files** or **Select folder**, drop files onto the page, or paste paths.
2. Set the image format, quality, and maximum width and height.
3. Set the video format, quality, encoder speed, and maximum resolution.
4. Choose an output folder, or keep the default `optimized` folder in this project.
5. Click **Optimize Files**.
6. Each file shows real progress. **Cancel** stops the encode that is running and does not start the rest.
7. The results show the original size, the new size, megabytes saved, and the percent reduction.
8. If a result is larger than the original, choose **Keep** or **Discard**.
9. Click **Open output folder**, then upload the new files to your site.
10. **Optimize another batch** clears the list. It does not delete files that were already written.

**Select files** reads the originals where they are. Drag and drop, or the upload box, first copies them into `.staging`. That copy is removed after a successful encode. For a large video, Select files is the better choice.

Originals are never overwritten. If the output name is already taken, or it would be the same path as the original, the app adds `-2`, `-3`, and so on.

A bad file is reported on its own row. The rest of the batch still runs.

## Defaults

Images:

- WebP at quality 82
- Fit inside 2560×2560
- Do not enlarge
- Keep the aspect ratio
- Apply EXIF orientation
- Remove EXIF, GPS, and camera metadata
- Keep the color profile when the encoder accepts it

Videos:

- MP4, H.264, AAC stereo at 128 kbps
- Balanced quality is CRF 24. High quality is CRF 20. Smaller file is CRF 28.
- x264 preset `medium`
- Fit inside 1920×1080
- `+faststart`, so the video can start playing before the download finishes
- WebM is optional: VP9 at CRF 28, 32, or 36, with Opus audio

Choose **Original resolution** to keep a video’s size, or **Keep original dimensions** for a photo. Neither option enlarges the file.

**Skip files that are already suitable** leaves a file alone when it is already in the format you chose, already fits the size limit, and is already reasonably compressed. A 25 MB camera photo is not skipped. A small WebP that already fits is skipped, because encoding it again would only make it look worse. Uncheck the box to force a new file. Skipped files stay where they are; nothing is copied.

To change the values the form starts with, copy `config.example.json` to `config.json`.

## Tests

```text
.\.venv\Scripts\python -m pytest
```

Video tests are skipped when `ffmpeg` is not on PATH.

## Limitations

- Animated GIF and animated WebP are rejected. A multi-page TIFF keeps the first page and says so.
- Subtitles and extra audio tracks are not copied. The first audio track becomes stereo, unless you check **Remove audio**.
- HDR video is tone-mapped when FFmpeg has the `zscale` and `tonemap` filters. If it does not, colors can shift. Output is 8-bit 4:2:0 so browsers can play it.
- HEIC, HEIF, and AVIF depend on `pillow-heif`, which is installed with the other packages. If that library cannot encode AVIF, the AVIF choice is disabled.
- **Select folder** looks through subfolders and stops after 2000 media files.
- Only one batch runs at a time. The queue lives in memory. Restarting the app clears the queue. Files that already finished stay in the output folder.
- If the app is force-closed during an encode, a `.partial` file may be left behind. Delete it. The next start removes partial files from the staging folder and the default output folder.
- This page is for you, on this computer. Do not publish it or bind it to a public address.

## Layout

- `app/` — image encoding, FFmpeg encoding, and the local server
- `static/` — the page
- `tests/` — tests for resizing, metadata, skipping, and real encodes
- `start.bat` and `start.ps1` — Windows startup

## Privacy

Photos and videos are read and written on this machine. The app does not send them to a conversion service, does not use an API key, and does not collect usage data.
