#!/bin/bash
# Z-Image-Turbo 生图模型下载（ModelScope 国内高速源，支持断点续传）
BASE="https://www.modelscope.cn/models/Comfy-Org/Z-Image-Turbo/resolve/master/split_files"
DEST="/d/Program/AI_Model_agent/AI_Model/ComfyUI/models"

download() {
  local url="$1"
  local dest="$2"
  local name="$3"
  local expect="$4"
  echo "=== 下载 $name (目标 $expect) ==="
  curl -L -C - --retry 5 --retry-delay 3 \
    -o "$dest" "$url" \
    -w "$name: %{size_download} bytes @ %{speed_download} B/s\n" 2>&1 | tail -2
  local actual=$(stat -c %s "$dest" 2>/dev/null || echo 0)
  echo "$name 现有大小: $actual bytes"
  echo ""
}

download "$BASE/vae/ae.safetensors" "$DEST/vae/ae.safetensors" "VAE" "340MB"
download "$BASE/text_encoders/qwen_3_4b_fp8_mixed.safetensors" "$DEST/text_encoders/qwen_3_4b_fp8_mixed.safetensors" "TextEncoder" "5.63GB"
download "$BASE/diffusion_models/z_image_turbo_int8_convrot.safetensors" "$DEST/diffusion_models/z_image_turbo_int8_convrot.safetensors" "Diffusion" "6.2GB"

echo "=== 最终文件大小 ==="
ls -lh "$DEST/vae/ae.safetensors" "$DEST/text_encoders/qwen_3_4b_fp8_mixed.safetensors" "$DEST/diffusion_models/z_image_turbo_int8_convrot.safetensors"
