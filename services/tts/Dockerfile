FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /podcasts /models && chown app /podcasts

USER 10001
ENV PODCAST_DIR=/podcasts KOKORO_MODEL=/models/kokoro-v1.0.onnx KOKORO_VOICES=/models/voices-v1.0.bin \
    HOME=/tmp
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4)" || exit 1
# HOME=/tmp: gunicorn >= 26 creates its control socket under $HOME (root fs is read-only)
# One process on purpose: the job queue and the feed lock live in memory.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "120", "--bind", "0.0.0.0:5000", "app:app"]
