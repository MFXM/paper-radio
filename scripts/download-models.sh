#!/usr/bin/env bash
# Download the Kokoro v1.0 model files (Apache-2.0) into ./models and verify them.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
mkdir -p models
fetch() {  # name sha256
  if [ -f "models/$1" ] && echo "$2  models/$1" | sha256sum -c --status; then
    echo "ok      models/$1"; return; fi
  echo "fetch   $1"; curl -fL --retry 3 -o "models/$1.part" "$BASE/$1"
  echo "$2  models/$1.part" | sha256sum -c --quiet
  mv "models/$1.part" "models/$1"
}
fetch kokoro-v1.0.onnx 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
fetch voices-v1.0.bin  bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
