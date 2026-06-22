# 录音降噪 + 中文转写 + 说话人区分

一套把"系统本底噪声 + 人声"的录音清理成干净人声、再转成带说话人标签的中文文字稿的流水线。

针对的具体场景：录音里混有一个**精确 ~4 Hz 的周期性"波波声"**（噪声幅度脉动），普通降噪去不掉。本工具用参考底噪建模 + 梳状脉冲跟踪专门解决它。

---

## 目录结构

```
audio-denoise-transcribe/
├── README.md
├── requirements.txt
├── run_all.sh          # 一键跑完整流程
├── denoise.py          # 第1步：参考底噪降噪 (COMBFIX)
├── transcribe.py       # 第2步：mlx-whisper 中文转写
├── diarize.py          # 第3步：声纹聚类分说话人 + 输出txt
└── analyze_noise.py    # (可选) 分析底噪频谱与4Hz脉冲, 用于调参/排查
```

你的输入需要两个目录：

```
reference/   纯系统本底噪声 (没有人声)        —— 用来学习噪声特征
human/       同样的系统噪声 + 真实人声         —— 待处理的录音
```

---

## 安装

```bash
# 系统依赖
brew install ffmpeg                 # macOS

# Python 依赖
pip3 install -r requirements.txt
```

> ⚠️ `transcribe.py` 用的 `mlx-whisper` 仅支持 **Apple Silicon (M 系列) Mac**。
> 其它平台可把转写换成 `faster-whisper` / `openai-whisper`（接口类似，自行改 `transcribe.py`）。

---

## 快速开始

```bash
cd audio-denoise-transcribe
./run_all.sh            # 默认读取 ./reference 和 ./human
# 或指定目录：
./run_all.sh /path/to/reference /path/to/human
```

产出：
- `denoised/` —— 降噪后的 mp3，文件名带源文件修改时间：`<原名>_denoised_<YYYYMMDD-HHMMSS>.mp3`
- `transcripts.json` —— 转写中间缓存（可续跑）
- `transcripts/` —— 每个录音一份 `.txt`，含时间戳与说话人标签（简体）

---

## 分步使用

### 1. 降噪 — `denoise.py`
```bash
python3 denoise.py --ref reference --in human --out denoised
```
常用参数：
| 参数 | 默认 | 说明 |
|------|------|------|
| `--ov` | `2.4` | 压噪力度，越大越狠（太大人声会失真） |
| `--beta` | `0.06` | 残留底噪地板，越小越干净（太小易有音乐噪声） |
| `--hp` | `90` | 高通截止频率(Hz)，去低频隆隆声；`0` 关闭 |
| `--bitrate` | `64k` | 输出 mp3 码率 |

### 2. 转写 — `transcribe.py`
```bash
python3 transcribe.py --in denoised --cache transcripts.json
# 只测前3个: --limit 3
```
结果缓存到 JSON，中断后重跑会跳过已完成文件。

### 3. 说话人区分 — `diarize.py`
```bash
python3 diarize.py --audio denoised --cache transcripts.json --out transcripts --speakers auto
# 已知是两人对话, 固定更稳:
python3 diarize.py --speakers 2
```

---

## 算法说明

### 降噪 (COMBFIX) — 为什么能去掉 4Hz 波波声
1. **噪声谱建模**：对所有 `reference` 求平均功率谱 `NPSD(f)`。
2. **4Hz 脉冲跟踪**：实测发现该脉冲在所有频段**同步相干**（相关系数 0.99+），即一个统一的乘性包络 `m(t)` 在调制整段噪声。
   人声的音节调制是 2–8 Hz 的**宽带、不规则**能量，不会集中在 4.00 Hz 这条尖锐谱线上。
   因此用**梳状滤波**只从功率包络里取 4Hz 及其谐波，重建噪声的逐时刻电平 `s(t)`，
   得到随时间变化的噪声估计 `NPSD(f)·s(t)` —— 只跟噪声脉冲走，几乎不碰人声。
3. **MMSE-LSA 增益**（Ephraim-Malah 对数谱）：按实时信噪比给柔性增益，而非硬门限，保住微弱人声。
4. **恒定底噪地板**：纯噪声段输出压到正比于 `√NPSD` 的固定电平（不随时间变），
   消除最后那点随脉冲漏出的残留，使底噪平稳不脉动。
5. **90 Hz 高通**：去掉人声频段以下的低频隆隆声。

> 调参经验：先动 `--ov`（噪声多→调大），人声发闷/失真→调小；再用 `--beta` 微调底噪干净度。

### 转写
- 模型 `whisper-large-v3-turbo`，中文，`condition_on_previous_text=False` + 压缩比/无语音概率阈值降低幻觉；
- 额外后处理过滤掉"数字串/单字重复"等典型幻觉段；
- 模型常输出繁体，最终在 `diarize.py` 用 OpenCC 统一转简体。

### 说话人区分
- `resemblyzer` 声纹编码器对每个片段算 256 维嵌入；
- 全局层次聚类（余弦距离），`auto` 模式按轮廓系数在候选人数里选最优；
- 按全程首次出场顺序编号为 `说话人1/2/…`。

**已知局限**：录音采样率低（11 kHz）、降噪会损伤声纹细节、短片段多 —— 说话人标签为"尽力而为"，
局部可能错分。若实质是两人对话，`--speakers 2` 通常比 `auto` 更稳。

---

## 可选：底噪分析

```bash
python3 analyze_noise.py reference/<某个文件>.mp3
```
打印底噪的低频谱、各频带能量占比、以及 4Hz 包络调制峰，便于确认问题/调参。
