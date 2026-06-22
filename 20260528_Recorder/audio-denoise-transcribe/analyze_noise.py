#!/usr/bin/env python3
"""
analyze_noise.py — 分析底噪频谱与周期性脉冲, 用于调参/排查

打印:
  - 低频(<400Hz)细节谱: 找隆隆声/工频等
  - 各频带能量占比
  - 幅度包络的调制谱(0-12Hz): 找类似 4Hz 的周期"波波声"峰

用法:
    python3 analyze_noise.py <音频文件>
依赖: numpy scipy + ffmpeg
"""
import sys, subprocess, numpy as np
from scipy.signal import welch, butter, sosfiltfilt

SR = 11025


def decode(path, sr=SR):
    p = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ar', str(sr), '-ac', '1', '-f', 'f32le', '-'],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 analyze_noise.py <音频文件>")
    x = decode(sys.argv[1])
    print(f"文件: {sys.argv[1]}  时长 {len(x)/SR:.1f}s  RMS {np.sqrt((x**2).mean()):.1f}")

    print("\n=== 各频带能量 (RMS) ===")
    for lo, hi in [(0, 30), (30, 80), (80, 300), (300, 1000), (1000, 5000)]:
        sos = butter(4, [max(lo, 1) / (SR / 2), hi / (SR / 2)], btype='band', output='sos')
        b = sosfiltfilt(sos, x)
        print(f"  {lo:4d}-{hi:4d} Hz : {np.sqrt((b**2).mean()):7.1f}")

    print("\n=== 幅度包络调制谱 0-12Hz (找周期性'波波声') ===")
    env = np.abs(x)
    sos = butter(4, 30 / (SR / 2), output='sos')
    env = sosfiltfilt(sos, env)
    env = env - env.mean()
    f, P = welch(env, SR, nperseg=SR * 8)
    floor = np.median(P[(f > 0.5) & (f < 12)])
    for i in range(len(f)):
        if f[i] > 12:
            break
        mark = '  <<< 调制峰' if P[i] > 4 * floor else ''
        print(f"  {f[i]:5.2f} Hz : {10*np.log10(P[i]+1e-12):6.1f} dB{mark}")


if __name__ == '__main__':
    main()
