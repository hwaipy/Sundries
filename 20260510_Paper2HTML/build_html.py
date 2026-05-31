#!/usr/bin/env python3
"""Convert MinerU markdown output to a mobile-friendly HTML with MathJax.

Usage:
    python3 build_html.py [paper_dir]              # auto-detect from mineru_out/
    python3 build_html.py [paper_dir] --title "X"  # override title

`paper_dir` defaults to the current working directory. It must contain a
`mineru_out/<stem>/auto/<stem>.md` produced by MinerU. The script writes
`<paper_dir>/<stem>.html` and copies `mineru_out/.../auto/images/` to
`<paper_dir>/images/`.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path
from html import escape


def find_md(paper_dir: Path) -> tuple[Path, str]:
    mineru_root = paper_dir / "mineru_out"
    if not mineru_root.is_dir():
        sys.exit(f"error: {mineru_root} not found. Run MinerU first.")
    candidates = [d for d in mineru_root.iterdir() if d.is_dir()]
    if not candidates:
        sys.exit(f"error: no subdirs under {mineru_root}")
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        sys.exit(f"error: multiple papers under mineru_out ({names}); pass --stem")
    stem = candidates[0].name
    md_file = mineru_root / stem / "auto" / f"{stem}.md"
    if not md_file.is_file():
        sys.exit(f"error: {md_file} not found")
    return md_file, stem


def first_h1(text: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


# ---------- Reference parsing & citation linking ----------
REF_HEAD_RE = re.compile(r"^#{1,6}\s+REFERENCES?\s*$", re.IGNORECASE | re.MULTILINE)
DOI_RE = re.compile(r"doi:\s*(?:https?://(?:dx\.)?doi\.org/)?(10\.\S+)", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")
REF_HEAD_PARTS_RE = re.compile(r"^(.*?)\((\d{4}[a-z]?)\)\.\s*(.*)$", re.DOTALL)


def parse_reference(text: str) -> tuple[str, str, str] | None:
    """Return (first_surname_lower, year, doi_or_url) or None."""
    m = REF_HEAD_PARTS_RE.match(text)
    if not m:
        return None
    authors_part, year, _rest = m.group(1).strip(), m.group(2), m.group(3)
    # First surname: the comma-separated chunks of authors_part start with the first surname
    first = authors_part.split(",", 1)[0].strip()
    surname = first.split()[0] if first else first
    surname = surname.rstrip(".,;:")
    doi_m = DOI_RE.search(text)
    if doi_m:
        url = "https://doi.org/" + doi_m.group(1).rstrip(".,;)")
    else:
        url_m = URL_RE.search(text)
        if url_m:
            url = url_m.group(0).rstrip(".,;)")
        else:
            # Fallback: Google Scholar search of the whole reference
            from urllib.parse import quote
            url = "https://scholar.google.com/scholar?q=" + quote(text[:300])
    return surname.lower(), year, url


# Citation pattern: surname(s) followed by parenthesised year list.
# Matches: "Author et al. (YYYY[a])", "Author and Author (YYYY)", "Author (YYYY)",
#          and "Author et al. (YYYY, YYYY)" / "Author (YYYY, YYYY)".
CITE_RE = re.compile(
    r"\b([A-Z][A-Za-z'\-]+)"            # surname1
    r"(?:\s+and\s+([A-Z][A-Za-z'\-]+))?" # optional "and surname2"
    r"(\s+et\s+al\.)?"                  # optional "et al."
    r"\s+\((\d{4}[a-z]?(?:,\s*\d{4}[a-z]?)*)\)"
)


def link_citations(s: str, ref_lookup: dict[tuple[str, str], str]) -> str:
    """Wrap year tokens inside in-text citations with anchors. Operates on already-escaped HTML."""
    def repl(m: re.Match) -> str:
        surname1 = m.group(1)
        years_str = m.group(4)
        # The whole match minus the year-list parens stays as-is; we only rewrite "(YYYY, YYYY)".
        prefix = m.group(0)[: m.group(0).rfind("(") + 1]
        # Build linked years
        parts = []
        any_linked = False
        for token in re.split(r"(,\s*)", years_str):
            if re.fullmatch(r"\d{4}[a-z]?", token):
                key = (surname1.lower(), token)
                ref_id = ref_lookup.get(key)
                if ref_id is None and token[-1].isalpha():
                    # Try without disambiguation letter
                    ref_id = ref_lookup.get((surname1.lower(), token[:-1]))
                if ref_id:
                    parts.append(f'<a class="cite" href="#{ref_id}" data-ref="{ref_id}">{token}</a>')
                    any_linked = True
                else:
                    parts.append(token)
            else:
                parts.append(token)
        if not any_linked:
            return m.group(0)
        return prefix + "".join(parts) + ")"
    return CITE_RE.sub(repl, s)


def build(paper_dir: Path, title_override: str | None = None) -> None:
    md_file, stem = find_md(paper_dir)
    md_dir = md_file.parent
    out_html = paper_dir / f"{stem}.html"
    out_img_dir = paper_dir / "images"

    text = md_file.read_text(encoding="utf-8")
    title = title_override or first_h1(text) or stem

    # Copy images alongside the HTML so relative src paths resolve.
    src_img = md_dir / "images"
    if out_img_dir.exists():
        shutil.rmtree(out_img_dir)
    if src_img.is_dir():
        shutil.copytree(src_img, out_img_dir)

    # Pull out display-math blocks first so paragraph splitting doesn't break them.
    DISPLAY_MATH = re.compile(r"\$\$\s*([\s\S]*?)\s*\$\$", re.MULTILINE)
    placeholders: list[str] = []

    def stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00MATH{len(placeholders) - 1}\x00"

    text = DISPLAY_MATH.sub(stash, text)

    IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def render_inline(s: str) -> str:
        inline_math = re.compile(r"\$[^$\n]+\$")
        spans: list[str] = []

        def stash_inline(m: re.Match) -> str:
            spans.append(m.group(0))
            return f"\x01M{len(spans) - 1}\x01"

        s = inline_math.sub(stash_inline, s)
        s = escape(s, quote=False)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\x01M(\d+)\x01", lambda m: spans[int(m.group(1))], s)
        return s

    blocks = re.split(r"\n{2,}", text.strip())

    # First pass: locate REFERENCES heading, parse following paragraphs as references.
    refs_start_idx: int | None = None
    for i, blk in enumerate(blocks):
        if re.match(r"^#{1,6}\s+REFERENCES?\s*$", blk.strip(), re.IGNORECASE):
            refs_start_idx = i
            break

    ref_lookup: dict[tuple[str, str], str] = {}
    if refs_start_idx is not None:
        ref_n = 0
        for blk in blocks[refs_start_idx + 1 :]:
            blk_clean = blk.strip()
            if not blk_clean or blk_clean.startswith("#"):
                continue
            parsed = parse_reference(blk_clean)
            if parsed is None:
                continue
            surname, year, _url = parsed
            ref_id = f"ref-{ref_n}"
            ref_lookup[(surname, year)] = ref_id
            ref_n += 1

    out_parts: list[str] = []
    in_refs = False
    ref_n = 0
    for i, blk in enumerate(blocks):
        blk = blk.strip()
        if not blk:
            continue

        pm = re.fullmatch(r"\x00MATH(\d+)\x00", blk)
        if pm:
            out_parts.append(f'<div class="display-math">{placeholders[int(pm.group(1))]}</div>')
            continue

        h = re.match(r"^(#{1,6})\s+(.*)$", blk)
        if h:
            level = len(h.group(1))
            out_parts.append(f"<h{level}>{render_inline(h.group(2))}</h{level}>")
            if i == refs_start_idx:
                in_refs = True
            elif in_refs and level <= 2:
                # New top-level heading after refs ends the refs section
                in_refs = False
            continue

        if IMG.search(blk):
            def img_repl(m: re.Match) -> str:
                return f'<img src="{escape(m.group(2))}" alt="{escape(m.group(1))}">'

            rendered = IMG.sub(img_repl, blk)
            only_imgs = re.fullmatch(r"(\s*<img[^>]*>\s*)+", rendered)
            if only_imgs:
                out_parts.append(f'<div class="figure">{rendered.strip()}</div>')
            else:
                parts = re.split(r"(<img[^>]*>)", rendered)
                rendered_parts = []
                for part in parts:
                    if part.startswith("<img"):
                        rendered_parts.append(part)
                    else:
                        part_text = re.sub(
                            r"\x00MATH(\d+)\x00",
                            lambda m: placeholders[int(m.group(1))],
                            part,
                        )
                        rendered_parts.append(render_inline(part_text))
                out_parts.append(f'<div class="figure">{"".join(rendered_parts)}</div>')
            continue

        blk_restored = re.sub(
            r"\x00MATH(\d+)\x00", lambda m: placeholders[int(m.group(1))], blk
        )

        if in_refs:
            parsed = parse_reference(blk_restored)
            if parsed is not None:
                _surname, _year, url = parsed
                ref_id = f"ref-{ref_n}"
                ref_n += 1
                paragraph = render_inline(blk_restored).replace("\n", " ")
                out_parts.append(
                    f'<p id="{ref_id}" class="reference" data-url="{escape(url)}">{paragraph}</p>'
                )
                continue
            # Not parseable — emit as plain paragraph
            paragraph = render_inline(blk_restored).replace("\n", "<br>\n")
            out_parts.append(f"<p>{paragraph}</p>")
            continue

        paragraph = render_inline(blk_restored).replace("\n", "<br>\n")
        # Link in-text citations (only outside refs section, and only on text nodes — safe because
        # the paragraph contains escaped text with optional <strong>/<em>/inline math; the citation
        # pattern won't match inside MathJax delimiters since $...$ contains $ characters that
        # break the surname pattern, and the inline_math placeholders were already substituted.
        if ref_lookup:
            paragraph = link_citations(paragraph, ref_lookup)
        out_parts.append(f"<p>{paragraph}</p>")

    body = "\n\n".join(out_parts)

    template = TEMPLATE.format(title=escape(title), body=body)
    out_html.write_text(template, encoding="utf-8")
    img_count = sum(1 for _ in out_img_dir.iterdir()) if out_img_dir.is_dir() else 0
    print(f"wrote {out_html} ({out_html.stat().st_size} bytes)")
    print(f"images at {out_img_dir} ({img_count} files)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>
  MathJax = {{
    tex: {{
      inlineMath: [['$', '$'], ['\\(', '\\)']],
      displayMath: [['$$', '$$'], ['\\[', '\\]']],
      tags: 'ams'
    }},
    svg: {{ fontCache: 'global' }}
  }};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" async></script>
<style>
  :root {{
    --fg: #1a1a1a;
    --muted: #555;
    --accent: #2c5aa0;
    --bg: #fafafa;
    --card: #ffffff;
    --border: #e2e2e2;
    --base-font: 17px;
  }}
  html {{ scroll-behavior: smooth; }}
  body {{
    font-family: "Charter", "Georgia", "Times New Roman", serif;
    max-width: 880px;
    margin: 0 auto;
    padding: 2.5em 2em 5em;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.7;
    font-size: var(--base-font);
  }}
  .font-toolbar {{
    position: fixed;
    right: 16px;
    bottom: 16px;
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 4px 6px 4px 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    z-index: 1000;
    font-family: -apple-system, "Helvetica Neue", sans-serif;
  }}
  .font-toolbar .progress {{
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    min-width: 36px;
    text-align: right;
    margin-right: 4px;
  }}
  .font-toolbar button {{
    border: none;
    background: transparent;
    color: var(--accent);
    width: 36px;
    height: 36px;
    border-radius: 999px;
    cursor: pointer;
    font-weight: 600;
    line-height: 1;
    padding: 0;
  }}
  .font-toolbar button:hover {{ background: #eef2f9; }}
  .font-toolbar button:active {{ background: #dde6f4; }}
  .font-toolbar .small {{ font-size: 13px; }}
  .font-toolbar .large {{ font-size: 19px; }}
  .font-toolbar .readout {{
    font-size: 13px; color: var(--muted); min-width: 44px;
    font-variant-numeric: tabular-nums;
  }}
  h1 {{
    font-size: 1.9em; line-height: 1.25; color: var(--accent);
    border-bottom: 2px solid var(--accent); padding-bottom: 0.35em; margin-top: 0;
  }}
  h2 {{
    font-size: 1.45em; color: var(--accent); margin-top: 2em;
    border-bottom: 1px solid var(--border); padding-bottom: 0.2em;
  }}
  h3 {{ font-size: 1.15em; color: #1f3f70; margin-top: 1.6em; }}
  p {{ margin: 0.8em 0; text-align: justify; overflow-wrap: anywhere; word-break: break-word; }}
  strong {{ color: #000; }}
  em {{ color: #333; }}
  .figure {{
    margin: 1.5em 0; text-align: center;
    background: #f0f4fa; border: 1px solid #c7d4e8; border-radius: 6px;
    padding: 0.8em;
  }}
  .figure img {{ max-width: 100%; height: auto; display: inline-block; margin: 0.4em 0; }}
  .display-math {{
    overflow-x: auto; overflow-y: hidden; max-width: 100%; padding: 0.3em 0;
    margin: 1em 0;
  }}
  mjx-container[display="true"] {{
    overflow-x: auto; overflow-y: hidden; max-width: 100%; padding: 0.3em 0;
  }}
  a.cite {{
    color: var(--accent);
    text-decoration: none;
    border-bottom: 1px dotted var(--accent);
    cursor: pointer;
    padding: 0 1px;
  }}
  a.cite:hover, a.cite.active {{ background: #e8efff; border-bottom-style: solid; }}
  p.reference {{
    font-size: 0.9em;
    margin: 0.6em 0;
    padding: 0.4em 0.6em;
    border-radius: 4px;
    transition: background-color 0.4s;
  }}
  p.reference.flash {{ background: #fff3c4; }}
  .cite-popup {{
    position: absolute;
    z-index: 2000;
    max-width: 360px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.18);
    padding: 0.8em 1em 0.7em;
    font-size: 0.92em;
    line-height: 1.5;
    color: var(--fg);
  }}
  .cite-popup .cite-text {{ margin-bottom: 0.6em; }}
  .cite-popup .cite-actions {{
    display: flex; justify-content: space-between; align-items: center;
    border-top: 1px solid var(--border); padding-top: 0.5em;
    font-size: 0.88em;
  }}
  .cite-popup a.cite-open {{
    color: var(--accent); text-decoration: none; font-weight: 600;
  }}
  .cite-popup a.cite-open:hover {{ text-decoration: underline; }}
  .cite-popup .cite-hint {{ color: var(--muted); font-size: 0.85em; }}
  @media (max-width: 768px) {{
    :root {{ --base-font: 18px; }}
    body {{ padding: 1.2em 0.9em 3em; line-height: 1.65; }}
    h1 {{ font-size: 1.7em; }}
    h2 {{ font-size: 1.35em; }}
    h3 {{ font-size: 1.15em; }}
    .font-toolbar {{ right: 10px; bottom: 10px; }}
    .cite-popup {{ max-width: calc(100vw - 24px); }}
  }}
  @media print {{
    body {{ background: white; max-width: none; padding: 1em; }}
    .display-math, mjx-container[display="true"] {{ overflow: visible; }}
    .font-toolbar, .cite-popup {{ display: none; }}
    a.cite {{ color: inherit; border-bottom: none; }}
  }}
</style>
</head>
<body>
<div class="font-toolbar" role="toolbar" aria-label="Reading controls">
  <span class="progress" aria-label="Reading progress">0%</span>
  <button class="small" type="button" data-action="dec" aria-label="Decrease font size" title="Decrease font">A−</button>
  <button class="readout" type="button" data-action="reset" aria-label="Reset font size" title="Click to reset">17px</button>
  <button class="large" type="button" data-action="inc" aria-label="Increase font size" title="Increase font">A+</button>
</div>
{body}
<script>
(function() {{
  const root = document.documentElement;
  const readout = document.querySelector('.font-toolbar .readout');
  const KEY = 'manuscriptFontSize';
  const MIN = 12, MAX = 32, STEP = 1;
  const mq = window.matchMedia('(max-width: 768px)');
  function defaultSize() {{ return mq.matches ? 18 : 17; }}
  function current() {{
    const stored = parseInt(localStorage.getItem(KEY), 10);
    if (Number.isFinite(stored)) return stored;
    return defaultSize();
  }}
  function updateReadout() {{
    if (readout) readout.textContent = current() + 'px';
  }}
  function apply(px) {{
    px = Math.max(MIN, Math.min(MAX, px));
    root.style.setProperty('--base-font', px + 'px');
    localStorage.setItem(KEY, String(px));
    updateReadout();
  }}
  function reset() {{
    localStorage.removeItem(KEY);
    root.style.removeProperty('--base-font');
    updateReadout();
  }}
  const stored = parseInt(localStorage.getItem(KEY), 10);
  if (Number.isFinite(stored)) root.style.setProperty('--base-font', stored + 'px');
  updateReadout();
  mq.addEventListener('change', updateReadout);

  document.querySelectorAll('.font-toolbar button').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const action = btn.dataset.action;
      if (action === 'inc') apply(current() + STEP);
      else if (action === 'dec') apply(current() - STEP);
      else if (action === 'reset') reset();
    }});
  }});
}})();

// --- Reading-progress persistence + percent indicator (single device, localStorage) ---
(function() {{
  const KEY = 'readScroll:' + location.pathname;
  const progressEl = document.querySelector('.font-toolbar .progress');
  let saveTimer = null;

  function maxScroll() {{
    return Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  }}
  function ratioNow() {{
    const max = maxScroll();
    return max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
  }}
  function updateProgress() {{
    if (progressEl) progressEl.textContent = Math.round(ratioNow() * 100) + '%';
  }}
  function save() {{
    const max = maxScroll();
    if (max <= 0) return;
    const payload = {{ y: window.scrollY, max, t: Date.now() }};
    try {{ localStorage.setItem(KEY, JSON.stringify(payload)); }} catch (e) {{}}
  }}
  function scheduleSave() {{
    updateProgress();
    if (saveTimer) return;
    saveTimer = setTimeout(() => {{ saveTimer = null; save(); }}, 500);
  }}
  function restore() {{
    let raw;
    try {{ raw = localStorage.getItem(KEY); }} catch (e) {{ return; }}
    if (!raw) {{ updateProgress(); return; }}
    let data;
    try {{ data = JSON.parse(raw); }} catch (e) {{ updateProgress(); return; }}
    const max = maxScroll();
    if (!Number.isFinite(data.y) || max <= 0) {{ updateProgress(); return; }}
    const ratio = data.max > 0 ? data.y / data.max : 0;
    const target = Math.min(max, Math.round(ratio * max));
    window.scrollTo({{ top: target, behavior: 'auto' }});
    updateProgress();
  }}
  function whenReady(cb) {{
    if (window.MathJax && MathJax.startup && MathJax.startup.promise) {{
      MathJax.startup.promise.then(cb).catch(cb);
    }} else {{
      window.addEventListener('load', () => setTimeout(cb, 300));
    }}
  }}

  whenReady(restore);
  window.addEventListener('scroll', scheduleSave, {{ passive: true }});
  window.addEventListener('resize', updateProgress);
  window.addEventListener('beforeunload', save);
  document.querySelectorAll('.font-toolbar button').forEach(btn => {{
    btn.addEventListener('click', () => setTimeout(() => {{ updateProgress(); save(); }}, 100));
  }});
}})();

// --- Citation popup: first click shows reference, second click opens original ---
(function() {{
  let popup = null;
  let activeCite = null;

  function closePopup() {{
    if (popup) {{ popup.remove(); popup = null; }}
    if (activeCite) {{ activeCite.classList.remove('active'); activeCite = null; }}
  }}

  function showPopup(cite) {{
    closePopup();
    const refId = cite.dataset.ref;
    const ref = document.getElementById(refId);
    if (!ref) return;
    const url = ref.dataset.url || '';
    popup = document.createElement('div');
    popup.className = 'cite-popup';
    popup.setAttribute('role', 'dialog');
    const text = document.createElement('div');
    text.className = 'cite-text';
    text.textContent = ref.textContent.trim();
    popup.appendChild(text);
    const actions = document.createElement('div');
    actions.className = 'cite-actions';
    if (url) {{
      const open = document.createElement('a');
      open.className = 'cite-open';
      open.href = url;
      open.target = '_blank';
      open.rel = 'noopener';
      open.textContent = 'Open original ↗';
      actions.appendChild(open);
    }} else {{
      const noUrl = document.createElement('span');
      noUrl.className = 'cite-hint';
      noUrl.textContent = 'No DOI/URL';
      actions.appendChild(noUrl);
    }}
    const hint = document.createElement('span');
    hint.className = 'cite-hint';
    hint.textContent = url ? 'tap citation again to open' : '';
    actions.appendChild(hint);
    popup.appendChild(actions);
    document.body.appendChild(popup);
    // Position below the citation; flip above if it would overflow viewport.
    const rect = cite.getBoundingClientRect();
    const popRect = popup.getBoundingClientRect();
    const margin = 8;
    let top = rect.bottom + window.scrollY + 6;
    let left = rect.left + window.scrollX;
    if (left + popRect.width > window.scrollX + document.documentElement.clientWidth - margin) {{
      left = window.scrollX + document.documentElement.clientWidth - popRect.width - margin;
    }}
    if (left < window.scrollX + margin) left = window.scrollX + margin;
    if (rect.bottom + popRect.height + 12 > window.innerHeight && rect.top - popRect.height - 6 > 0) {{
      top = rect.top + window.scrollY - popRect.height - 6;
    }}
    popup.style.top = top + 'px';
    popup.style.left = left + 'px';
    cite.classList.add('active');
    activeCite = cite;
  }}

  document.querySelectorAll('a.cite').forEach(cite => {{
    cite.addEventListener('click', (e) => {{
      e.preventDefault();
      e.stopPropagation();
      if (cite === activeCite) {{
        const refId = cite.dataset.ref;
        const ref = document.getElementById(refId);
        const url = ref && ref.dataset.url;
        if (url) {{
          window.open(url, '_blank', 'noopener');
        }}
        closePopup();
      }} else {{
        showPopup(cite);
      }}
    }});
  }});

  document.addEventListener('click', (e) => {{
    if (!popup) return;
    if (e.target.closest('.cite-popup') || e.target.closest('a.cite')) return;
    closePopup();
  }});
  document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape') closePopup();
  }});
  window.addEventListener('scroll', () => {{ if (popup) closePopup(); }}, {{ passive: true }});
}})();
</script>
</body>
</html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description="MinerU markdown -> mobile-friendly HTML")
    p.add_argument("paper_dir", nargs="?", default=".", help="Paper review directory (default: cwd)")
    p.add_argument("--title", help="Override page title (default: first H1 in markdown)")
    args = p.parse_args()
    build(Path(args.paper_dir).resolve(), args.title)


if __name__ == "__main__":
    main()
