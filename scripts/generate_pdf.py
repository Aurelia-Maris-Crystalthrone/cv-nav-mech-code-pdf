#!/usr/bin/env python3
"""
generate_pdf.py — Markdown 报告 → 排版精美的 PDF（中文字体 + Pygments 语法高亮）

设计思路：
  1. 所有排版参数从 assets/config.json 读取（字体、字号、边距、页眉页脚、
     高亮主题、代码字体），脚本本身零硬编码排版常量；
  2. HTML 模板 assets/report-template.html 中用 {{ 占位符 }}，脚本注入
     正文 HTML + CSS 变量后交给 xhtml2pdf 渲染；
  3. 代码块经 Pygments codehilite 扩展生成带内联样式的 HTML，
     xhtml2pdf 对 CSS 支持有限，因此用 formatter 的 nowrap + 行内色覆盖；
  4. xhtml2pdf 渲染失败时降级输出 HTML（退出码 10），保证交付物不缺席。

退出码：
  0 成功   1 参数错误   2 Markdown 源文件不存在   3 依赖缺失
  4 配置/模板/字体加载失败   5 渲染失败（已降级输出 HTML）  10 仅 HTML 降级产物
"""

import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ARGS = 1
EXIT_NO_SOURCE = 2
EXIT_NO_DEP = 3
EXIT_CONFIG = 4
EXIT_RENDER_FAIL = 5
EXIT_HTML_FALLBACK = 10

# 配置默认值（仅当 config.json 缺失时兜底；正常路径全部走 config）
DEFAULT_CONFIG = {
    "page": {"size": "A4", "margin_cm": {"top": 2.0, "bottom": 2.0,
                                          "left": 2.2, "right": 2.2}},
    "fonts": {
        "cjk": "assets/fonts/NotoSansSC-Regular.ttf",
        "cjk_bold": "assets/fonts/NotoSansSC-Bold.ttf",
        "mono": "assets/fonts/DejaVuSansMono.ttf",
        "cjk_family": "NotoSansSC",
        "mono_family": "MonoCode",
    },
    "typography": {
        "body_size": "10.5pt", "h1_size": "18pt", "h2_size": "14pt",
        "h3_size": "12pt", "code_size": "8.5pt", "line_height": 1.6,
    },
    "highlight": {"theme": "default", "noclasses": True},
    "header": {"enabled": False, "text": ""},
    "footer": {"enabled": True, "text": "cv-nav-mech-code-pdf v{{version}}"},
}

SKILL_VERSION = "1.0.0"


def eprint(*a):
    print(*a, file=sys.stderr)


def check_deps():
    missing = []
    try:
        import markdown  # noqa: F401
    except ImportError:
        missing.append("markdown")
    try:
        import pygments  # noqa: F401
    except ImportError:
        missing.append("pygments")
    try:
        import xhtml2pdf  # noqa: F401
    except ImportError:
        missing.append("xhtml2pdf")
    if missing:
        eprint(f"[generate_pdf] 依赖缺失：{', '.join(missing)}。"
               f"请执行：pip install {' '.join(missing)}")
        sys.exit(EXIT_NO_DEP)


def load_config(config_path: Path, skill_root: Path) -> dict:
    """加载 config.json；缺失时用内置默认值并警告（不中断）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if config_path.exists():
        try:
            user_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            deep_merge(cfg, user_cfg)
        except json.JSONDecodeError as exc:
            eprint(f"[generate_pdf] config.json 解析失败（{exc}），"
                   "使用默认配置。")
            sys.exit(EXIT_CONFIG)
    else:
        eprint(f"[generate_pdf] 未找到 {config_path}，使用内置默认配置。")
    # 相对路径（字体）基于 skill 根目录解析
    fonts = cfg.get("fonts", {})
    for key in ("cjk", "cjk_bold", "mono"):
        p = Path(fonts.get(key, ""))
        if p and not p.is_absolute():
            fonts[key] = str((skill_root / p).resolve())
    return cfg


def deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


def is_valid_ttf(path) -> bool:
    """校验 TTF/OTF 文件头（防 0 字节或截断文件混入）。"""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
        # TTF: 00 01 00 00；OTF(CFF 轮廓 reportlab 不支持，但放行探测）
        return magic in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")
    except OSError:
        return False


def find_font(candidates, label: str):
    """按候选列表探测字体文件（校验文件头），全部无效时报错退出。"""
    for c in candidates:
        if c and Path(c).exists() and Path(c).stat().st_size > 1000 \
                and is_valid_ttf(c):
            return str(Path(c).resolve())
    eprint(f"[generate_pdf] {label} 字体未找到或文件无效。已尝试：{candidates}。"
           "请将 Noto Sans CJK SC（NotoSansSC-Regular.ttf，TTF 轮廓版本）放入 "
           "assets/fonts/，或在 config.json fonts 段指定绝对路径。"
           "注意：reportlab/xhtml2pdf 不支持 CFF 轮廓的 .otf，请使用 TTF 版本。")
    sys.exit(EXIT_CONFIG)


def patch_cjk_in_code(body_html: str, cfg: dict) -> str:
    """xhtml2pdf 无逐字形字体回退：含中文的代码块必须整体换用中文字体。

    对每个 <pre>/<code> 检测 CJK 字符，命中则在标签上注入中文字体
    font-family（Noto Sans SC 自带完整拉丁字形，代码缩进仍可对齐，
    但字符宽度非严格等宽——PDF 中架构图/伪代码以此保证中文不乱码）。
    纯 ASCII 代码块保持等宽字体。同时给 <title> 加 ASCII 保护
    （xhtml2pdf 用 Helvetica 渲染 PDF 元数据标题，含 CJK 会告警）。
    """
    import re
    cjk_family = cfg.get("fonts", {}).get("cjk_family", "NotoSansSC")

    def _patch(m):
        tag, attrs, inner = m.group(1), m.group(2), m.group(3)
        if not RE_CJK.search(inner):
            return m.group(0)
        # 剥离已有 font-family 声明，注入中文字体
        attrs = re.sub(r"\s*font-family\s*:[^;\"]+;?", "", attrs)
        style = attrs.strip()
        if style:
            if style.startswith("style="):
                style = style[:-1] + f" font-family: '{cjk_family}';\""
            else:
                style = style[:-1] + f" font-family: '{cjk_family}';\""
        else:
            style = f'style="font-family: \'{cjk_family}\';"'
        if tag == "pre":
            # pre 内嵌套的 <code> 会被 CSS『pre, code』规则直接命中而
            # 覆盖继承（xhtml2pdf 无 inherit 语义），必须同步注入
            inner = re.sub(
                r"<code((?:\s+[a-z-]+=\"[^\"]*\")*)\s*>",
                lambda cm: f"<code{cm.group(1)} style=\"font-family: '{cjk_family}';\">",
                inner)
        return f"<{tag} {style}>{inner}</{tag}>"

    return re.sub(
        r"<(pre|code)((?:\s+[a-z-]+=\"[^\"]*\")*)\s*>(.*?)</\1>",
        _patch, body_html, flags=re.DOTALL)


RE_CJK = None  # 模块级占位，main() 中初始化（避免 import 时正则开销）


def md_to_html(md_text: str, cfg: dict) -> str:
    import markdown
    highlight = cfg.get("highlight", {})
    md = markdown.Markdown(
        extensions=[
            "tables",
            "fenced_code",
            "codehilite",
            "toc",
            "sane_lists",
        ],
        extension_configs={
            "codehilite": {
                "noclasses": True,  # 内联样式，xhtml2pdf 兼容
                "pygments_style": highlight.get("theme", "default"),
                "guess_lang": False,
            },
            "toc": {"title": "目录"},
        },
    )
    return md.convert(md_text)


def build_css(cfg: dict, cjk_font: str, cjk_bold: str, mono_font: str) -> str:
    page = cfg.get("page", {})
    m = page.get("margin_cm", {})
    typo = cfg.get("typography", {})
    fonts = cfg.get("fonts", {})
    header, footer = cfg.get("header", {}), cfg.get("footer", {})

    css = f"""
@font-face {{
    font-family: "{fonts.get('cjk_family', 'NotoSansSC')}";
    src: url("{cjk_font}");
}}
@font-face {{
    font-family: "{fonts.get('cjk_family', 'NotoSansSC')}";
    src: url("{cjk_bold}");
    font-weight: bold;
}}
@font-face {{
    font-family: "{fonts.get('mono_family', 'MonoCode')}";
    src: url("{mono_font}");
}}
@page {{
    size: {page.get('size', 'A4')};
    margin: {m.get('top', 2.0)}cm {m.get('right', 2.2)}cm
            {m.get('bottom', 2.0)}cm {m.get('left', 2.2)}cm;
    @frame footer_frame {{
        -pdf-frame-content: footer_content;
        left: 50pt; width: 495pt; top: 800pt; height: 24pt;
    }}
}}
body {{
    font-family: "{fonts.get('cjk_family', 'NotoSansSC')}";
    font-size: {typo.get('body_size', '10.5pt')};
    line-height: {typo.get('line_height', 1.6)};
    color: #1a1a1a;
}}
h1 {{ font-size: {typo.get('h1_size', '18pt')}; margin: 14pt 0 8pt; }}
h2 {{ font-size: {typo.get('h2_size', '14pt')}; margin: 12pt 0 6pt;
      border-bottom: 0.6pt solid #ccc; padding-bottom: 2pt; }}
h3 {{ font-size: {typo.get('h3_size', '12pt')}; margin: 10pt 0 4pt; }}
pre, code, .codehilite pre {{
    font-family: "{fonts.get('mono_family', 'MonoCode')}";
    font-size: {typo.get('code_size', '8.5pt')};
}}
.codehilite pre {{ background-color: #f6f8fa;
                   padding: 6pt; border: 0.5pt solid #d0d7de; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0; }}
th, td {{ border: 0.5pt solid #999; padding: 3pt 5pt;
          font-size: 9.5pt; vertical-align: top; }}
th {{ background-color: #f0f0f0; }}
blockquote {{ border-left: 2.5pt solid #4a7fb5; margin: 6pt 0;
              padding: 2pt 8pt; background-color: #f4f8fc; }}
"""
    if not header.get("enabled", False):
        css += "\n#header_content { display: none; }\n"
    if not footer.get("enabled", True):
        css += "\n#footer_content { display: none; }\n"
    return css


def render_template(template_html: str, body_html: str, css: str,
                    cfg: dict, md_title: str) -> str:
    footer = cfg.get("footer", {})
    footer_text = (footer.get("text", "")
                   .replace("{{version}}", SKILL_VERSION))
    html = template_html
    for placeholder, value in (
        ("{{TITLE}}", md_title),
        ("{{BODY}}", body_html),
        ("{{CSS}}", css),
        ("{{FOOTER_TEXT}}", footer_text),
        ("{{HEADER_TEXT}}", cfg.get("header", {}).get("text", "")),
    ):
        html = html.replace(placeholder, value)
    return html


def main():
    ap = argparse.ArgumentParser(
        description="Markdown 报告 → PDF（中文字体 + Pygments 高亮）")
    ap.add_argument("markdown", help="报告 Markdown 文件路径")
    ap.add_argument("--outdir", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--config", default=None,
                    help="config.json 路径（默认 skill 的 assets/config.json）")
    args = ap.parse_args()

    check_deps()

    script_dir = Path(__file__).resolve().parent
    skill_root = script_dir.parent
    config_path = (Path(args.config) if args.config
                   else skill_root / "assets" / "config.json")
    cfg = load_config(config_path, skill_root)

    md_path = Path(args.markdown)
    if not md_path.exists():
        eprint(f"[generate_pdf] Markdown 文件不存在：{md_path}")
        sys.exit(EXIT_NO_SOURCE)

    # 字体探测（assets/fonts 优先，系统字体目录兜底）
    fonts_cfg = cfg["fonts"]
    cjk = find_font(
        [fonts_cfg.get("cjk"),
         skill_root / "assets" / "fonts" / "NotoSansSC-Regular.ttf",
         skill_root / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"],
        "中文正体")
    cjk_bold = find_font(
        [fonts_cfg.get("cjk_bold"),
         skill_root / "assets" / "fonts" / "NotoSansSC-Bold.ttf",
         skill_root / "assets" / "fonts" / "NotoSansCJKsc-Bold.otf",
         cjk],
        "中文粗体")
    mono = find_font(
        [fonts_cfg.get("mono"),
         skill_root / "assets" / "fonts" / "DejaVuSansMono.ttf",
         "C:/Windows/Fonts/consola.ttf"],
        "等宽（代码）")

    template_path = skill_root / "assets" / "report-template.html"
    if not template_path.exists():
        eprint(f"[generate_pdf] 模板缺失：{template_path}")
        sys.exit(EXIT_CONFIG)
    template_html = template_path.read_text(encoding="utf-8")

    md_text = md_path.read_text(encoding="utf-8")
    body_html = md_to_html(md_text, cfg)
    # 中文代码块字体补丁（xhtml2pdf 无回退链，必须显式指定）
    global RE_CJK
    if RE_CJK is None:
        import re as _re
        RE_CJK = _re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
    body_html = patch_cjk_in_code(body_html, cfg)
    title = next((ln[2:].strip() for ln in md_text.splitlines()
                  if ln.startswith("# ")), md_path.stem)
    css = build_css(cfg, cjk, cjk_bold, mono)
    # <title> 中的 CJK 会导致 xhtml2pdf（Helvetica 元数据）告警，ASCII 化
    ascii_title = "".join(ch if ord(ch) < 128 else "_" for ch in title)
    html = render_template(template_html, body_html, css, cfg, ascii_title)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    html_path = outdir / f"{md_path.stem}.html"
    pdf_path = outdir / f"{md_path.stem}.pdf"
    html_path.write_text(html, encoding="utf-8")

    try:
        from xhtml2pdf import pisa
        with open(pdf_path, "wb") as fh:
            status = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
        if status.err:
            raise RuntimeError(f"xhtml2pdf 报告 {status.err} 处错误")
        print(json.dumps({
            "status": "ok", "pdf": str(pdf_path), "html": str(html_path),
            "fonts": {"cjk": cjk, "mono": mono},
            "config_used": str(config_path),
        }, ensure_ascii=False, indent=2))
        sys.exit(EXIT_OK)
    except SystemExit:
        raise
    except Exception as exc:
        eprint(f"[generate_pdf] PDF 渲染失败：{type(exc).__name__}: {exc}。"
               f"已降级输出 HTML：{html_path}")
        sys.exit(EXIT_HTML_FALLBACK)


if __name__ == "__main__":
    main()
