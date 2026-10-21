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
