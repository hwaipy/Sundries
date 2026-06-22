#!/usr/bin/env python3
"""
diarize.py — 说话人区分 + 写出每文件 .txt (简体中文)

流程
----
1. 读取 transcribe.py 生成的 transcripts.json。
2. 对每个转写片段, 从对应音频里切出该时间段, 用 resemblyzer 声纹编码器算 256 维嵌入。
3. 把所有片段的嵌入做"全局"层次聚类 (跨文件统一, 保证同一个人标签一致)。
   --speakers auto 时在候选数里按轮廓系数自动选最优; 也可固定 (如 --speakers 2)。
4. 聚类按"全程首次出场顺序"映射成 说话人1 / 说话人2 / ...
5. opencc 繁转简, 每个录音写一份 transcripts/<原名>.txt。

注意: 录音采样率低 / 降噪后声纹有损 / 片段短, 说话人标签为"尽力而为", 局部可能错分。
若实质是两人对话, 建议 --speakers 2 通常更稳。

用法
----
    python3 diarize.py --audio denoised --cache transcripts.json --out transcripts --speakers auto
    python3 diarize.py --speakers 2          # 固定两人

依赖：resemblyzer scikit-learn opencc-python-reimplemented numpy + 系统 ffmpeg
"""
import argparse, json, glob, os, subprocess, numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav
from sklearn.cluster import AgglomerativeClustering
from sklearn.preprocessing import normalize
from sklearn.metrics import silhouette_score
from opencc import OpenCC

cc = OpenCC('t2s')
SR = 16000      # resemblyzer 要求 16kHz


def decode16k(path):
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ar', str(SR), '-ac', '1', '-f', 'f32le', '-'],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32)


def origbase(fn):
    """20260606_0268_denoised_...mp3 -> 20260606_0268"""
    return os.path.basename(fn).split('_denoised')[0]


def mmss(t):
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def main():
    ap = argparse.ArgumentParser(description="说话人区分 + 输出txt")
    ap.add_argument('--audio', default='denoised', help='音频目录 (文件名需与cache的key一致)')
    ap.add_argument('--cache', default='transcripts.json', help='transcribe.py产出的JSON')
    ap.add_argument('--out', default='transcripts', help='txt输出目录')
    ap.add_argument('--speakers', default='auto', help="说话人数: auto 或 具体数字(如2)")
    ap.add_argument('--candidates', default='3,4', help="auto模式下的候选人数, 逗号分隔")
    args = ap.parse_args()

    tr = json.load(open(args.cache))
    files = sorted(tr.keys())
    enc = VoiceEncoder()

    embs, index = [], []   # index: (file, seg_i)
    for f in files:
        segs = tr[f]
        if not segs:
            continue
        wav = decode16k(os.path.join(args.audio, f))
        for i, s in enumerate(segs):
            a = wav[int(s['start'] * SR):int(s['end'] * SR)]
            if len(a) < int(0.25 * SR):                 # 太短的片段补上下文
                c0 = max(0, int(s['start'] * SR) - SR // 4)
                c1 = min(len(wav), int(s['end'] * SR) + SR // 4)
                a = wav[c0:c1]
            try:
                w = preprocess_wav(a, source_sr=SR)
                if len(w) < int(0.2 * SR):
                    w = a
            except Exception:
                w = a
            try:
                e = enc.embed_utterance(w)
            except Exception:
                e = np.zeros(256, dtype=np.float32)
            embs.append(e); index.append((f, i))

    X = normalize(np.array(embs))

    if args.speakers == 'auto':
        cands = [int(x) for x in args.candidates.split(',')]
        best = None
        for k in cands:
            if len(X) <= k:
                continue
            lab = AgglomerativeClustering(n_clusters=k, metric='cosine', linkage='average').fit_predict(X)
            try:
                sc = silhouette_score(X, lab, metric='cosine')
            except Exception:
                sc = -1
            print(f"  k={k} 轮廓系数={sc:.3f}")
            if best is None or sc > best[0]:
                best = (sc, k, lab)
        _, k, lab = best
        print(f"自动选定说话人数 k={k}")
    else:
        k = int(args.speakers)
        lab = AgglomerativeClustering(n_clusters=k, metric='cosine', linkage='average').fit_predict(X)
        print(f"固定说话人数 k={k}")

    # 按全程首次出场顺序给说话人编号
    first = {}
    for g, ((f, i), l) in enumerate(zip(index, lab)):
        first.setdefault(l, g)
    order = sorted(first, key=lambda l: first[l])
    spk = {l: f"说话人{n + 1}" for n, l in enumerate(order)}
    labmap = {(f, i): spk[l] for (f, i), l in zip(index, lab)}

    os.makedirs(args.out, exist_ok=True)
    for f in files:
        lines = [f"# {origbase(f)}  来源文件: {f}", ""]
        for i, s in enumerate(tr[f]):
            who = labmap.get((f, i), "说话人?")
            lines.append(f"[{mmss(s['start'])}-{mmss(s['end'])}] {who}：{cc.convert(s['text'])}")
        open(os.path.join(args.out, f"{origbase(f)}.txt"), 'w').write("\n".join(lines) + "\n")
    print(f"完成 -> 写出 {len(files)} 个 txt 到 {args.out}/  (说话人数={k})")


if __name__ == '__main__':
    main()
