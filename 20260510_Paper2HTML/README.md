# Paper2HTML

把审稿用的论文 PDF 转成手机/桌面都好读的 HTML：MinerU 解析 → `build_html.py` 套模板。

## 目录约定

每个稿件一个子目录（如 `~/SynologyDrive/Claude/<日期 + 简述>/`）：
```
<paper_dir>/
  <paper>.PDF                  # 原始 PDF
  <paper>.html                 # 最终输出（自动生成）
  images/                      # 与 HTML 同级（自动生成）
  mineru_out/                  # MinerU 中间产物（保留备查）
  questions.md                 # 阅读中产生的问题（按主题分组）
```

## 第 1 步：MinerU 解析 PDF

已安装位置：`~/.local/bin/mineru`（pip user 安装；如未在 PATH，先 `export PATH="$HOME/.local/bin:$PATH"`）。

英文稿：
```bash
cd <paper_dir>
mineru -p "<paper>.PDF" -o mineru_out -b pipeline -l en
```
中文稿改 `-l ch`。`pipeline` backend 在 CPU 机器上能跑，首次运行下模型 ~2 GB。22 页的稿件 CPU 上约 4-5 分钟。

## 第 2 步：生成 HTML

```bash
cd <paper_dir>
python3 ~/codes/Sundries/20260510_Paper2HTML/build_html.py
```
脚本会：
- 自动找 `mineru_out/<stem>/auto/<stem>.md`（要求 `mineru_out/` 下只有一个子目录）
- 标题取 markdown 第一个 `# H1`，可用 `--title "..."` 覆盖
- 输出 `<paper_dir>/<stem>.html`，并把图片复制到 `<paper_dir>/images/`

## HTML 视图保留特性（修改时不要去掉）

- **viewport meta** + 移动端媒体查询（手机默认 18px，桌面 17px）
- **MathJax 3** + AMS tags：`$...$` 行内、`$$...$$` 行间
- 行间公式用 `mjx-container[display="true"]` + `.display-math` 横向滚动，长公式不撑宽页面
- 段落 `overflow-wrap: anywhere; word-break: break-word;`，长 DOI / URL 强制换行
- 右下角浮动工具栏：
  - **X% read** 阅读进度百分比（实时更新）
  - **A−** / 当前字号 / **A+**，中间按钮点击重置；字号选择存 `localStorage`
- **阅读进度持久化**：scroll 节流 500ms 写 `localStorage`（key=`location.pathname`），存 `{y, max}`，恢复时按比例换算（字号变了也能回到相近位置），等 `MathJax.startup.promise` 完成再恢复
- **引文链接 + 弹窗**：自动解析 `## REFERENCES` 段落（每条一段），每条带 `id="ref-N"` 和 `data-url`（DOI 优先 → 第一个 http URL → Google Scholar 搜索 fallback）。正文中识别 `Author et al. (YYYY[a/b])` / `Author and Author (YYYY)` / `Author (YYYY)` / `Author et al. (YYYY, YYYY)` 模式，把年份变成 `<a class="cite">`。点击行为：第一次显示弹窗（含完整文献文字 + Open original ↗ 按钮），第二次点同一引文则直接打开 DOI；点击外部 / Esc / 滚动会关闭弹窗
- 打印时工具栏 `display:none`，公式 `overflow:visible`

## 部署 / 分享 URL

文件在 `~/SynologyDrive/...` 下，nginx 已暴露：
- 本地 `/synology/<path>` → 公网 `https://claude.qpqi.group/files/synology/<path>`

## 阅读笔记

`questions.md` 按主题（如「噪声相关」「实验方法」「理论分析」）分组，每条一句话能描述清楚动机。
