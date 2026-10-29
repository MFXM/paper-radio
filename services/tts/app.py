"""Kokoro TTS + podcast feed service.

Receives a script over HTTP, synthesises it with Kokoro (ONNX), encodes an MP3
with chapter markers, writes an HTML transcript and appends an item to an
RSS 2.0 podcast feed. Jobs are processed one at a time by a background worker.

Endpoints
  POST /podcast   {title, text | chapters[], tldr?, link?, voice?}  -> 202 {filename_slug}
  POST /delete    {filename}                                        -> removes files + feed item
  GET  /health
  POST /v1/synthesize {text, voice?, speed?}                        -> audio/wav, one utterance, synchronous
  GET  /v1/voices                                                   -> {voices[], default}
  POST /v1/episode  multipart: audio (WAV), title, text?, tldr?, link? -> 201 {filename_slug}, finished audio
  DELETE /v1/episode/<slug>                                         -> removes an uploaded episode

The two /v1 routes are a synchronous single-utterance synthesis endpoint for other apps. They share
the worker's Kokoro instance and never touch PODCAST_DIR or the feed. If TTS_SYNTH_TOKEN is set they
require `Authorization: Bearer <token>`.

/v1/episode publishes audio another app has already rendered (e.g. a QKB page narration): the WAV is encoded
to MP3 right away, with its transcript and feed item, without waiting behind queued /podcast jobs. Uploaded
episodes get slugs episode_<ts>_<id>, and DELETE /v1/episode only removes those, never generated episodes.
Both use the same bearer token as the other /v1 routes.
"""
from __future__ import annotations

import hmac
import html
import io
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request

PODCAST_DIR = os.environ.get("PODCAST_DIR", "/podcasts")
MODEL_PATH = os.environ.get("KOKORO_MODEL", "/models/kokoro-v1.0.onnx")
VOICES_PATH = os.environ.get("KOKORO_VOICES", "/models/voices-v1.0.bin")
BASE_URL = os.environ.get("PODCAST_PUBLIC_URL", "http://localhost:8080").rstrip("/")
FEED_TITLE = os.environ.get("PODCAST_TITLE", "Research Radio")
FEED_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "Automated audio summaries of research papers")
FEED_LANGUAGE = os.environ.get("PODCAST_LANGUAGE", "en-gb")
DEFAULT_VOICE = os.environ.get("TTS_VOICE", "bm_george")
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.0"))
CHAPTER_GAP_S = float(os.environ.get("TTS_CHAPTER_GAP_SECONDS", "0.8"))
MAX_TEXT_CHARS = int(os.environ.get("TTS_MAX_TEXT_CHARS", "400000"))
SYNTH_TOKEN = os.environ.get("TTS_SYNTH_TOKEN") or None
SYNTH_MAX_CHARS = 600          # one sentence or a short paragraph; longer texts belong in /podcast
SYNTH_SPEED_RANGE = (0.5, 2.0)
MAX_EPISODE_BYTES = int(float(os.environ.get("TTS_MAX_EPISODE_MB", "200")) * 2**20)   # an uploaded WAV (~45 min at 24 kHz)
MAX_TITLE_CHARS = 300

FEED_PATH = os.path.join(PODCAST_DIR, "feed.xml")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
UPLOAD_SLUG_RE = re.compile(r"^episode_\d{1,12}_[0-9a-f]{6}$")
CHAPTER_SEPARATOR = "\n\n---\n\n"

app = Flask(__name__)
job_queue: "queue.Queue[dict | None]" = queue.Queue()
feed_lock = threading.Lock()
worker_state = {"ready": False, "error": None, "current": None}
# The worker's Kokoro instance, shared with the synchronous route once loaded.
engine: dict = {"kokoro": None}
# Serialises kokoro.create: the podcast worker takes it per chapter, so a synchronous request waits
# for at most one chapter, not a whole episode.
kokoro_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers
def esc(value: str) -> str:
    """Escape text for HTML and XML attribute/element content."""
    return html.escape(value or "", quote=True)


def cdata(value: str) -> str:
    """Wrap text in CDATA, splitting any embedded terminator."""
    return "<![CDATA[" + (value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def init_feed() -> None:
    if os.path.exists(FEED_PATH):
        return
    os.makedirs(PODCAST_DIR, exist_ok=True)
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{esc(FEED_TITLE)}</title>
    <link>{esc(BASE_URL)}</link>
    <description>{esc(FEED_DESCRIPTION)}</description>
    <language>{esc(FEED_LANGUAGE)}</language>
    <itunes:image href="{esc(BASE_URL)}/cover.png"/>
    <image>
      <url>{esc(BASE_URL)}/cover.png</url>
      <title>{esc(FEED_TITLE)}</title>
      <link>{esc(BASE_URL)}</link>
    </image>
  </channel>
</rss>
"""
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        f.write(rss)


def parse_chapters(data: dict) -> list[dict]:
    """Return [{title, text}] from an explicit 'chapters' list or from 'text'
    split on the chapter separator (blank line, ---, blank line)."""
    if isinstance(data.get("chapters"), list) and data["chapters"]:
        out = []
        for i, ch in enumerate(data["chapters"], 1):
            if isinstance(ch, dict) and str(ch.get("text", "")).strip():
                out.append({"title": str(ch.get("title") or f"Chapter {i}"), "text": str(ch["text"])})
        return out

    text = str(data.get("text", ""))
    chunks = [c.strip() for c in text.split(CHAPTER_SEPARATOR) if c.strip()]
    if len(chunks) <= 1:
        return [{"title": "Full Episode", "text": text}]

    chapters = []
    for idx, chunk in enumerate(chunks, 1):
        lines = chunk.split("\n")
        first = lines[0].strip()
        if first.startswith("#"):
            title, body = re.sub(r"^#+\s*", "", first), "\n".join(lines[1:]).strip()
        elif re.match(r"^Chapter\s+\d+:", first, re.IGNORECASE):
            title, body = first, "\n".join(lines[1:]).strip()
        else:
            title, body = f"Chapter {idx}", chunk
        chapters.append({"title": title or f"Chapter {idx}", "text": body or chunk})
    return chapters


def paragraphs_html(text: str) -> str:
    return "".join(f"<p>{esc(p.strip())}</p>" for p in (text or "").split("\n\n") if p.strip())


def build_html_transcript(title: str, link: str, text: str) -> str:
    link_section = (
        f'<div class="meta"><strong>Paper Link:</strong> '
        f'<a href="{esc(link)}" target="_blank" rel="noopener">{esc(link)}</a></div>'
        if link else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)} - Transcript</title>
  <style>
    :root {{ --bg:#121212; --card:#1e1e1e; --text:#e0e0e0; --accent:#4da6ff; --border:#333; }}
    @media (prefers-color-scheme: light) {{
      :root {{ --bg:#f8f9fa; --card:#fff; --text:#212529; --accent:#0066cc; --border:#e0e0e0; }}
    }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
           line-height:1.6; background:var(--bg); color:var(--text); max-width:800px; margin:0 auto; padding:16px; }}
    header {{ background:var(--card); border:1px solid var(--border); padding:20px; border-radius:8px; margin-bottom:24px; }}
    h1 {{ font-size:1.3rem; margin:0 0 10px 0; line-height:1.3; }}
    .meta {{ font-size:.85rem; opacity:.85; margin-top:6px; }}
    a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    article p {{ margin-bottom:1.2em; font-size:1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(title)}</h1>
    {link_section}
  </header>
  <article>
    {paragraphs_html(text)}
  </article>
</body>
</html>
"""


def ffmetadata(title: str, markers: list[dict]) -> str:
    def m(v: str) -> str:  # escape per FFMETADATA spec
        return re.sub(r"([=;#\\\n])", r"\\\1", v)
    lines = [";FFMETADATA1", f"title={m(title)}", ""]
    for ch in markers:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={ch['start_ms']}", f"END={ch['end_ms']}",
                  f"title={m(ch['title'])}", ""]
    return "\n".join(lines)


def feed_item(title: str, slug: str, size: int, tldr: str, link: str, text: str, pub_date: str) -> str:
    notes = []
    if tldr:
        notes.append(f"<p><strong>TL;DR:</strong> {esc(tldr)}</p>")
    if link:
        notes.append(f'<p><strong>Paper Link:</strong> <a href="{esc(link)}">{esc(link)}</a></p>')
    notes.append(f"<hr/><h3>Full Transcript</h3>{paragraphs_html(text)}")
    return (
        "\n    <item>\n"
        f"      <title>{cdata(title)}</title>\n"
        f"      <description>{cdata(chr(10).join(notes))}</description>\n"
        f'      <enclosure url="{esc(BASE_URL)}/{slug}.mp3" length="{size}" type="audio/mpeg"/>\n'
        f'      <podcast:transcript url="{esc(BASE_URL)}/{slug}.html" type="text/html"/>\n'
        + (f"      <link>{esc(link)}</link>\n" if link else "")
        + f'      <guid isPermaLink="false">{slug}</guid>\n'
        f"      <pubDate>{pub_date}</pubDate>\n"
        "    </item>\n"
    )


def append_feed_item(item_xml: str) -> None:
    with feed_lock:
        init_feed()
        with open(FEED_PATH, "r", encoding="utf-8") as f:
            feed = f.read()
        idx = feed.rfind("</channel>")
        feed = feed[:idx] + item_xml + "  " + feed[idx:]
        tmp = FEED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(feed)
        os.replace(tmp, FEED_PATH)


def remove_feed_item(slug: str) -> bool:
    with feed_lock:
        if not os.path.exists(FEED_PATH):
            return False
        with open(FEED_PATH, "r", encoding="utf-8") as f:
            feed = f.read()
        pattern = rf"\s*<item>(?:(?!</item>)[\s\S])*?<guid[^>]*>{re.escape(slug)}</guid>[\s\S]*?</item>"
        new, n = re.subn(pattern, "", feed)
        if n:
            tmp = FEED_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new)
            os.replace(tmp, FEED_PATH)
        return bool(n)


def lang_for(voice: str) -> str:
    return "en-gb" if voice.startswith("b") else "en-us"


# --------------------------------------------------------------------------- worker
def synthesise(kokoro, data: dict) -> str:
    import numpy as np
    import soundfile as sf

    text = data.get("text", "")
    title = data.get("title") or f"Podcast {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    voice = data.get("voice") or DEFAULT_VOICE
    slug = data["filename_slug"]
    chapters = [c for c in parse_chapters(data) if c["text"].strip()]
    if not text:
        text = CHAPTER_SEPARATOR.join(c["text"] for c in chapters)

    segments, markers, cur_ms, sr = [], [], 0, 24000
    for idx, ch in enumerate(chapters, 1):
        with kokoro_lock:
            samples, sr = kokoro.create(ch["text"].strip(), voice=voice, speed=TTS_SPEED, lang=lang_for(voice))
        dur = int(len(samples) / sr * 1000)
        markers.append({"title": ch["title"].strip(), "start_ms": cur_ms, "end_ms": cur_ms + dur})
        segments.append(samples)
        cur_ms += dur
        if idx < len(chapters) and CHAPTER_GAP_S > 0:
            segments.append(np.zeros(int(sr * CHAPTER_GAP_S), dtype=samples.dtype))
            cur_ms += int(CHAPTER_GAP_S * 1000)

    wav_path = None
    try:
        fd, wav_path = tempfile.mkstemp(suffix=".wav"); os.close(fd)
        sf.write(wav_path, np.concatenate(segments), sr)
        mp3_path = encode_mp3(wav_path, title, markers, slug)
    finally:
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)
    publish(title, slug, mp3_path, text, data.get("tldr", ""), data.get("link", ""))
    return mp3_path


def encode_mp3(wav_path: str, title: str, markers: list[dict], slug: str) -> str:
    """WAV -> PODCAST_DIR/<slug>.mp3 (128 kbit/s, ID3 title and chapter markers); returns the MP3 path."""
    meta_path = None
    try:
        fd, meta_path = tempfile.mkstemp(suffix=".txt"); os.close(fd)
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(ffmetadata(title, markers))
        mp3_path = os.path.join(PODCAST_DIR, f"{slug}.mp3")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path, "-i", meta_path,
                        "-map_metadata", "1", "-id3v2_version", "3", "-b:a", "128k", mp3_path], check=True)
        return mp3_path
    finally:
        if meta_path and os.path.exists(meta_path):
            os.remove(meta_path)


def publish(title: str, slug: str, mp3_path: str, text: str, tldr: str, link: str) -> None:
    """Write the HTML transcript of an encoded episode and add it to the feed."""
    with open(os.path.join(PODCAST_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
        f.write(build_html_transcript(title, link, text))
    pub = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    append_feed_item(feed_item(title, slug, os.path.getsize(mp3_path), tldr, link, text, pub))


def remove_episode(slug: str) -> tuple[bool, bool]:
    """Delete <slug>.mp3/.html and its feed item; (mp3 deleted, feed updated)."""
    deleted = False
    for ext in (".mp3", ".html"):
        p = os.path.join(PODCAST_DIR, slug + ext)
        if os.path.exists(p):
            os.remove(p)
            deleted = deleted or ext == ".mp3"
    return deleted, remove_feed_item(slug)


def tts_worker() -> None:
    try:
        from kokoro_onnx import Kokoro
        kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
    except Exception as exc:  # model missing or broken: report via /health
        worker_state["error"] = f"model load failed: {exc}"
        print(worker_state["error"], flush=True)
        return
    engine["kokoro"] = kokoro
    worker_state["ready"] = True
    print("Kokoro TTS worker ready.", flush=True)
    while True:
        data = job_queue.get()
        if data is None:
            break
        worker_state["current"] = data.get("filename_slug")
        try:
            path = synthesise(kokoro, data)
            print(f"Finished '{data.get('title')}' -> {path}", flush=True)
        except Exception as exc:
            print(f"Error generating '{data.get('title')}': {exc}", flush=True)
        finally:
            worker_state["current"] = None
            job_queue.task_done()


# --------------------------------------------------------------------------- synchronous synthesis
def pcm16_wav(samples, sample_rate: int) -> bytes:
    """Float samples in [-1, 1] -> a 16-bit PCM mono WAV file (bytes)."""
    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def voice_ids(kokoro) -> list[str]:
    """Voice ids in the loaded voices file, sorted."""
    get = getattr(kokoro, "get_voices", None)
    return sorted(get() if callable(get) else (getattr(kokoro, "voices", None) or {}))


def synth_denied():
    """None if the request may use the /v1 routes, else a 401 response (only when TTS_SYNTH_TOKEN is set)."""
    if not SYNTH_TOKEN:
        return None
    got = request.headers.get("Authorization", "")
    if hmac.compare_digest(got.encode("utf-8"), f"Bearer {SYNTH_TOKEN}".encode("utf-8")):
        return None
    return jsonify(error="missing or wrong bearer token"), 401, {"WWW-Authenticate": "Bearer"}


def loaded_engine():
    """(kokoro, None) once the worker has loaded the model, else (None, 503 response)."""
    if worker_state["error"]:
        return None, (jsonify(error=worker_state["error"]), 503)
    kokoro = engine["kokoro"]
    if kokoro is None:
        return None, (jsonify(error="the speech model is still loading; try again shortly"), 503, {"Retry-After": "10"})
    return kokoro, None


def parse_speed(raw) -> float | None:
    """Speed from the request (default TTS_SPEED); None if it is not a number in SYNTH_SPEED_RANGE."""
    if raw is None or raw == "":
        return TTS_SPEED
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        return None
    lo, hi = SYNTH_SPEED_RANGE
    return speed if math.isfinite(speed) and lo <= speed <= hi else None


@app.post("/v1/synthesize")
def synthesize_one():
    denied = synth_denied()
    if denied:
        return denied
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify(error="'text' is required"), 400
    if len(text) > SYNTH_MAX_CHARS:
        return jsonify(error=f"text is {len(text)} characters; the limit is {SYNTH_MAX_CHARS}, send one sentence at a time"), 413
    speed = parse_speed(data.get("speed"))
    if speed is None:
        return jsonify(error=f"speed must be a number between {SYNTH_SPEED_RANGE[0]} and {SYNTH_SPEED_RANGE[1]}"), 422
    kokoro, not_ready = loaded_engine()
    if not_ready:
        return not_ready
    voice = str(data.get("voice") or DEFAULT_VOICE).strip()
    if voice not in voice_ids(kokoro):
        return jsonify(error=f"unknown voice '{voice[:40]}'; GET /v1/voices lists the available ones"), 422
    try:
        with kokoro_lock:
            samples, sr = kokoro.create(text, voice=voice, speed=speed, lang=lang_for(voice))
    except Exception as exc:  # model errors: report briefly, no stack trace
        print(f"Synchronous synthesis failed: {exc}", flush=True)
        return jsonify(error=f"synthesis failed: {type(exc).__name__}"), 500
    return Response(pcm16_wav(samples, sr), mimetype="audio/wav",
                    headers={"Cache-Control": "no-store", "X-Sample-Rate": str(int(sr))})


@app.get("/v1/voices")
def list_voices():
    denied = synth_denied()
    if denied:
        return denied
    kokoro, not_ready = loaded_engine()
    if not_ready:
        return not_ready
    return jsonify(voices=voice_ids(kokoro), default=DEFAULT_VOICE)


# --------------------------------------------------------------------------- finished audio
def wav_duration_ms(path: str) -> int | None:
    """Length of a PCM WAV in ms, None if the file is not one or is empty."""
    try:
        with wave.open(path) as w:
            frames, rate = w.getnframes(), w.getframerate()
    except (wave.Error, EOFError):
        return None
    return int(frames / rate * 1000) if frames and rate else None


@app.post("/v1/episode")
def upload_episode():
    denied = synth_denied()
    if denied:
        return denied
    if (request.content_length or 0) > MAX_EPISODE_BYTES:
        return jsonify(error=f"upload larger than {MAX_EPISODE_BYTES // 2**20} MB (TTS_MAX_EPISODE_MB)"), 413
    audio = request.files.get("audio")
    title = " ".join(str(request.form.get("title") or "").split())
    text = str(request.form.get("text") or "")
    tldr = str(request.form.get("tldr") or "")[:1000]
    link = str(request.form.get("link") or "").strip()
    if audio is None or not title:
        return jsonify(error="'audio' (a WAV file) and 'title' are required"), 400
    if len(title) > MAX_TITLE_CHARS or len(text) > MAX_TEXT_CHARS:
        return jsonify(error=f"title is limited to {MAX_TITLE_CHARS}, text to {MAX_TEXT_CHARS} characters"), 413
    if link and not re.match(r"^https?://\S{1,490}$", link):
        return jsonify(error="link must be an http(s) URL"), 400
    os.makedirs(PODCAST_DIR, exist_ok=True)
    slug = f"episode_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    fd, wav_path = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    try:
        audio.save(wav_path)
        dur = wav_duration_ms(wav_path)
        if dur is None:
            return jsonify(error="audio must be a non-empty PCM WAV"), 415
        mp3_path = encode_mp3(wav_path, title, [{"title": title, "start_ms": 0, "end_ms": dur}], slug)
    except subprocess.CalledProcessError as exc:
        print(f"Encoding uploaded episode '{title}' failed: {exc}", flush=True)
        partial = os.path.join(PODCAST_DIR, f"{slug}.mp3")
        if os.path.exists(partial):
            os.remove(partial)
        return jsonify(error="encoding the audio failed"), 500
    finally:
        os.remove(wav_path)
    publish(title, slug, mp3_path, text, tldr, link)
    print(f"Published uploaded episode '{title}' -> {mp3_path}", flush=True)
    return jsonify(filename_slug=slug, duration_s=round(dur / 1000, 1),
                   audio_url=f"{BASE_URL}/{slug}.mp3", transcript_url=f"{BASE_URL}/{slug}.html"), 201


@app.delete("/v1/episode/<slug>")
def delete_uploaded_episode(slug: str):
    denied = synth_denied()
    if denied:
        return denied
    if not UPLOAD_SLUG_RE.match(slug):
        return jsonify(error="only uploaded episodes (episode_<ts>_<id>) can be removed here"), 400
    deleted, feed_updated = remove_episode(slug)
    if not (deleted or feed_updated):
        return jsonify(error="no such episode"), 404
    return jsonify(deleted=True, filename_slug=slug)


# --------------------------------------------------------------------------- routes
@app.post("/podcast")
def podcast():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    if not data.get("text") and not data.get("chapters"):
        return jsonify(error="Either 'text' or 'chapters' is required"), 400
    if len(str(data.get("text", ""))) > MAX_TEXT_CHARS:
        return jsonify(error=f"text longer than {MAX_TEXT_CHARS} characters"), 413
    if worker_state["error"]:
        return jsonify(error=worker_state["error"]), 503
    slug = f"podcast_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    data["filename_slug"] = slug
    job_queue.put(data)
    return jsonify(status="queued", filename_slug=slug, queue_position=job_queue.qsize(),
                   audio_url=f"{BASE_URL}/{slug}.mp3", transcript_url=f"{BASE_URL}/{slug}.html"), 202


@app.route("/delete", methods=["POST", "DELETE"])
def delete():
    data = request.get_json(silent=True) or {}
    slug = str(data.get("filename", "")).removesuffix(".mp3").removesuffix(".html")
    if not SLUG_RE.match(slug):
        return jsonify(error="filename must be a podcast slug like podcast_<ts>_<id>"), 400
    deleted, feed_updated = remove_episode(slug)
    return jsonify(status="success", file_deleted=deleted, feed_updated=feed_updated)


@app.get("/health")
def health():
    status = 503 if worker_state["error"] else 200
    return jsonify(ready=worker_state["ready"], error=worker_state["error"],
                   queued=job_queue.qsize(), current=worker_state["current"]), status


def start_worker() -> None:
    threading.Thread(target=tts_worker, daemon=True, name="tts-worker").start()


if os.environ.get("TTS_DISABLE_WORKER") != "1":
    init_feed() if os.path.isdir(PODCAST_DIR) else None
    start_worker()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
