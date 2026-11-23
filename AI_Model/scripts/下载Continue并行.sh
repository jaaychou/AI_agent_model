#!/bin/bash
# Continue VSIX 16路并行分片下载 —— 应对限速/断连
URL="https://marketplace.visualstudio.com/_apis/public/gallery/publishers/Continue/vsextensions/continue/latest/vspackage"
DEST="/d/Program/AI_Model_agent/AI_Model/downloads/continue_full.vsix"
TMP="/d/Program/AI_Model_agent/AI_Model/downloads/continue_parts"
TOTAL=113176565
CHUNK=524288
PARTS=$(( (TOTAL + CHUNK - 1) / CHUNK ))
CONC=16

mkdir -p "$TMP"
: > "$DEST"

echo "Total: $PARTS parts x ${CHUNK}B, $CONC parallel"

download_part() {
  local i=$1
  local start=$((i * CHUNK))
  local end=$((start + CHUNK - 1))
  [ "$end" -ge "$TOTAL" ] && end=$((TOTAL - 1))
  local expect=$((end - start + 1))
  local f="$TMP/part_$(printf %04d "$i")"

  [ -f "$f" ] && [ "$(stat -c%s "$f" 2>/dev/null)" = "$expect" ] && return 0

  for try in 1 2 3 4; do
    curl -s -m 60 -r "${start}-${end}" -o "$f" "$URL" 2>/dev/null
    [ "$(stat -c%s "$f" 2>/dev/null || echo 0)" = "$expect" ] && return 0
    sleep 1
  done
  return 1
}

export -f download_part
export URL TMP TOTAL CHUNK

seq 0 $((PARTS - 1)) | xargs -P "$CONC" -I {} bash -c 'download_part {}' 2>/dev/null

# 校验并合并
fail=0
for i in $(seq 0 $((PARTS - 1))); do
  start=$((i * CHUNK)); end=$((start + CHUNK - 1))
  [ "$end" -ge "$TOTAL" ] && end=$((TOTAL - 1))
  expect=$((end - start + 1))
  f="$TMP/part_$(printf %04d "$i")"
  if [ "$(stat -c%s "$f" 2>/dev/null || echo 0)" != "$expect" ]; then echo "MISSING part $i"; fail=1; break; fi
  cat "$f" >> "$DEST"
done

echo "PARTS_DONE fail=$fail size=$(stat -c%s "$DEST")"
if [ "$fail" = 0 ]; then unzip -t "$DEST" 2>&1 | tail -2; fi
