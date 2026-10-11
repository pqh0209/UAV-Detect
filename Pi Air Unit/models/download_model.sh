#!/bin/bash
# Tải model SSD MobileNet V2 COCO đã biên dịch cho Edge TPU (Google Coral, Apache-2.0).
# labelmap.txt (COCO, dòng đầu "???") đã có sẵn trong repo.
set -euo pipefail
cd "$(dirname "$0")"
URL=https://dl.google.com/coral/canned_models/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite
SHA256=1df6f563746346d656c58aa2ae801b32f2a29e4e4adee46a65aeac6decc5f887
[ -f edgetpu.tflite ] || curl -fL -o edgetpu.tflite "$URL"
echo "$SHA256  edgetpu.tflite" | sha256sum -c -
