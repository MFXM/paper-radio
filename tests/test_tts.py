import json, os, re
import pytest
from conftest import load


@pytest.fixture()
def tts(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_DISABLE_WORKER", "1")
    monkeypatch.setenv("PODCAST_DIR", str(tmp_path))
    monkeypatch.setenv("PODCAST_PUBLIC_URL", "http://feed.example/")
    mod = load("tts_app", "services/tts/app.py")
    return mod


def test_parse_chapters_split_and_titles(tts):
    text = "# Intro\nHello there.\n\n---\n\nChapter 2: Method\nDetails.\n\n---\n\nPlain chunk."
    ch = tts.parse_chapters({"text": text})
    assert [c["title"] for c in ch] == ["Intro", "Chapter 2: Method", "Chapter 3"]
    assert ch[0]["text"] == "Hello there."


def test_parse_chapters_single_and_explicit(tts):
    assert tts.parse_chapters({"text": "only one"}) == [{"title": "Full Episode", "text": "only one"}]
    ch = tts.parse_chapters({"chapters": [{"title": "A", "text": "x"}, {"text": ""}, "bad"]})
    assert ch == [{"title": "A", "text": "x"}]


def test_html_transcript_is_escaped(tts):
    page = tts.build_html_transcript("<script>x</script>", 'https://a.b/"q', "a <b> c")
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert "&lt;b&gt;" in page and "&quot;q" in page


def test_feed_append_and_remove(tts):
    tts.init_feed()
    item = tts.feed_item("T ]]> x", "podcast_1_abc123", 10, "tl<dr>", "https://arxiv.org/abs/1", "Body", "Mon, 01 Jan 2026 00:00:00 +0000")
    tts.append_feed_item(item)
    feed = open(tts.FEED_PATH, encoding="utf-8").read()
    assert "http://feed.example/podcast_1_abc123.mp3" in feed
    assert feed.count("<item>") == 1 and feed.strip().endswith("</rss>")
    import xml.dom.minidom
    xml.dom.minidom.parseString(feed)                       # still well-formed
    assert tts.remove_feed_item("podcast_1_abc123") is True
    assert "<item>" not in open(tts.FEED_PATH, encoding="utf-8").read()
    assert tts.remove_feed_item("podcast_1_abc123") is False


def test_ffmetadata_escapes(tts):
    meta = tts.ffmetadata("a=b;c", [{"title": "x#y", "start_ms": 0, "end_ms": 5}])
    assert "title=a\\=b\\;c" in meta and "title=x\\#y" in meta


def test_routes(tts):
    client = tts.app.test_client()
    assert client.post("/podcast", json={}).status_code == 400
    r = client.post("/podcast", json={"title": "t", "text": "hello"})
    assert r.status_code == 202
    body = r.get_json()
    assert re.match(r"^podcast_\d+_[0-9a-f]{6}$", body["filename_slug"])
    assert body["audio_url"].startswith("http://feed.example/podcast_")
    assert tts.job_queue.qsize() == 1
    # path traversal is rejected
    assert client.post("/delete", json={"filename": "../../etc/passwd"}).status_code == 400
    assert client.post("/delete", json={"filename": "podcast_1_abc123.mp3"}).get_json()["status"] == "success"


# --------------------------------------------------------------------------- synchronous synthesis
class FakeKokoro:
    """Stands in for kokoro_onnx.Kokoro: three voices, a short tone per call."""
    sample_rate = 24000

    def __init__(self, lock=None):
        self.calls, self.lock = [], lock

    def get_voices(self):
        return ["af_heart", "bf_emma", "bm_george"]

    def create(self, text, voice, speed, lang):
        import numpy as np
        assert self.lock is None or self.lock.locked()           # every call holds the shared lock
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        n = int(self.sample_rate * 0.2)
        return (0.5 * np.sin(np.arange(n) / 20)).astype(np.float32), self.sample_rate


def synth_app(tmp_path, monkeypatch, token=None):
    monkeypatch.setenv("TTS_DISABLE_WORKER", "1")
    monkeypatch.setenv("PODCAST_DIR", str(tmp_path / "podcasts"))
    if token:
        monkeypatch.setenv("TTS_SYNTH_TOKEN", token)
    else:
        monkeypatch.delenv("TTS_SYNTH_TOKEN", raising=False)
    mod = load("tts_app_sync", "services/tts/app.py")
    fake = FakeKokoro(mod.kokoro_lock)
    mod.engine["kokoro"] = fake
    mod.worker_state["ready"] = True
    return mod, fake, mod.app.test_client()


def test_synthesize_returns_pcm_wav(tmp_path, monkeypatch):
    import io, wave
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    r = client.post("/v1/synthesize", json={"text": " Hello there. ", "voice": "bf_emma", "speed": 1.25})
    assert r.status_code == 200 and r.mimetype == "audio/wav" and r.headers["X-Sample-Rate"] == "24000"
    assert r.data[:4] == b"RIFF" and r.data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(r.data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 4800)
    assert fake.calls == [{"text": "Hello there.", "voice": "bf_emma", "speed": 1.25, "lang": "en-gb"}]
    # form fields work too, and voice/speed default to TTS_VOICE / TTS_SPEED
    r = client.post("/v1/synthesize", data={"text": "Hi."})
    assert r.status_code == 200 and fake.calls[-1]["voice"] == mod.DEFAULT_VOICE and fake.calls[-1]["speed"] == mod.TTS_SPEED
    assert not mod.kokoro_lock.locked()


def test_synthesize_validation(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    assert client.post("/v1/synthesize", json={}).status_code == 400
    assert client.post("/v1/synthesize", json={"text": "   "}).status_code == 400
    assert client.post("/v1/synthesize", json={"text": "x" * 600}).status_code == 200
    r = client.post("/v1/synthesize", json={"text": "x" * 601})
    assert r.status_code == 413 and "600" in r.get_json()["error"]
    r = client.post("/v1/synthesize", json={"text": "Hi.", "voice": "zz_nobody"})
    assert r.status_code == 422 and "unknown voice" in r.get_json()["error"]
    for bad in (0.49, 2.01, "fast", float("nan")):
        assert client.post("/v1/synthesize", json={"text": "Hi.", "speed": bad}).status_code == 422, bad
    assert len(fake.calls) == 1                                    # only the 600-character request was spoken


def test_synthesize_auth(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch, token="s3cret")
    for path, call in (("/v1/synthesize", client.post), ("/v1/voices", client.get)):
        kw = {"json": {"text": "Hi."}} if path == "/v1/synthesize" else {}
        assert call(path, **kw).status_code == 401
        assert call(path, headers={"Authorization": "Bearer wrong"}, **kw).status_code == 401
        assert call(path, headers={"Authorization": "s3cret"}, **kw).status_code == 401
        assert call(path, headers={"Authorization": "Bearer s3cret"}, **kw).status_code == 200
    # the existing routes keep their behaviour
    assert client.get("/health").status_code == 200


def test_synthesize_open_without_token(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    assert mod.SYNTH_TOKEN is None
    assert client.post("/v1/synthesize", json={"text": "Hi."}).status_code == 200
    assert client.get("/v1/voices").status_code == 200


def test_synthesize_not_ready(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    mod.engine["kokoro"] = None
    r = client.post("/v1/synthesize", json={"text": "Hi."})
    assert r.status_code == 503 and "loading" in r.get_json()["error"]
    assert client.get("/v1/voices").status_code == 503
    mod.worker_state["error"] = "model load failed: missing file"
    r = client.post("/v1/synthesize", json={"text": "Hi."})
    assert r.status_code == 503 and "model load failed" in r.get_json()["error"]


def test_voices_lists_the_loaded_file(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    body = client.get("/v1/voices").get_json()
    assert body == {"voices": ["af_heart", "bf_emma", "bm_george"], "default": mod.DEFAULT_VOICE}


def test_synthesize_leaves_feed_and_podcast_dir_alone(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    pod = tmp_path / "podcasts"
    pod.mkdir()
    mod.init_feed()
    before = {p.name: p.read_bytes() for p in pod.iterdir()}
    for _ in range(3):
        assert client.post("/v1/synthesize", json={"text": "Hello."}).status_code == 200
    client.get("/v1/voices")
    assert {p.name: p.read_bytes() for p in pod.iterdir()} == before
    assert mod.job_queue.qsize() == 0


def test_engine_error_is_reported(tmp_path, monkeypatch):
    mod, fake, client = synth_app(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("onnx exploded")

    fake.create = boom
    r = client.post("/v1/synthesize", json={"text": "Hi."})
    assert r.status_code == 500 and r.get_json()["error"] == "synthesis failed: RuntimeError"
    assert not mod.kokoro_lock.locked()


def test_podcast_worker_takes_the_lock_per_chapter(tmp_path, monkeypatch):
    import sys, types
    mod, fake, client = synth_app(tmp_path, monkeypatch)
    (tmp_path / "podcasts").mkdir()
    monkeypatch.setitem(sys.modules, "soundfile", types.SimpleNamespace(write=lambda path, data, sr: open(path, "wb").close()))

    def fake_ffmpeg(cmd, check):
        open(cmd[-1], "wb").write(b"mp3")                        # last argument is the output path

    monkeypatch.setattr(mod.subprocess, "run", fake_ffmpeg)
    mod.synthesise(fake, {"title": "T", "text": "# A\nOne.\n\n---\n\n# B\nTwo.", "filename_slug": "podcast_1_abc123"})
    assert [c["text"] for c in fake.calls] == ["One.", "Two."]  # FakeKokoro.create asserts the lock is held
    assert not mod.kokoro_lock.locked()



# --------------------------------------------------------------------------- uploaded episodes (/v1/episode)
def tone_wav(seconds=0.5, rate=24000):
    import io, math, struct, wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(int(seconds * rate))))
    return out.getvalue()


def episode_app(tmp_path, monkeypatch, token="s3cret", real_ffmpeg=False):
    mod, fake, client = synth_app(tmp_path, monkeypatch, token=token)
    (tmp_path / "podcasts").mkdir(exist_ok=True)
    mod.init_feed()
    seen = []
    if not real_ffmpeg:
        def fake_ffmpeg(cmd, check):
            seen.append(cmd)
            open(cmd[-1], "wb").write(b"ID3" + open(cmd[cmd.index("-i") + 1], "rb").read()[:100])
        monkeypatch.setattr(mod.subprocess, "run", fake_ffmpeg)
    return mod, fake, client, seen


def upload(client, token="s3cret", **fields):
    import io
    data = {"title": "Concept - Qubit", "text": "A qubit is a unit.\n\nIt has two states.", "tldr": "A qubit is a unit.",
            "link": "http://10.0.0.221:8090/#/n/qubit", **fields}
    if "audio" not in fields:
        data["audio"] = (io.BytesIO(tone_wav()), "narration.wav", "audio/wav")
    elif fields["audio"] is None:
        data.pop("audio")
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/v1/episode", data=data, headers=h, content_type="multipart/form-data")


def test_episode_upload_encodes_and_adds_feed_item(tmp_path, monkeypatch):
    import xml.dom.minidom
    mod, fake, client, seen = episode_app(tmp_path, monkeypatch)
    r = upload(client)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    slug = body["filename_slug"]
    assert re.match(r"^episode_\d+_[0-9a-f]{6}$", slug) and body["duration_s"] == 0.5
    pod = tmp_path / "podcasts"
    assert (pod / f"{slug}.mp3").exists() and (pod / f"{slug}.html").exists()
    assert "It has two states." in (pod / f"{slug}.html").read_text(encoding="utf-8")
    feed = (pod / "feed.xml").read_text(encoding="utf-8")
    xml.dom.minidom.parseString(feed)
    assert "<![CDATA[Concept - Qubit]]>" in feed and f"{slug}.mp3" in feed and "http://10.0.0.221:8090/#/n/qubit" in feed
    meta = [c for c in seen[0] if c.endswith(".txt")]
    assert seen[0][-1].endswith(f"{slug}.mp3") and meta                     # chapter/title metadata passed to ffmpeg
    assert fake.calls == [] and mod.job_queue.qsize() == 0                  # nothing synthesised, nothing queued
    assert len(list(pod.glob("*.wav"))) == 0


def test_episode_upload_validation(tmp_path, monkeypatch):
    import io
    mod, fake, client, seen = episode_app(tmp_path, monkeypatch)
    assert upload(client, token=None).status_code == 401
    assert upload(client, token="wrong").status_code == 401
    assert upload(client, audio=None).status_code == 400
    assert upload(client, title="  ").status_code == 400
    assert upload(client, title="x" * 301).status_code == 413
    assert upload(client, link="javascript:alert(1)").status_code == 400
    r = upload(client, audio=(io.BytesIO(b"not a wav"), "n.wav", "audio/wav"))
    assert r.status_code == 415
    monkeypatch.setattr(mod, "MAX_EPISODE_BYTES", 1000)
    assert upload(client).status_code == 413
    assert seen == [] and "<item>" not in (tmp_path / "podcasts" / "feed.xml").read_text(encoding="utf-8")


def test_episode_encode_failure_leaves_nothing(tmp_path, monkeypatch):
    mod, fake, client, seen = episode_app(tmp_path, monkeypatch)

    def broken(cmd, check):
        open(cmd[-1], "wb").write(b"half")
        raise mod.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(mod.subprocess, "run", broken)
    r = upload(client)
    assert r.status_code == 500
    assert sorted(p.name for p in (tmp_path / "podcasts").iterdir()) == ["feed.xml"]


def test_episode_delete_only_uploaded_ones(tmp_path, monkeypatch):
    mod, fake, client, seen = episode_app(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer s3cret"}
    slug = upload(client).get_json()["filename_slug"]
    # a generated episode in the same feed must stay out of reach
    mod.append_feed_item(mod.feed_item("Briefing - X", "podcast_1_abc123", 10, "", "", "t", "Mon, 01 Jan 2026 00:00:00 +0000"))
    assert client.delete("/v1/episode/podcast_1_abc123", headers=auth).status_code == 400
    assert client.delete(f"/v1/episode/{slug}").status_code == 401
    r = client.delete(f"/v1/episode/{slug}", headers=auth)
    assert r.status_code == 200 and r.get_json() == {"deleted": True, "filename_slug": slug}
    feed = (tmp_path / "podcasts" / "feed.xml").read_text(encoding="utf-8")
    assert slug not in feed and "podcast_1_abc123" in feed
    assert not (tmp_path / "podcasts" / f"{slug}.mp3").exists()
    assert client.delete(f"/v1/episode/{slug}", headers=auth).status_code == 404


def test_episode_upload_real_ffmpeg(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    mod, fake, client, seen = episode_app(tmp_path, monkeypatch, real_ffmpeg=True)
    r = upload(client)
    assert r.status_code == 201, r.get_json()
    mp3 = (tmp_path / "podcasts" / f"{r.get_json()['filename_slug']}.mp3").read_bytes()
    assert mp3[:3] == b"ID3" and len(mp3) > 1000
