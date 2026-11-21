# ASCII Forge (local web app)

[![CI](https://github.com/Mr-NobodyX7777/ascii-forge/actions/ci.yml/badge.svg)](https://github.com/Mr-NobodyX7777/ascii-forge/actions/ci.yml)

Turn images, GIFs and videos into ASCII art in your browser. It runs as a small
web server on your own machine, so nothing is uploaded anywhere.

## Run it

```
python3 app.py
```

Then open **http://127.0.0.1:5000**. That's it.

On the first run it sets itself up: it creates a private `.venv` folder next to `app.py`,
installs everything it needs there (OpenCV, Pillow, Flask, yt-dlp, a bundled ffmpeg and a
Deno runtime for YouTube links) and restarts itself inside it. Your system Python is not
touched. It needs an internet connection that one time. Later runs start immediately.

Only Python 3.9+ has to be installed already. (On Debian/Ubuntu, if setup complains, run
`sudo apt install python3-venv` once.) If you already have ffmpeg it is used instead of the bundled one.

## Modules

| Module | What it does |
|---|---|
| **Image Studio** | Image -> ASCII artwork. Exports PNG, TXT, and a before/after PNG. Result page has a before/after slider. |
| **GIF Studio** | Video or GIF -> animated ASCII GIF (fps and width controls). |
| **Video Studio** | Video -> ASCII MP4 with the original audio. Can also export a GIF, PNG frames (zip) and text frames (zip). |
| **Character Studio** | Styles (classic monochrome, full RGB, green terminal, matrix, custom), character ramps, your own character set (optionally sorted by ink density), character density (columns), character size, bold, invert. |
| **Colour Studio** | RGB / monochrome / custom palette (2-6 colour gradient, presets included), background colour, brightness, contrast, saturation. |
| **Export Studio** | Output resolution (640-2560 px wide), formats, audio waveform strip, before/after comparison (side by side or split), GIF and frame-export settings. |
| **Project Manager** | Saved projects (settings), conversion history with thumbnails, pin/delete, re-use settings, storage and auto-clean-up settings. |

Use **preview** to see one frame with your settings before printing (works with uploaded files and local paths).

## Automatic clean-up

Generated files no longer pile up:

- Outputs are deleted **24 hours** after a conversion finishes (change it in Project Manager; 0 = never).
- A **storage cap** (default 5 GB) removes the oldest outputs first.
- **Pinned** conversions are never deleted.
- Stray output folders and uploads older than 12 hours are removed.
- History rows and small thumbnails stay after files expire, so you can re-use the settings.
- Runs at startup, every 10 minutes, after each finished job, and on "clean up now".

Data lives in `generated/` (outputs) and `data/` (SQLite database, uploads, thumbnails).

## Security

This is a local tool. The server only answers requests addressed to `127.0.0.1`/`localhost`
and rejects state-changing requests that lack its custom header or come from another origin,
so a web page you visit cannot drive it. Don't expose it to a network.

## Notes

- Glyphs are drawn with the bundled IBM Plex Mono (SIL OFL, `static/fonts/`). Characters the font lacks (e.g. CJK, katakana) are skipped and reported.
- At most 2 conversions run at once; others wait in the queue.
- GIFs are capped at 400 frames and frame zips at 1000 frames.
- Anything `yt-dlp` supports works as a video link.
