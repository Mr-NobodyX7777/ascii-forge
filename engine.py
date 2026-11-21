"""
Rendering engine for the ASCII Forge: styles, colour handling, glyph masks,
audio waveform strip and before/after compositing. No Flask in here.
"""

import os
import re
import wave

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "static", "fonts")

# ---------------------------------------------------------------- presets --

RAMPS = {
    "standard": " .:-=+*#%@",
    "dense": " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "blocks": " ░▒▓█",
    "minimal": " .:+#",
    "binary": " 01",
}

MATRIX_CHARS = " 0123456789:.=*+-<>|ZXYWVTHEMAC"

STYLES = {
    "classic": dict(label="Classic monochrome", charset=RAMPS["standard"], ramp="standard",
                    color_mode="mono", fg="#e6e6e6", bg="#000000", random_chars=False, invert=False),
    "rgb": dict(label="Full RGB", charset=RAMPS["standard"], ramp="standard",
                color_mode="rgb", bg="#000000", random_chars=False, invert=False),
    "terminal": dict(label="Green terminal", charset=RAMPS["standard"], ramp="standard",
                     color_mode="palette", palette=["#031a0a", "#1fa84f", "#8dffa8"], bg="#020d05",
                     random_chars=False, invert=False),
    "matrix": dict(label="Matrix", charset=MATRIX_CHARS, ramp="custom",
                   color_mode="palette", palette=["#002b0c", "#00b83f", "#d2ffdc"], bg="#000000",
                   random_chars=True, invert=False),
    "custom": dict(label="Custom character set", charset=" .oO0@", ramp="custom",
                   color_mode="rgb", bg="#000000", random_chars=False, invert=False),
}

PALETTES = {
    "Green phosphor": ["#031a0a", "#1fa84f", "#8dffa8"],
    "Amber CRT": ["#1a0f00", "#ff9d00", "#ffe6a8"],
    "Cyberpunk": ["#0d0221", "#ff2a6d", "#05d9e8"],
    "Sunset": ["#1b0b3a", "#d1345b", "#f7b267", "#fff3c4"],
    "Ice": ["#03111f", "#1b6ca8", "#9ad1ff", "#ffffff"],
    "Game Boy": ["#0f380f", "#306230", "#8bac0f", "#9bbc0f"],
    "Paper & ink": ["#f4efe6", "#8a7f72", "#1c1b1a"],
}

ALLOWED_OUTPUTS = {
    "image": ["png", "txt"],
    "gif": ["gif", "frames_png", "frames_txt"],
    "video": ["mp4", "gif", "frames_png", "frames_txt"],
}
DEFAULT_OUTPUTS = {"image": ["png", "txt"], "gif": ["gif"], "video": ["mp4"]}

MAX_OUTPUT_PIXELS = 12_000_000


# ------------------------------------------------------------ parameters ---

def _num(v, lo, hi, default, integer=False):
    try:
        v = float(v)
        if v != v:
            raise ValueError
    except (TypeError, ValueError):
        v = default
    v = min(max(v, lo), hi)
    return int(round(v)) if integer else v


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex(v, default):
    return v if isinstance(v, str) and _HEX.match(v) else default


def normalize_params(raw):
    """Validate + clamp everything that came from the browser."""
    raw = raw or {}
    kind = raw.get("kind") if raw.get("kind") in ALLOWED_OUTPUTS else "video"
    style = raw.get("style") if raw.get("style") in STYLES else "rgb"

    charset = raw.get("charset")
    charset = charset if isinstance(charset, str) and charset else RAMPS["standard"]
    charset = charset[:64]

    mode = raw.get("color_mode") if raw.get("color_mode") in ("rgb", "mono", "palette") else "rgb"
    pal = raw.get("palette")
    if not (isinstance(pal, list) and 2 <= len(pal) <= 6):
        pal = STYLES["terminal"]["palette"]
    pal = [_hex(c, "#808080") for c in pal]

    outputs = [o for o in (raw.get("outputs") or []) if o in ALLOWED_OUTPUTS[kind]]
    if not outputs:
        outputs = list(DEFAULT_OUTPUTS[kind])

    max_s = raw.get("max_seconds")
    try:
        max_s = float(max_s) if max_s not in (None, "") else None
    except (TypeError, ValueError):
        max_s = None
    if max_s is not None and max_s <= 0:
        max_s = None

    return dict(
        kind=kind,
        style=style,
        charset=charset,
        sort_chars=bool(raw.get("sort_chars")),
        random_chars=bool(raw.get("random_chars")),
        invert=bool(raw.get("invert")),
        bold=bool(raw.get("bold")),
        columns=_num(raw.get("columns"), 30, 480, 160, True),
        glyph_scale=_num(raw.get("glyph_scale"), 0.5, 1.5, 1.0),
        color_mode=mode,
        fg=_hex(raw.get("fg"), "#e6e6e6"),
        palette=pal,
        bg=_hex(raw.get("bg"), "#000000"),
        brightness=_num(raw.get("brightness"), -100, 100, 0),
        contrast=_num(raw.get("contrast"), 0.3, 3.0, 1.0),
        saturation=_num(raw.get("saturation"), 0.0, 3.0, 1.0),
        width=_num(raw.get("width"), 320, 2560, 1280, True),
        compare=raw.get("compare") if raw.get("compare") in ("none", "side", "split") else "none",
        waveform=bool(raw.get("waveform")) and kind == "video",
        waveform_rows=_num(raw.get("waveform_rows"), 2, 20, 6, True),
        outputs=outputs,
        gif_fps=_num(raw.get("gif_fps"), 2, 30, 12),
        gif_width=_num(raw.get("gif_width"), 160, 1280, 640, True),
        frame_step=_num(raw.get("frame_step"), 1, 1000, 10, True),
        max_seconds=max_s,
        source=str(raw.get("source") or "")[:2000],
        upload_id=str(raw.get("upload_id") or "")[:80],
        source_name=str(raw.get("source_name") or "")[:200],
    )


def hex_bgr(h):
    h = h.lstrip("#")
    return np.array([int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)], dtype=np.float32)


# ----------------------------------------------------------------- glyphs --

def _font(bold, size):
    name = "IBMPlexMono-Bold.ttf" if bold else "IBMPlexMono-Regular.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def build_masks(charset, cw, ch, scale, bold, sort_chars):
    """Render every character once into an alpha mask of shape (ch, cw).
    Returns (chars, masks[n, ch, cw], dropped_chars)."""
    font = _font(bold, max(3, int(round(ch * 0.8 * scale))))

    def draw(c):
        im = Image.new("L", (cw * 3, ch * 3), 0)
        ImageDraw.Draw(im).text((cw * 1.5, ch * 1.5), c, font=font, fill=255, anchor="mm")
        return np.asarray(im.crop((cw, ch, cw * 2, ch * 2)), dtype=np.float32) / 255.0

    notdef = draw("\U0010ffff")           # the font's "missing glyph" box
    chars, masks, dropped, seen = [], [], [], set()
    for c in charset:
        if c in seen or c in "\r\n\t":
            continue
        seen.add(c)
        m = draw(c) if c != " " else np.zeros((ch, cw), np.float32)
        if c != " " and np.array_equal(m, notdef):
            dropped.append(c)             # font has no glyph for it
            continue
        chars.append(c)
        masks.append(m)
    if len(chars) < 2:
        raise ValueError("Need at least 2 usable characters in the character set.")
    masks = np.stack(masks)
    if sort_chars:
        order = np.argsort(masks.reshape(len(chars), -1).mean(axis=1), kind="stable")
        chars = [chars[i] for i in order]
        masks = masks[order]
    peak = masks.max()
    if peak > 0:
        masks = masks / peak              # tiny cells lose peak brightness to anti-aliasing
    return chars, masks.astype(np.float32), dropped


# ---------------------------------------------------------------- layout ---

def layout(p, src_w, src_h):
    cols = p["columns"]
    cell_w = max(2, int(round(p["width"] / cols)))
    cell_w += cell_w % 2                  # even sizes keep H.264 happy
    cell_h = cell_w * 2
    rows = max(1, int(round(cols * cell_w * src_h / (src_w * cell_h))))
    wf = p["waveform_rows"] if p["waveform"] else 0
    out_w = cols * cell_w
    out_h = (rows + wf) * cell_h
    final_w = out_w * 2 if p["compare"] == "side" else out_w
    if final_w * out_h > MAX_OUTPUT_PIXELS:
        raise ValueError(f"Output would be {final_w}x{out_h}px - too large. Lower the resolution or density.")
    return cols, rows, cell_w, cell_h, wf, out_w, out_h


# --------------------------------------------------------------- renderer --

_LUM = np.array([0.114, 0.587, 0.299], dtype=np.float32)   # BGR weights


class Renderer:
    def __init__(self, p, src_w, src_h):
        self.p = p
        (self.cols, self.rows, self.cw, self.ch,
         self.wf_rows, self.out_w, self.out_h) = layout(p, src_w, src_h)
        self.chars, self.masks, self.dropped = build_masks(
            p["charset"], self.cw, self.ch, p["glyph_scale"], p["bold"], p["sort_chars"])
        self.chars_arr = np.array(self.chars)
        self.dense = int(np.argmax(self.masks.reshape(len(self.chars), -1).sum(axis=1)))   # densest glyph, used for the waveform
        self.bg = hex_bgr(p["bg"])
        self.fg = hex_bgr(p["fg"])
        stops = np.stack([hex_bgr(c) for c in p["palette"]])
        xs = np.linspace(0, 255, len(stops))
        grid = np.arange(256)
        self.lut = np.stack([np.interp(grid, xs, stops[:, k]) for k in range(3)], axis=1).astype(np.float32)
        if p["color_mode"] == "mono":
            self.accent = self.fg
        elif p["color_mode"] == "palette":
            self.accent = stops[-1]
        else:
            self.accent = hex_bgr("#3ddc84")
        self.final_w = self.out_w * 2 if p["compare"] == "side" else self.out_w

    # -- core ---------------------------------------------------------------
    def render(self, frame_bgr, frame_no=0, amps=None, want_text=False):
        p, cols, rows = self.p, self.cols, self.rows
        small = cv2.resize(frame_bgr, (cols, rows), interpolation=cv2.INTER_AREA).astype(np.float32)
        small = (small - 127.5) * p["contrast"] + 127.5 + p["brightness"] * 2.55
        gray = small @ _LUM
        small = np.clip(gray[..., None] + (small - gray[..., None]) * p["saturation"], 0, 255)
        lum = np.clip(small @ _LUM, 0, 255)

        n = len(self.chars)
        if p["random_chars"]:                          # matrix rain: random glyph, brightness from the picture
            rng = np.random.default_rng(1234 + frame_no // 3)
            idx = np.where(lum > 28, rng.integers(1, n, size=(rows, cols)), 0)
        else:
            sel = 255 - lum if p["invert"] else lum
            idx = np.rint(sel / 255.0 * (n - 1)).astype(np.intp)

        mode = p["color_mode"]
        if mode == "rgb":
            fg = small
        elif mode == "mono":
            fg = np.broadcast_to(self.fg, (rows, cols, 3))
        else:
            fg = self.lut[lum.astype(np.intp)]

        if self.wf_rows:                               # audio waveform strip under the picture
            k = self.wf_rows
            a = np.zeros(cols, np.float32) if amps is None else np.asarray(amps, np.float32)
            dist = np.abs(np.arange(k) + 0.5 - k / 2.0)[:, None]
            half = np.maximum(a * k / 2.0, 0.5)[None, :]
            widx = np.where(dist <= half, self.dense, 0).astype(np.intp)
            idx = np.vstack([idx, widx])
            fg = np.vstack([fg, np.broadcast_to(self.accent, (k, cols, 3))])

        text = None
        if want_text:
            text = "\n".join("".join(r) for r in self.chars_arr[idx])

        g = self.masks[idx]                                        # (R, C, ch, cw)
        tile = g[..., None] * (fg[:, :, None, None, :] - self.bg) + self.bg
        img = tile.transpose(0, 2, 1, 3, 4).reshape(self.out_h, self.out_w, 3)
        img = np.clip(img, 0, 255).astype(np.uint8)

        if p["compare"] != "none":
            img = self._compare(frame_bgr, img)
        return img, text

    def _compare(self, frame_bgr, ascii_img):
        ascii_h = self.rows * self.ch
        orig = cv2.resize(frame_bgr, (self.out_w, ascii_h), interpolation=cv2.INTER_AREA)
        canvas = np.empty((self.out_h, self.out_w, 3), np.uint8)
        canvas[:] = self.bg.astype(np.uint8)
        canvas[:ascii_h] = orig
        if self.p["compare"] == "side":
            return np.hstack([canvas, ascii_img])
        out = ascii_img.copy()                          # split: left half original, right half ASCII
        half = self.out_w // 2
        out[:, :half] = canvas[:, :half]
        out[:, max(half - 1, 0):half + 1] = self.accent.astype(np.uint8)
        return out


# ------------------------------------------------------------------ audio --

class AudioEnvelope:
    """Cheap amplitude envelope of a PCM wav, used to draw the waveform strip."""
    BLOCK = 64

    def __init__(self, wav_path):
        with wave.open(wav_path, "rb") as w:
            self.sr = w.getframerate()
            chans = w.getnchannels()
            parts, carry = [], np.zeros(0, np.float32)
            while True:
                raw = w.readframes(1_000_000)
                if not raw:
                    break
                data = np.abs(np.frombuffer(raw, dtype=np.int16).astype(np.float32).reshape(-1, chans)).max(axis=1)
                data = np.concatenate([carry, data])
                use = (len(data) // self.BLOCK) * self.BLOCK
                carry = data[use:]
                if use:
                    parts.append(data[:use].reshape(-1, self.BLOCK).max(axis=1))
            if len(carry):
                parts.append(np.array([carry.max()], np.float32))
        self.env = np.concatenate(parts) if parts else np.zeros(1, np.float32)
        self.bps = self.sr / self.BLOCK                     # envelope blocks per second
        self.norm = max(float(np.percentile(self.env, 99.5)), 1.0)

    def amps_at(self, t, cols, window=0.12):
        i0 = max(int((t - window / 2) * self.bps), 0)
        i1 = min(int((t + window / 2) * self.bps) + 1, len(self.env))
        seg = self.env[i0:i1]
        if len(seg) < 2:
            return np.zeros(cols, np.float32)
        seg = np.interp(np.linspace(0, len(seg) - 1, cols), np.arange(len(seg)), seg)
        return np.sqrt(np.clip(seg / self.norm, 0, 1)).astype(np.float32)
