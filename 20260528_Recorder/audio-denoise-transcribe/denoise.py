#!/usr/bin/env python3
"""
denoise.py — 基于参考底噪的语音降噪 (COMBFIX 方案)

适用场景
--------
你有两组录音：
  reference/  纯系统本底噪声 (没有人声)
  human/      同样的系统噪声 + 真实人声
本脚本从 reference 学习噪声谱，从 human 中减掉，显露人声。

针对的难点：本底噪声里有一个精确 ~4 Hz 的周期性"波波声"(幅度脉动)。
普通的稳态谱减无法去除它。COMBFIX 的做法：
  1. 从所有 reference 求平均噪声功率谱 (基础噪声谱)。
  2. 该 4 Hz 脉冲在所有频段同步相干 —— 是一个统一的乘性包络在调制噪声。
     用梳状滤波从每个 human 文件的功率包络里只提取 4 Hz 及其谐波，
     重建出噪声的"逐时刻电平 s(t)"，得到随时间变化的噪声估计 NPSD(f)*s(t)。
  3. 对数谱 MMSE (Ephraim-Malah) 增益做柔性抑制，按实时信噪比保留人声。
  4. 用"恒定底噪地板"(正比于 √NPSD，不随时间变) 兜底，使纯噪声段输出平稳、
     不再有 4 Hz 残留脉动。
  5. 90 Hz 高通去掉人声频段以下的低频隆隆声。

输出文件名带上源文件的修改时间：<原名>_denoised_<YYYYMMDD-HHMMSS>.mp3

用法
----
    python3 denoise.py --ref reference --in human --out denoised
可调参数：--ov (压噪力度, 默认2.4)  --beta (底噪地板高低, 默认0.06)  --hp (高通Hz, 默认90)

依赖：numpy scipy + 系统 ffmpeg/ffprobe
"""
import argparse, glob, os, subprocess, time, numpy as np
from scipy.signal import stft, istft, butter, sosfiltfilt

SR = 11025          # 处理采样率 (与源文件一致)
NP = 512            # STFT 窗长
HOP = 128           # STFT 帧移
WIN = 'hann'
FSENV = SR / HOP    # 功率包络的采样率


def decode(path, sr=SR):
    """用 ffmpeg 把任意音频解码成单声道 float64 数组"""
    p = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-ar', str(sr), '-ac', '1', '-f', 'f32le', '-'],
        capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def build_noise_profile(ref_dir):
    """从所有参考文件求平均噪声功率谱 NPSD(f)"""
    acc, cnt = None, 0
    files = sorted(glob.glob(os.path.join(ref_dir, '*.*')))
    for f in files:
        x = decode(f)
        _, _, Z = stft(x, SR, window=WIN, nperseg=NP, noverlap=NP - HOP)
        P = (np.abs(Z) ** 2).sum(1)
        acc = P if acc is None else acc + P
        cnt += (np.abs(Z) ** 2).shape[1]
    if acc is None:
        raise SystemExit(f"参考目录里没有音频文件: {ref_dir}")
    return acc / cnt, len(files)


def pulse_track(P):
    """从帧功率包络 P(t) 提取 4Hz 及其谐波的周期分量 (噪声脉冲)"""
    Pm = P - P.mean()
    nf = len(P)
    F = np.fft.rfft(Pm)
    fr = np.fft.rfftfreq(nf, 1 / FSENV)
    band = (fr > 3.4) & (fr < 4.6)
    f0 = fr[band][np.argmax(np.abs(F[band]))]   # 自动锁定 ~4Hz 基频
    mask = np.zeros_like(F)
    for k in range(1, 13):                       # 1..12 次谐波
        sel = np.abs(fr - k * f0) < 0.28
        mask[sel] = F[sel]
    return np.fft.irfft(mask, n=nf), f0


def enhance(x, NPSD, ov=2.4, beta=0.06, alpha_dd=0.98):
    SQN = np.sqrt(NPSD)
    STOT = NPSD.sum()
    _, _, Z = stft(x, SR, window=WIN, nperseg=NP, noverlap=NP - HOP)
    mag = np.abs(Z); ph = np.angle(Z); Y = mag ** 2
    nb, nf = Z.shape
    c, _ = pulse_track(Y.sum(0))
    s = np.clip((STOT + c) / STOT, 0.1, 12.0)            # 逐时刻噪声电平
    noise = NPSD[:, None] * s[None, :]                   # 随时间变化的噪声估计
    gamma = np.minimum(Y / (ov * noise), 1e3)            # 后验信噪比
    Gp = np.ones(nb); gp = np.ones(nb); Go = np.empty_like(Y)
    floor = beta * SQN                                    # 恒定输出底噪地板
    for l in range(nf):
        g = gamma[:, l]
        xi = np.maximum(alpha_dd * (Gp ** 2) * gp + (1 - alpha_dd) * np.maximum(g - 1, 0), 1e-3)
        v = xi / (1 + xi) * g
        ei = np.where(v < 0.1, -2.31 * np.log10(v) - 0.6,
             np.where(v > 1, np.exp(-v) / v * (1 - 1 / v + 2 / v ** 2),
                      -np.log(v) - 0.57722 + v - v * v / 4))
        G = np.minimum(xi / (1 + xi) * np.exp(0.5 * ei), 1.0)   # MMSE-LSA 增益
        cm = np.maximum(G * mag[:, l], floor)                   # 应用恒定地板
        Go[:, l] = cm / np.maximum(mag[:, l], 1e-9)
        Gp = Go[:, l]; gp = g
    _, y = istft(Go * mag * np.exp(1j * ph), SR, window=WIN, nperseg=NP, noverlap=NP - HOP)
    return y


def main():
    ap = argparse.ArgumentParser(description="COMBFIX 参考底噪降噪")
    ap.add_argument('--ref', default='reference', help='纯噪声参考目录')
    ap.add_argument('--in', dest='inp', default='human', help='含人声的输入目录')
    ap.add_argument('--out', default='denoised', help='输出目录')
    ap.add_argument('--ov', type=float, default=2.4, help='压噪力度 (越大越狠, 默认2.4)')
    ap.add_argument('--beta', type=float, default=0.06, help='底噪地板高低 (越小越干净, 默认0.06)')
    ap.add_argument('--hp', type=float, default=90.0, help='高通截止频率Hz, 0=关闭 (默认90)')
    ap.add_argument('--bitrate', default='64k', help='输出mp3码率 (默认64k)')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print(f"[1/2] 从 {args.ref} 构建噪声谱 ...")
    NPSD, nref = build_noise_profile(args.ref)
    print(f"      使用了 {nref} 个参考文件")

    hp = butter(4, args.hp / (SR / 2), btype='high', output='sos') if args.hp > 0 else None
    files = sorted(glob.glob(os.path.join(args.inp, '*.*')))
    print(f"[2/2] 处理 {len(files)} 个输入文件 ...")
    for i, f in enumerate(files, 1):
        x = decode(f)
        if hp is not None:
            x = sosfiltfilt(hp, x)
        y = enhance(x, NPSD, ov=args.ov, beta=args.beta)
        y = y / (np.abs(y).max() + 1e-9) * 0.97 * 32767      # 归一化防削顶
        # 临时 wav -> mp3, 文件名带源文件修改时间
        base = os.path.splitext(os.path.basename(f))[0]
        ts = time.strftime('%Y%m%d-%H%M%S', time.localtime(os.path.getmtime(f)))
        out_mp3 = os.path.join(args.out, f"{base}_denoised_{ts}.mp3")
        tmp = os.path.join(args.out, f".{base}.tmp.wav")
        import scipy.io.wavfile as wav
        wav.write(tmp, SR, y.astype(np.int16))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', tmp,
                        '-ar', str(SR), '-ac', '1', '-b:a', args.bitrate, out_mp3], check=True)
        os.remove(tmp)
        print(f"  [{i}/{len(files)}] {os.path.basename(out_mp3)}")
    print(f"完成 -> {args.out}/")


if __name__ == '__main__':
    main()
