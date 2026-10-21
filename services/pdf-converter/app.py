"""Document -> Markdown conversion service (PDF, EPUB, MOBI).

POST /convert-arxiv   {"url": "https://arxiv.org/pdf/<id>"}   -> {title, fullText}
POST /convert-file    multipart field "file"                 -> {title, fullText}
GET  /health

Only hosts listed in PDF_ALLOWED_HOSTS may be fetched (default: arxiv.org and
export.arxiv.org), so the service cannot be used to reach arbitrary internal
addresses.
"""
import os
from urllib.parse import urlparse

import httpx
import pymupdf
import pymupdf4llm
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, HttpUrl

SUPPORTED_EXTENSIONS = {".pdf", ".epub", ".mobi"}
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PDF_ALLOWED_HOSTS", "arxiv.org,export.arxiv.org").split(",") if h.strip()}
MAX_BYTES = int(float(os.environ.get("PDF_MAX_MB", "100")) * 1024 * 1024)
USER_AGENT = os.environ.get("HTTP_USER_AGENT", "paper-radio/1.0 (pdf-converter)")
FETCH_TIMEOUT = float(os.environ.get("PDF_FETCH_TIMEOUT", "60"))

app = FastAPI(title="Document to Markdown converter")


class DocumentRequest(BaseModel):
    url: HttpUrl


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def filetype_and_name(identifier: str) -> tuple[str, str]:
    filename = os.path.basename(urlparse(identifier).path) or "document.pdf"
    ext = os.path.splitext(filename.lower())[1]
    return (ext.lstrip(".") if ext in SUPPORTED_EXTENSIONS else "pdf"), filename


def convert_bytes(data: bytes, filetype: str, fallback_title: str) -> dict:
    try:
        doc = pymupdf.open(stream=data, filetype=filetype)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not open {filetype.upper()}: {exc}")
    try:
        meta_title = (doc.metadata or {}).get("title") or ""
        text = pymupdf4llm.to_markdown(doc)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to extract text: {exc}")
    finally:
        doc.close()
    return {"title": meta_title.strip() or fallback_title, "fullText": text}


@app.post("/convert-arxiv")
async def convert_url(req: DocumentRequest):
    url = str(req.url)
    if not host_allowed(url):
        raise HTTPException(status_code=403, detail="Host not in PDF_ALLOWED_HOSTS")
    filetype, name = filetype_and_name(url)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT,
                                     headers={"User-Agent": USER_AGENT}) as client:
            async with client.stream("GET", url) as resp:
                if not host_allowed(str(resp.url)):
                    raise HTTPException(status_code=403, detail="Redirect left PDF_ALLOWED_HOSTS")
                resp.raise_for_status()
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(status_code=413, detail="Document larger than PDF_MAX_MB")
                    chunks.append(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch document: {exc}")
    return convert_bytes(b"".join(chunks), filetype, name)


@app.post("/convert-file")
async def convert_file(file: UploadFile = File(...)):
    name = file.filename or "document.pdf"
    ext = os.path.splitext(name.lower())[1]
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Document larger than PDF_MAX_MB")
    return convert_bytes(data, ext.lstrip("."), name)


@app.get("/health")
def health():
    return {"status": "ok"}
