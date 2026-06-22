#!/usr/bin/env bash
# 一键跑完整流程: 降噪 -> 转写 -> 说话人区分
# 用法: ./run_all.sh [reference目录] [human目录]
set -e
REF="${1:-reference}"
HUMAN="${2:-human}"

echo "==== 1/3 降噪 (COMBFIX) ===="
python3 denoise.py --ref "$REF" --in "$HUMAN" --out denoised

echo "==== 2/3 转写 (mlx-whisper, 中文) ===="
python3 transcribe.py --in denoised --cache transcripts.json

echo "==== 3/3 说话人区分 + 输出txt ===="
python3 diarize.py --audio denoised --cache transcripts.json --out transcripts --speakers auto

echo "全部完成:"
echo "  降噪音频  -> denoised/"
echo "  分说话人稿 -> transcripts/"
