#!/usr/bin/env python3
"""
transcribe.py — 用 mlx-whisper 把降噪后的录音转成文字 (中文)

- 模型: mlx-community/whisper-large-v3-turbo (Apple Silicon, 本地离线)
- 带抗幻觉过滤: 去掉非人声段产生的数字串/重复字等假识别
- 结果缓存到 JSON, 可中断续跑 (已转写的文件跳过)
- 输出仍可能是繁体, 由 diarize.py 在写 txt 时统一转简体

用法
----
    python3 transcribe.py --in denoised --cache transcripts.json
    python3 transcribe.py --in denoised --limit 3      # 只测前3个

依赖：mlx-whisper (pip install mlx-whisper) + 系统 ffmpeg
"""
import argparse, json, glob, os, mlx_whisper

REPO = "mlx-community/whisper-large-v3-turbo"
PROMPT = "以下是普通话的对话录音转写，请使用简体中文。"


def is_halluc(s):
    """判断一个片段是否为 whisper 在非人声段的幻觉"""
    t = s["text"].strip()
    if not t:
        return True
    toks = t.split()
    if len(toks) >= 8 and len(set(toks)) <= 2:          # "6 6 6 6 ..."
        return True
    cc = t.replace(" ", "")
    if len(cc) >= 10 and len(set(cc)) <= 2:             # "啊啊啊啊..."
        return True
    if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.7:
        return True
    if s.get("compression_ratio", 0) > 2.6:
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="mlx-whisper 中文转写")
    ap.add_argument('--in', dest='inp', default='denoised', help='输入音频目录')
    ap.add_argument('--cache', default='transcripts.json', help='结果缓存JSON')
    ap.add_argument('--limit', type=int, default=0, help='只处理前N个 (0=全部)')
    ap.add_argument('--repo', default=REPO, help='whisper模型repo')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.inp, '*.*')))
    if args.limit:
        files = files[:args.limit]
    out = json.load(open(args.cache)) if os.path.exists(args.cache) else {}

    for i, f in enumerate(files, 1):
        key = os.path.basename(f)
        if key in out:
            print(f"[{i}/{len(files)}] 已缓存 {key}", flush=True)
            continue
        r = mlx_whisper.transcribe(
            f, path_or_hf_repo=args.repo, language="zh", initial_prompt=PROMPT,
            condition_on_previous_text=False, no_speech_threshold=0.6,
            compression_ratio_threshold=2.4, logprob_threshold=-1.0, verbose=False)
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                 "no_speech_prob": s.get("no_speech_prob", 0),
                 "avg_logprob": s.get("avg_logprob", 0),
                 "compression_ratio": s.get("compression_ratio", 0)} for s in r["segments"]]
        segs = [s for s in segs if not is_halluc(s)]
        out[key] = segs
        json.dump(out, open(args.cache, 'w'), ensure_ascii=False)
        print(f"[{i}/{len(files)}] {key}  {len(segs)} 段", flush=True)
    print("ALL_DONE", flush=True)


if __name__ == '__main__':
    main()
