#!/bin/bash
# Continue VSIX 分片下载器 —— 应对市场源限速/断连
# 每片 1MB，逐片下载并校验大小，断点续传

URL="https://marketplace.visualstudio.com/_apis/public/gallery/publishers/Continue/vsextensions/continue/latest/vspackage"
DEST="/d/Program/AI_Model_agent/AI_Model/downloads/continue_full.vsix"
TMP="/d/Program/AI_Model_agent/AI_Model/downloads/continue_parts"
TOTAL=113176565
CHUNK=1048576
PARTS=$(( (TOTAL + CHUNK - 1) / CHUNK ))

mkdir -p "$TMP"
: > "$DEST"

echo "Total parts: $PARTS (each ${CHUNK} bytes)"
for i in $(seq 0 $((PARTS - 1))); do
  start=$((i * CHUNK))
  end=$((start + CHUNK - 1))
  [ "$end" -ge "$TOTAL" ] && end=$((TOTAL - 1))
  expect=$((end - start + 1))
  f="$TMP/part_$(printf %03d "$i")"

  if [ -f "$f" ] && [ "$(stat -c%s "$f" 2>/dev/null)" = "$expect" ]; then
    cat "$f" >> "$DEST"
    continue
  fi

  ok=0
  for try in 1 2 3; do
    curl -s -m 40 -r "${start}-${end}" -o "$f" "$URL" 2>/dev/null
    got=$(stat -c%s "$f" 2>/dev/null || echo 0)
    if [ "$got" = "$expect" ]; then ok=1; break; fi
    sleep 1
  done

  if [ "$ok" = 1 ]; then
    cat "$f" >> "$DEST"
  else
    echo "FAILED part $i (got $got / $expect)"
    exit 1
  fi

  n=$((i + 1))
  if [ $((n % 10)) -eq 0 ] || [ "$n" -eq "$PARTS" ]; then
    echo "Progress: $n/$PARTS parts ($((n * 100 / PARTS))%) - $(stat -c%s "$DEST") bytes"
  fi
done

echo "DONE: $(stat -c%s "$DEST") bytes"
unzip -t "$DEST" 2>&1 | tail -2
