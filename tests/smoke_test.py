"""Smoke test used by CI: does ASCII Forge actually run and convert things?

Run from the project root:  python tests/smoke_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import app as forge

H = {"X-Requested-With": "ascii-forge"}
client = forge.app.test_client()
tmp = tempfile.mkdtemp()


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def wait(job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/progress/{job_id}").get_json()
        if r["status"] in ("done", "error", "cancelled"):
            return r
        time.sleep(0.3)
    check(False, "job timed out")


# --- make a tiny test image and video -------------------------------------
x, y = np.meshgrid(np.arange(320), np.arange(180))
frame = np.dstack([x % 256, y % 256, (x + y) % 256]).astype(np.uint8)
cv2.circle(frame, (160, 90), 50, (255, 255, 255), -1)
img_path = os.path.join(tmp, "t.png")
vid_path = os.path.join(tmp, "t.mp4")
cv2.imwrite(img_path, frame)
writer = cv2.VideoWriter(vid_path, cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 180))
for i in range(24):
    f = frame.copy()
    cv2.circle(f, (40 + i * 10, 90), 30, (0, 255, 255), -1)
    writer.write(f)
writer.release()

# --- pages and security ----------------------------------------------------
check(client.get("/").status_code == 200, "index page loads")
check(client.get("/api/presets").status_code == 200, "presets endpoint")
check(client.post("/convert", json={}).status_code == 403, "POST without header is rejected")
check(client.get("/", headers={"Host": "evil.example"}).status_code == 403, "foreign Host header is rejected")

# --- every style renders ---------------------------------------------------
for style in forge.engine.STYLES:
    raw = dict(forge.engine.STYLES[style], kind="image", columns=80, width=640)
    p = forge.engine.normalize_params(raw)
    r = forge.engine.Renderer(p, 320, 180)
    out, text = r.render(frame, want_text=True)
    check(out.shape[0] > 0 and text, f"style '{style}' renders")

# --- full jobs through the API --------------------------------------------
r = client.post("/convert", headers=H, json={
    "kind": "image", "source": img_path, "style": "terminal", "color_mode": "palette",
    "compare": "side", "outputs": ["png", "txt"], "columns": 80, "width": 640})
j = wait(r.get_json()["job_id"])
check(j["status"] == "done" and len(j["files"]) == 3, "image job: PNG + TXT + before/after")

r = client.post("/convert", headers=H, json={
    "kind": "video", "source": vid_path, "waveform": True, "outputs": ["mp4", "gif", "frames_png", "frames_txt"],
    "columns": 80, "width": 640, "frame_step": 6, "gif_fps": 6, "gif_width": 320})
jid = r.get_json()["job_id"]
j = wait(jid)
check(j["status"] == "done", "video job finishes: " + j["message"])
names = {f["name"] for f in j["files"]}
check({"ascii.mp4", "ascii.gif", "frames_png.zip", "frames_txt.zip"} <= names, "video job: MP4, GIF and frame zips")

# --- history, projects and cleanup ----------------------------------------
check(len(client.get("/api/history").get_json()["jobs"]) >= 2, "history lists the jobs")
check(client.post("/api/projects", headers=H, json={"name": "ci", "params": {"kind": "image"}}).status_code == 200, "project saves")
with forge.store._db() as c:
    c.execute("UPDATE jobs SET finished = finished - ?", (200 * 3600,))
cleaned = client.post("/api/cleanup", headers=H).get_json()["cleaned"]
check(cleaned["expired_jobs"] >= 2, "old outputs are cleaned up automatically")
check(not os.path.exists(os.path.join(forge.GENERATED_DIR, jid)), "expired output folder is gone")

print("\nAll smoke tests passed.")
