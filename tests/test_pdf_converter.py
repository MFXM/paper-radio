import io
import pytest
from conftest import load

pymupdf = pytest.importorskip("pymupdf")


@pytest.fixture()
def pdf(monkeypatch):
    monkeypatch.setenv("PDF_ALLOWED_HOSTS", "arxiv.org,export.arxiv.org")
    monkeypatch.setenv("PDF_MAX_MB", "1")
    return load("pdf_app", "services/pdf-converter/app.py")


def make_pdf(text="Hello pipeline", title="My Paper"):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.set_metadata({"title": title})
    data = doc.tobytes()
    doc.close()
    return data


def test_host_allowlist(pdf):
    assert pdf.host_allowed("https://arxiv.org/pdf/1706.03762")
    assert pdf.host_allowed("https://export.arxiv.org/abs/1")
    assert not pdf.host_allowed("http://evilarxiv.org/x.pdf")
    assert not pdf.host_allowed("http://qdrant:6333/x.pdf")
    assert not pdf.host_allowed("http://169.254.169.254/latest")


def test_convert_bytes(pdf):
    out = pdf.convert_bytes(make_pdf(), "pdf", "fallback.pdf")
    assert out["title"] == "My Paper"
    assert "Hello pipeline" in out["fullText"]


def test_endpoints(pdf):
    from fastapi.testclient import TestClient
    client = TestClient(pdf.app)
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/convert-arxiv", json={"url": "http://qdrant:6333/x.pdf"})
    assert r.status_code == 403
    r = client.post("/convert-file", files={"file": ("a.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 200 and "Hello pipeline" in r.json()["fullText"]
    r = client.post("/convert-file", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 400
    r = client.post("/convert-file", files={"file": ("big.pdf", b"0" * (1024 * 1024 + 10), "application/pdf")})
    assert r.status_code == 413
