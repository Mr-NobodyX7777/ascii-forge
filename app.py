#!/usr/bin/env python3
"""
ASCII Forge - local web app
=============================
Run this file, open http://127.0.0.1:5000. Modules in the UI:

  Image Studio      image  -> ASCII artwork (PNG / TXT)
  GIF Studio        video  -> animated ASCII GIF
  Video Studio      video  -> RGB ASCII video (MP4, with audio)
  Character Studio character sets, density, glyph size
  Colour Studio     RGB / monochrome / custom palettes, brightness, contrast...
  Export Studio     MP4, GIF, PNG frames, text, resolution, waveform, before/after
  Project Manager   saved projects, conversion history, auto-clean-up settings

Runs locally on purpose: a browser can't read pixels from arbitrary video URLs
(CORS), so the fetch-and-convert step lives in this small server instead.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse


def ensure_dependencies():
    """Make 'python3 app.py' work on a fresh machine.

    If packages are missing, create a private virtual environment (.venv) next to
    this file, install everything there (including a Deno runtime for YouTube
    links), and re-launch this script inside it. Nothing touches your system Python.
    """
    packages = {
        "cv2": "opencv-python-headless",
        "numpy": "numpy",
        "PIL": "pillow",
        "yt_dlp": "yt-dlp",
        "flask": "flask",
        "imageio_ffmpeg": "imageio-ffmpeg",   # bundled ffmpeg, used if none is installed
    }
    optional = {"deno": "deno"}               # JavaScript runtime yt-dlp needs for YouTube; installed alongside, never required
    if __name__ != "__main__":                # imported (tests, tooling): never bootstrap or relaunch
        return
    here = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(here, ".venv")
    bin_dir = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin")
    venv_py = os.path.join(bin_dir, "python.exe" if os.name == "nt" else "python")
    missing = [pip for mod, pip in packages.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    if os.environ.get("FORGE_BOOTSTRAPPED"):
        sys.exit("Dependencies are still missing after install: " + ", ".join(missing))

    def pip_install(py, pkgs, extra=()):
        subprocess.check_call([py, "-m", "pip", "install", "--quiet", *extra, *pkgs])

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"First run: setting up (installing {', '.join(missing)}). This can take a minute...", file=sys.stderr)
    try:
        if in_venv:                                   # already inside someone's venv: just install here
            pip_install(sys.executable, missing)
            return
        if not os.path.exists(venv_py):
            subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        pip_install(venv_py, list(packages.values()) + list(optional.values()))
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Private environment setup failed ({exc}); trying a direct install instead...", file=sys.stderr)
        for extra in ([], ["--user"], ["--break-system-packages"]):
            try:
                pip_install(sys.executable, missing, extra)
                return
            except subprocess.CalledProcessError:
                continue
        sys.exit("Could not auto-install dependencies. Please run manually:\n"
                 f"    pip install {' '.join(list(packages.values()) + list(optional.values()))}\n"
                 "(on Debian/Ubuntu you may first need: sudo apt install python3-venv)")
    env = dict(os.environ, FORGE_BOOTSTRAPPED="1", PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))
    sys.exit(subprocess.call([venv_py, os.path.abspath(__file__), *sys.argv[1:]], env=env))


ensure_dependencies()

import cv2
import numpy as np
from flask import Flask, abort, jsonify, render_template, request, send_from_directory, Response
from PIL import Image

import engine
from store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
PORT = int(os.environ.get("PORT", "5000"))

store = Store(os.path.join(DATA_DIR, "forge.db"), GENERATED_DIR, UPLOAD_DIR, THUMB_DIR)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024        # 1 GB uploads

JOBS = {}                         # live progress of running jobs only
JOBS_LOCK = threading.Lock()
RUN_SLOTS = threading.Semaphore(2)   # at most 2 conversions at once

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v", "gif"}
MAX_GIF_FRAMES = 400
MAX_ZIP_FRAMES = 1000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ------------------------------------------------------------- security ----
# The server reads local files and fetches URLs on request, so a web page you
# happen to visit must not be able to drive it. Block foreign Host headers
# (DNS rebinding) and require a custom header on every state-changing request
# (a cross-site page can't add one without a CORS pre-flight, which we never grant).

@app.before_request
def guard():
    if request.host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in {h.strip("[]") for h in LOCAL_HOSTS}:
            abort(403)
        if request.headers.get("X-Requested-With") != "ascii-forge":
            abort(403)


@app.after_request
def no_sniff(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------- source input ---

def _clean(msg):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).strip()


def upload_path(upload_id):
    if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{2,5}", upload_id or ""):
        return None
    path = os.path.join(UPLOAD_DIR, upload_id)
    return path if os.path.isfile(path) else None


def is_url(s):
    return bool(re.match(r"^https?://", s, re.I))


def local_source(p):
    """Path of an uploaded file or an existing local path; None for URLs."""
    if p["upload_id"]:
        path = upload_path(p["upload_id"])
        if not path:
            raise RuntimeError("the uploaded file has expired - please upload it again")
        return path
    s = p["source"].strip().strip('"').strip("'")
    if s and not is_url(s):
        path = os.path.expanduser(s)
        if os.path.isfile(path):
            return path
        raise RuntimeError("file not found: " + s)
    return None


def download_video(url, out_dir, max_seconds, log):
    import yt_dlp
    base = {"outtmpl": os.path.join(out_dir, "input.%(ext)s"), "merge_output_format": "mp4",
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "color": {"stdout": "no_color", "stderr": "no_color"}}
    fmt = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    attempts = []
    if max_seconds:
        try:
            from yt_dlp.utils import download_range_func
            attempts.append({"format": fmt, "download_ranges": download_range_func(None, [(0, max_seconds)]),
                             "force_keyframes_at_cuts": True})
        except Exception:
            pass
    attempts += [{"format": fmt}, {"format": "best"}]
    log("downloading video...")
    last = None
    for extra in attempts:
        for f in os.listdir(out_dir):
            if f.startswith("input"):
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                ydl.download([url])
        except Exception as exc:
            last = exc
            continue
        files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                 if f.startswith("input") and not f.endswith((".part", ".ytdl", ".temp"))]
        if files:
            return max(files, key=os.path.getsize)
    raise RuntimeError("download failed: " + _clean(last or "no file produced")[:500] +
                       "\nTips: update yt-dlp (`pip install -U yt-dlp`), make sure ffmpeg is on your PATH, "
                       "and for YouTube install Deno or Node.js.")


def fetch_image(url, out_dir):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ascii-forge"})
    path = os.path.join(out_dir, "input.img")
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        read = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            read += len(chunk)
            if read > 60 * 1024 * 1024:
                raise RuntimeError("image is larger than 60 MB")
            f.write(chunk)
    return path


def read_image(path):
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if img is None:
        # animated / odd formats: let Pillow try
        try:
            with Image.open(path) as im:
                img = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception:
            raise RuntimeError("could not read that image")
    return img


def extract_audio(video_path, audio_path, max_seconds=None):
    cmd = ["ffmpeg", "-y", "-i", video_path]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", audio_path]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 44


def check_ffmpeg():
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bin_dir = os.path.join(BASE_DIR, "_ffmpeg_bin")
        os.makedirs(bin_dir, exist_ok=True)
        target = os.path.join(bin_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(target):
            print("No system ffmpeg found - using the bundled one.", file=sys.stderr)
            shutil.copy2(exe, target)
            os.chmod(target, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception as exc:
        sys.exit(f"Could not find or set up ffmpeg ({exc}).\n"
                 "Install it manually: Windows `winget install Gyan.FFmpeg`, "
                 "macOS `brew install ffmpeg`, Linux `sudo apt install ffmpeg`.")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg setup failed - please install ffmpeg manually and add it to PATH.")


# ------------------------------------------------------------------ jobs ---

class Cancelled(Exception):
    pass


def _file_entry(job_id, name, label, kind):
    path = os.path.join(GENERATED_DIR, job_id, name)
    return {"name": name, "label": label, "kind": kind, "url": f"/media/{job_id}/{name}",
            "size": os.path.getsize(path)}


def _png_bytes(img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("could not encode PNG")
    return buf.tobytes()


def _gif_frame(img_bgr, max_w):
    h, w = img_bgr.shape[:2]
    if w > max_w:
        img_bgr = cv2.resize(img_bgr, (max_w, max(1, int(round(h * max_w / w)))), interpolation=cv2.INTER_AREA)
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    return pil.convert("P", palette=Image.ADAPTIVE, colors=256)


def run_job(job_id, p):
    def log(message, status=None, progress=None):
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["message"] = message
            if status:
                job["status"] = status
            if progress is not None:
                job["progress"] = progress

    def check_cancel():
        with JOBS_LOCK:
            if JOBS[job_id].get("cancel"):
                raise Cancelled()

    out_dir = os.path.join(GENERATED_DIR, job_id)
    tmp_dir = tempfile.mkdtemp(prefix="ascii_forge_")
    files, notes = [], []
    ff = None
    try:
        RUN_SLOTS.acquire()
        try:
            check_cancel()
            os.makedirs(out_dir, exist_ok=True)
            kind, outputs = p["kind"], p["outputs"]
            log("reading source...", status="starting")

            path = local_source(p)
            if path is None:
                url = p["source"].strip()
                path = fetch_image(url, tmp_dir) if kind == "image" else download_video(url, tmp_dir, p["max_seconds"], log)

            # ---------------- image ----------------
            if kind == "image":
                frame = read_image(path)
                h, w = frame.shape[:2]
                log("printing...", status="rendering", progress=[0, 1])
                r = engine.Renderer(p, w, h)
                if r.dropped:
                    notes.append("no glyph in the font for: " + "".join(r.dropped))
                plain = dict(p, compare="none")
                rp = engine.Renderer(plain, w, h) if p["compare"] != "none" else r
                after, text = rp.render(frame, want_text=True)
                before = cv2.resize(frame, (rp.out_w, rp.rows * rp.ch), interpolation=cv2.INTER_AREA)
                pad = np.empty((rp.out_h, rp.out_w, 3), np.uint8)
                pad[:] = rp.bg.astype(np.uint8)
                pad[:before.shape[0]] = before
                after_png = _png_bytes(after)
                for name, data in (("slider_after.png", after_png), ("slider_before.png", _png_bytes(pad))):
                    with open(os.path.join(out_dir, name), "wb") as f:     # used by the before/after slider
                        f.write(data)
                if "png" in outputs:
                    with open(os.path.join(out_dir, "ascii.png"), "wb") as f:
                        f.write(after_png)
                    files.append(_file_entry(job_id, "ascii.png", "ASCII image (PNG)", "png"))
                if p["compare"] != "none":
                    cmp_img, _ = r.render(frame)
                    with open(os.path.join(out_dir, "compare.png"), "wb") as f:
                        f.write(_png_bytes(cmp_img))
                    files.append(_file_entry(job_id, "compare.png", "Before / after (PNG)", "png"))
                if "txt" in outputs:
                    with open(os.path.join(out_dir, "ascii.txt"), "w", encoding="utf-8") as f:
                        f.write(text)
                    files.append(_file_entry(job_id, "ascii.txt", "Plain text (TXT)", "txt"))
                _thumb(job_id, after)
                result_extra = {"slider": {"before": f"/media/{job_id}/slider_before.png",
                                           "after": f"/media/{job_id}/slider_after.png"}}
            else:
                result_extra = _run_video(job_id, p, path, tmp_dir, out_dir, files, notes, log, check_cancel)
        finally:
            RUN_SLOTS.release()

        for e in files:                      # sizes may have changed after final writes
            e["size"] = os.path.getsize(os.path.join(out_dir, e["name"]))
        msg = "done." + ("  " + "; ".join(notes) if notes else "")
        store.finish_job(job_id, "done", msg, files)
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", message=msg, files=files, **result_extra)
        store.cleanup()
    except Cancelled:
        shutil.rmtree(out_dir, ignore_errors=True)
        store.finish_job(job_id, "cancelled", "cancelled.")
        with JOBS_LOCK:
            JOBS[job_id].update(status="cancelled", message="cancelled.")
    except Exception as exc:
        if not isinstance(exc, (ValueError, RuntimeError)):     # expected user errors need no stack trace
            import traceback
            traceback.print_exc()
        shutil.rmtree(out_dir, ignore_errors=True)
        store.finish_job(job_id, "error", _clean(exc))
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", message=_clean(exc))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        threading.Timer(3600, lambda: _forget(job_id)).start()


def _forget(job_id):
    with JOBS_LOCK:
        JOBS.pop(job_id, None)


def _thumb(job_id, img_bgr):
    h, w = img_bgr.shape[:2]
    tw = 320
    th = max(1, int(round(h * tw / w)))
    small = cv2.resize(img_bgr, (tw, th), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if ok:
        with open(os.path.join(THUMB_DIR, f"{job_id}.jpg"), "wb") as f:
            f.write(buf.tobytes())


def _run_video(job_id, p, path, tmp_dir, out_dir, files, notes, log, check_cancel):
    outputs, max_seconds = p["outputs"], p["max_seconds"]
    log("reading video info...", status="probing")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("could not open the video")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps < 1 or fps > 240:
        fps = 24.0
    vid_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16)
    vid_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9)
    r = engine.Renderer(p, vid_w, vid_h)
    if r.dropped:
        notes.append("no glyph in the font for: " + "".join(r.dropped))
    max_frames = int(max_seconds * fps) if max_seconds else None
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    total = min(max_frames, n_frames) if (max_frames and n_frames) else (max_frames or n_frames or 1)

    want_mp4 = "mp4" in outputs
    want_audio = want_mp4 or p["waveform"]
    audio_path, has_audio, env = os.path.join(tmp_dir, "audio.wav"), False, None
    if want_audio:
        log("pulling audio track...", status="extracting_audio")
        has_audio = extract_audio(path, audio_path, max_seconds)
        if p["waveform"]:
            if has_audio:
                env = engine.AudioEnvelope(audio_path)
            else:
                notes.append("no audio track - waveform strip left flat")

    gif_step = max(1, int(round(fps / p["gif_fps"])))
    gif_frames = []
    zf_png = zf_txt = None
    if "frames_png" in outputs:
        zf_png = zipfile.ZipFile(os.path.join(out_dir, "frames_png.zip"), "w", zipfile.ZIP_STORED)
    if "frames_txt" in outputs:
        zf_txt = zipfile.ZipFile(os.path.join(out_dir, "frames_txt.zip"), "w", zipfile.ZIP_DEFLATED)
    want_text = zf_txt is not None

    ff_err = None
    if want_mp4:
        mp4_path = os.path.join(out_dir, "ascii.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{r.final_w}x{r.out_h}", "-r", f"{fps:.5f}", "-i", "-"]
        if has_audio:
            cmd += ["-i", audio_path]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
        cmd += ["-movflags", "+faststart", mp4_path]
        ff_err = open(os.path.join(tmp_dir, "ffmpeg.log"), "wb")
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=ff_err)

    thumb_done = False
    fi = png_n = txt_n = 0
    log("printing frames...", status="rendering", progress=[0, total])
    try:
        while not (max_frames and fi >= max_frames):
            check_cancel()
            ok, frame = cap.read()
            if not ok:
                break
            amps = env.amps_at(fi / fps, r.cols) if env is not None else None
            img, text = r.render(frame, fi, amps=amps, want_text=want_text)
            if not thumb_done and fi >= min(total // 10, 30):
                _thumb(job_id, img)
                thumb_done = True
            if ff is not None:
                try:
                    ff.stdin.write(img.tobytes())
                except (BrokenPipeError, OSError):
                    ff_err.flush()
                    raise RuntimeError("video encoding failed: " + _ff_tail(tmp_dir))
            if "gif" in outputs and fi % gif_step == 0:
                if len(gif_frames) < MAX_GIF_FRAMES:
                    gif_frames.append(_gif_frame(img, p["gif_width"]))
                elif "gif_trunc" not in notes:
                    notes.append(f"GIF stopped at {MAX_GIF_FRAMES} frames (lower the length or GIF fps)")
                    notes.append("gif_trunc")
            if fi % p["frame_step"] == 0:
                if zf_png is not None and png_n < MAX_ZIP_FRAMES:
                    zf_png.writestr(f"frame_{fi:06d}.png", _png_bytes(img))
                    png_n += 1
                if zf_txt is not None and txt_n < MAX_ZIP_FRAMES:
                    zf_txt.writestr(f"frame_{fi:06d}.txt", text)
                    txt_n += 1
            fi += 1
            if fi % 5 == 0 or fi == total:
                log(f"printing frame {fi}/{max(total, fi)}...", progress=[fi, max(total, fi)])
    except BaseException:
        if ff is not None:
            ff.kill()
            ff.wait()
        raise
    finally:
        cap.release()
        if ff_err is not None and ff is not None and ff.poll() is not None:
            ff_err.close()
        if zf_png:
            zf_png.close()
        if zf_txt:
            zf_txt.close()

    if fi == 0:
        raise RuntimeError("no frames were rendered - the video may be empty or unreadable")
    if "gif_trunc" in notes:
        notes.remove("gif_trunc")
    if (zf_png and png_n >= MAX_ZIP_FRAMES) or (zf_txt and txt_n >= MAX_ZIP_FRAMES):
        notes.append(f"frame export stopped at {MAX_ZIP_FRAMES} frames (raise 'every Nth frame')")

    if ff is not None:
        log("encoding and attaching audio...", status="muxing")
        ff.stdin.close()
        if ff.wait() != 0:
            raise RuntimeError("video encoding failed: " + _ff_tail(tmp_dir))
        ff_err.close()
        files.append(_file_entry(job_id, "ascii.mp4", "Video (MP4)", "mp4"))
    if "gif" in outputs and gif_frames:
        log("building GIF...", status="muxing")
        gif_path = os.path.join(out_dir, "ascii.gif")
        gif_frames[0].save(gif_path, save_all=True, append_images=gif_frames[1:], loop=0,
                           duration=int(round(1000 / (fps / gif_step))), disposal=2)
        files.append(_file_entry(job_id, "ascii.gif", "Animated GIF", "gif"))
    if zf_png:
        files.append(_file_entry(job_id, "frames_png.zip", f"PNG frames ({png_n}, zip)", "zip"))
    if zf_txt:
        files.append(_file_entry(job_id, "frames_txt.zip", f"Text frames ({txt_n}, zip)", "zip"))
    return {}


def _ff_tail(tmp_dir):
    try:
        with open(os.path.join(tmp_dir, "ffmpeg.log"), "rb") as f:
            return f.read()[-400:].decode("utf-8", "replace")
    except OSError:
        return "(no ffmpeg output)"


# ---------------------------------------------------------------- routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/presets")
def presets():
    return jsonify(styles=engine.STYLES, palettes=engine.PALETTES, ramps=engine.RAMPS,
                   outputs=engine.ALLOWED_OUTPUTS)


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file received"), 400
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        return jsonify(error="unsupported file type: ." + ext), 400
    upload_id = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(UPLOAD_DIR, upload_id))
    return jsonify(upload_id=upload_id, name=f.filename[:200], is_image=ext in IMAGE_EXTS)


@app.post("/convert")
def convert():
    try:
        p = engine.normalize_params(request.get_json(silent=True))
    except Exception as exc:
        return jsonify(error=str(exc)), 400
    if not p["upload_id"] and not p["source"].strip():
        return jsonify(error="please give a file, link or local path"), 400
    if p["upload_id"] and not upload_path(p["upload_id"]):
        return jsonify(error="the uploaded file is gone - please upload it again"), 400
    src = p["source"].strip().strip('"').strip("'")
    if not p["upload_id"] and not is_url(src) and not os.path.isfile(os.path.expanduser(src)):
        return jsonify(error="that is neither an http(s) link nor an existing file"), 400
    job_id = uuid.uuid4().hex[:12]
    label = p["source_name"] or (urlparse(src).netloc + urlparse(src).path[-30:] if is_url(src) else os.path.basename(src))
    store.add_job(job_id, p["kind"], label or "untitled", p)
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "message": "queued...", "progress": None}
    threading.Thread(target=run_job, args=(job_id, p), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            return jsonify(dict(job))
    row = store.get_job(job_id)
    if not row:
        return jsonify(error="unknown job"), 404
    return jsonify(status=row["status"], message=row["message"], files=row["files"], expired=row["expired"])


@app.post("/cancel/<job_id>")
def cancel(job_id):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["cancel"] = True
    return jsonify(ok=True)


@app.post("/preview")
def preview():
    p = engine.normalize_params(request.get_json(silent=True))
    try:
        path = local_source(p)
        if path is None:
            return jsonify(error="Preview needs an uploaded file or a local path (links are only fetched when you print)."), 400
        frame = None
        if p["kind"] == "image":
            frame = read_image(path)
        else:
            cap = cv2.VideoCapture(path)
            try:
                count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if count > 10:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * 0.2))
                ok, frame = cap.read()
                if not ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                if not ok:
                    frame = None
            finally:
                cap.release()
            if frame is None:
                frame = read_image(path)
        h, w = frame.shape[:2]
        p["width"] = min(p["width"], 1280)
        r = engine.Renderer(p, w, h)
        demo = (np.abs(np.sin(np.linspace(0, 9, r.cols))) * 0.8).astype(np.float32) if r.wf_rows else None
        img, _ = r.render(frame, 0, amps=demo)
        return Response(_png_bytes(img), mimetype="image/png")
    except (RuntimeError, ValueError) as exc:
        return jsonify(error=_clean(exc)), 400


@app.get("/media/<job_id>/<path:filename>")
def media(job_id, filename):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        abort(404)
    return send_from_directory(os.path.join(GENERATED_DIR, job_id), filename)


@app.get("/thumb/<job_id>")
def thumb(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        abort(404)
    return send_from_directory(THUMB_DIR, f"{job_id}.jpg")


# ---- Project Manager API ----

@app.get("/api/history")
def history():
    jobs = store.list_jobs()
    for j in jobs:
        j["has_thumb"] = os.path.exists(os.path.join(THUMB_DIR, f"{j['id']}.jpg"))
    st = store.get_settings()
    return jsonify(jobs=jobs, settings=st, storage_used=store.storage_used())


@app.post("/api/history/<job_id>/pin")
def pin(job_id):
    pinned = bool((request.get_json(silent=True) or {}).get("pinned", True))
    job = store.get_job(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    if job["expired"]:
        return jsonify(error="the files of this conversion were already cleaned up"), 400
    store.set_pinned(job_id, pinned)
    return jsonify(ok=True)


@app.delete("/api/history/<job_id>")
def delete_history(job_id):
    with JOBS_LOCK:
        if JOBS.get(job_id, {}).get("status") not in (None, "done", "error", "cancelled"):
            return jsonify(error="job is still running - cancel it first"), 400
    store.delete_job(job_id)
    return jsonify(ok=True)


@app.get("/api/projects")
def projects():
    return jsonify(projects=store.list_projects())


@app.post("/api/projects")
def save_project():
    data = request.get_json(silent=True) or {}
    p = engine.normalize_params(data.get("params"))
    p.update(upload_id="", source=p["source"] if is_url(p["source"].strip()) else "")   # never store local paths/uploads
    name = (data.get("name") or "").strip() or "untitled project"
    pid = store.add_project(name, p["kind"], p)
    return jsonify(id=pid)


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    store.delete_project(pid)
    return jsonify(ok=True)


@app.post("/api/settings")
def settings():
    data = request.get_json(silent=True) or {}
    try:
        st = store.set_settings(data.get("retention_hours"), data.get("max_storage_gb"))
    except (TypeError, ValueError):
        return jsonify(error="invalid number"), 400
    return jsonify(settings=st, cleaned=store.cleanup())


@app.post("/api/cleanup")
def cleanup_now():
    return jsonify(cleaned=store.cleanup(), storage_used=store.storage_used())


# ------------------------------------------------------------ background ---

def _cleanup_loop():
    while True:
        time.sleep(600)
        try:
            store.cleanup()
        except Exception:
            import traceback
            traceback.print_exc()


def start_background():
    store.cleanup()                       # also once at startup
    threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    check_ffmpeg()
    start_background()
    print(f"Open http://127.0.0.1:{PORT} in your browser.", file=sys.stderr)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
