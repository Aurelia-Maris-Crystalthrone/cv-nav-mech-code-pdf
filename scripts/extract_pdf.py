#!/usr/bin/env python3
"""
extract_pdf.py — 论文 PDF 解析与代码区块提取（PyMuPDF 优先）

职责：
  1. 提取 PDF 元数据（标题/作者/页数等）          → paper_meta.json
  2. 提取逐页文本（带页码分隔标记）                → paper_text.md
  3. 启发式定位候选代码/伪代码区块（带评分与语言线索）→ code_blocks.json
  4. 扫描开源仓库链接（含脚注）                    → links.json

设计原则：
  - 排版/输出目录参数全部可配置，本脚本不做任何排版决策；
  - 边界异常显式退出码 + stderr 信息，失败快停，绝不静默吞错；
  - 仅做"候选定位"，最终取舍由 WorkBuddy 按 references/extraction-rules.md 复核。

退出码：
  0 正常（含"未发现代码区块"——那是有效结论，不是错误）
  1 参数/文件不存在   2 PDF 损坏不可读   3 PyMuPDF 未安装
  4 无文本层（疑似扫描件） 5 内部处理异常   6 PDF 加密
"""

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ARGS = 1
EXIT_CORRUPT = 2
EXIT_NO_PYMUPDF = 3
EXIT_NO_TEXT = 4
EXIT_INTERNAL = 5
EXIT_ENCRYPTED = 6


def eprint(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- 依赖检查
def import_pymupdf():
    """优先 PyMuPDF。新版包名 pymupdf，旧版兼容名 fitz。"""
    try:
        import pymupdf  # noqa: F401
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz
        return fitz
    except ImportError:
        eprint("[extract_pdf] PyMuPDF 未安装。请执行：pip install pymupdf")
        sys.exit(EXIT_NO_PYMUPDF)


# ---------------------------------------------------------------- 元数据
REPO_HOSTS = (
    "github.com", "gitlab.com", "gitee.com",
    "anonymous.4open.science", "bitbucket.org",
)
RE_URL = re.compile(r"https?://[^\s\)\]>,\"'）】]+", re.IGNORECASE)
# 行内引用标注，如 "... (github.com/a/b)" 中的裸域名（无协议头）
RE_BARE_DOMAIN = re.compile(
    r"(?:github|gitlab|gitee)\.com/[\w.\-]+/[\w.\-]+", re.IGNORECASE)


def extract_meta(doc, pdf_path: Path) -> dict:
    meta = doc.metadata or {}
    first_page_text = ""
    if doc.page_count > 0:
        first_page_text = doc[0].get_text("text")[:2000]
    lines = [ln.strip() for ln in first_page_text.splitlines() if ln.strip()]
    # arXiv 编号：优先从文本中找，其次从元数据 subject/title 回退
    arxiv = ""
    m = re.search(r"arXiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", first_page_text, re.I)
    if not m and meta.get("subject"):
        m = re.search(r"arXiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", meta["subject"], re.I)
    if m:
        arxiv = f"arXiv:{m.group(1)}" + (m.group(2) or "")
    return {
        "file": pdf_path.name,
        "file_size_bytes": pdf_path.stat().st_size,
        "title": (meta.get("title") or "").strip() or (lines[0] if lines else ""),
        "authors": (meta.get("author") or "").strip(),
        # PyMuPDF 元数据无单独 keywords 字段时为空，保留字段以防混淆
        "keywords": (meta.get("keywords") or "").strip(),
        "arxiv_id": arxiv,
        "page_count": doc.page_count,
        "is_encrypted": doc.needs_pass,
        "extractor": "PyMuPDF",
        "first_page_first_lines": lines[:5],
    }


# ---------------------------------------------------------------- 链接扫描
def scan_links(doc) -> list:
    """链接注释（可点击超链）+ 全文正则扫描（印刷 URL）双通道，去重合并。"""
    found = {}

    def add(url: str, page: int, source: str, context: str = ""):
        url = url.rstrip(".,;:)")
        key = url.lower()
        if key not in found:
            found[key] = {"url": url, "pages": [], "sources": set(),
                          "context": context[:160]}
        found[key]["pages"].append(page + 1)
        found[key]["sources"].add(source)

    # 通道 1：PDF 内嵌链接注释
    for pno in range(doc.page_count):
        try:
            for link in doc[pno].get_links():
                uri = link.get("uri", "")
                if uri and any(h in uri.lower() for h in REPO_HOSTS):
                    add(uri, pno, "link_annotation")
        except Exception:
            continue
    # 通道 2：正文文本（含脚注）正则扫描
    for pno in range(doc.page_count):
        text = doc[pno].get_text("text")
        for m in RE_URL.finditer(text):
            if any(h in m.group(0).lower() for h in REPO_HOSTS):
                add(m.group(0), pno, "text_scan")
        for m in RE_BARE_DOMAIN.finditer(text):
            frag = m.group(0)
            if f"https://{frag}".lower() not in found:
                add(f"https://{frag}", pno, "bare_domain",
                    text[max(0, m.start() - 60):m.end() + 60])
    out = []
    for item in found.values():
        out.append({
            "url": item["url"],
            "pages": sorted(set(item["pages"])),
            "sources": sorted(item["sources"]),
            "context": item["context"],
            "kind": ("repo_homepage_or_project"
                     if any(k in item["url"].lower()
                            for k in ("project", "page", ".io"))
                     else "code_repo_candidate"),
        })
    return out


# ---------------------------------------------------------------- 代码区块
# 语言线索表：(语言, 权重, 正则)。行级匹配，命中一条记一分。
LANG_HINTS = [
    ("python", 2, re.compile(
        r"^\s*(def |class |import |from \S+ import|@|print\(|if __name__)")),
    ("cpp", 2, re.compile(
        r"^\s*(#include|template\s*<|std::|void |int main|Eigen::|cv::|ros::)")),
    ("matlab", 2, re.compile(
        r"^\s*function\s+\[?[\w,\s]*\]?\s*=\s*\w+\(|\b(end|endfor|endif)\b\s*$")),
    ("pseudocode", 2, re.compile(
        r"←|:=|\bend if\b|\bend for\b|\bwhile\b.*\bdo\b|\brepeat\b|\buntil\b|\bforall\b|\breturn\b")),
]
# 强信号：Algorithm/Listing 环境，直接决定区块边界
RE_ALGO_HEAD = re.compile(
    r"^\s*Algorithm\s+\d+\s*[:.：]", re.IGNORECASE)
RE_LISTING_HEAD = re.compile(
    r"^\s*(Listing|Code|Figure)\s+\d+\s*[:.：]", re.IGNORECASE)
# 代码符号密度：符号字符占行内字符比例（启发式阈值见配置默认值）
RE_CODE_SYMBOL = re.compile(r"[={}<>+\-*/;()\[\]]|::|->|==")


def score_line(line: str):
    """返回 (代码符号密度, 语言线索得分表)。空行密度记 0。"""
    stripped = line.strip()
    if not stripped:
        return 0.0, {}
    hits = len(RE_CODE_SYMBOL.findall(stripped))
    density = hits / len(stripped)
    langs = {}
    for lang, weight, rx in LANG_HINTS:
        if rx.search(line):
            langs[lang] = langs.get(lang, 0) + weight
    return density, langs


# 缩进模式：连续行的前导空白一致性是代码块的强特征
RE_LEADING_WS = re.compile(r"^[ \t]+")


def block_is_code(lines: list, min_density: float, min_lines: int) -> bool:
    """块级判定：平均符号密度 + 语言线索 + 缩进模式三信号投票。

    - 密度信号：符号字符占比均值 ≥ min_density；
    - 语言信号：任一语言线索得分 ≥ 2；
    - 缩进信号：≥ 40% 的行带前导缩进，且存在 ≥ 2 级缩进深度
      （缩进结构 = 代码/伪代码的排版特征，正文段落几乎不会这样排）。
    任一信号达标即视为候选（宁多勿漏，人工复核做最终取舍）。
    """
    non_empty = [ln for ln in lines if ln.strip()]
    if len(non_empty) < min_lines:
        return False
    densities, lang_scores = [], {}
    for ln in non_empty:
        d, langs = score_line(ln)
        densities.append(d)
        for k, v in langs.items():
            lang_scores[k] = lang_scores.get(k, 0) + v
    avg_density = sum(densities) / len(densities)
    lang_hit = max(lang_scores.values(), default=0) >= 2
    indented = sum(1 for ln in non_empty if RE_LEADING_WS.match(ln))
    indent_ratio = indented / len(non_empty)
    depths = {len(RE_LEADING_WS.match(ln).group(0)) for ln in non_empty
              if RE_LEADING_WS.match(ln)}
    indent_signal = indent_ratio >= 0.4 and len(depths) >= 2
    return avg_density >= min_density or lang_hit or indent_signal


def score_block(lines: list):
    """返回块级统计：平均密度 / 强行占比 / 语言得分表。"""
    non_empty = [ln for ln in lines if ln.strip()]
    densities, lang_scores = [], {}
    for ln in non_empty:
        d, langs = score_line(ln)
        densities.append(d)
        for k, v in langs.items():
            lang_scores[k] = lang_scores.get(k, 0) + v
    avg = sum(densities) / len(densities) if densities else 0.0
    strong = (sum(1 for d in densities if d >= 0.25) / len(densities)
              if densities else 0.0)
    return avg, strong, lang_scores


def extract_code_blocks(doc, min_density=0.25, min_lines=3):
    """定位候选代码区块：Algorithm/Listing 环境 + PDF 文本块双通道。

    通道 1（行级，Algorithm 环境）：逐行扫描，环境内非空行全部收录；
    通道 2（块级，普通代码）：利用 PyMuPDF 的文本块（论文排版中代码
    天然成块），块级三信号投票判定，避免逐行密度对 Python 这类
    符号稀疏语言的漏检。
    非最终判定：评分仅供 WorkBuddy 复核排序，宁多勿漏。
    """
    candidates = []

    def push(page_no, lines, in_algo):
        avg, strong, lang_scores = score_block(lines)
        lang_guess = (max(lang_scores, key=lang_scores.get)
                      if lang_scores else "unknown")
        candidates.append({
            "page": page_no,
            "line_count": len([ln for ln in lines if ln.strip()]),
            "avg_symbol_density": round(avg, 3),
            "strong_line_ratio": round(strong, 3),
            "lang_guess": lang_guess,
            "lang_scores": lang_scores,
            "in_algorithm_env": in_algo,
            "text": "\n".join(lines),
        })

    # ---- 通道 1：行级 Algorithm/Listing 环境扫描 ----
    for pno in range(doc.page_count):
        lines = doc[pno].get_text("text").splitlines()
        buf, in_algo = [], False
        for ln in lines:
            if RE_ALGO_HEAD.search(ln) or RE_LISTING_HEAD.search(ln):
                if in_algo:
                    push(pno + 1, buf, True)
                buf, in_algo = [ln], True
                continue
            if in_algo:
                if not ln.strip():
                    continue  # 环境内空行不断块
                if re.match(r"^\s*\d{1,3}\s*$", ln):
                    continue  # 裸行号行跳过
                buf.append(ln)
            else:
                buf.append(ln)
        if in_algo and len([l for l in buf if l.strip()]) >= min_lines:
            push(pno + 1, buf, True)

    # ---- 通道 2：块级三信号投票 ----
    for pno in range(doc.page_count):
        try:
            blocks = doc[pno].get_text("blocks")
        except Exception:
            continue
        for b in blocks:
            if len(b) < 5 or b[6] != 0:  # 仅文本块
                continue
            text = b[4]
            if RE_ALGO_HEAD.search(text) or RE_LISTING_HEAD.search(text):
                continue  # 已由通道 1 收录
            lines = text.rstrip("\n").splitlines()
            if block_is_code(lines, min_density, min_lines):
                push(pno + 1, lines, False)

    # 质量排序：环境内 > 密度 > 行数
    candidates.sort(
        key=lambda c: (c["in_algorithm_env"], c["avg_symbol_density"],
                       c["line_count"]),
        reverse=True)
    return candidates


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(
        description="论文 PDF 解析：元数据/文本/代码区块/链接（PyMuPDF）")
    ap.add_argument("pdf", help="论文 PDF 路径")
    ap.add_argument("--outdir", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--min-density", type=float, default=0.25,
                    help="代码符号密度阈值（默认 0.25）")
    ap.add_argument("--min-lines", type=int, default=3,
                    help="候选区块最小行数（默认 3）")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        eprint(f"[extract_pdf] 文件不存在：{pdf_path}")
        sys.exit(EXIT_ARGS)
    if pdf_path.suffix.lower() != ".pdf":
        eprint(f"[extract_pdf] 非 PDF 文件：{pdf_path.suffix}")
        sys.exit(EXIT_ARGS)

    pymupdf = import_pymupdf()
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        eprint(f"[extract_pdf] PDF 损坏或不可读：{exc}")
        sys.exit(EXIT_CORRUPT)

    if doc.needs_pass:
        eprint("[extract_pdf] PDF 已加密，需提供解密版本。")
        sys.exit(EXIT_ENCRYPTED)

    try:
        meta = extract_meta(doc, pdf_path)
        # 逐页文本
        pages_text = []
        total_chars = 0
        for pno in range(doc.page_count):
            t = doc[pno].get_text("text")
            total_chars += len(t.strip())
            pages_text.append(t)
        if doc.page_count > 0 and total_chars < 30 * doc.page_count:
            eprint(f"[extract_pdf] 文本总量过低（{total_chars} 字符），"
                   "疑似扫描件（无文本层）。本 Skill 不做 OCR，"
                   "请先用 OCR 工具预处理。")
            sys.exit(EXIT_NO_TEXT)

        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)

        # paper_text.md：带页码分隔的分页文本
        parts = [f"# {meta['title'] or pdf_path.stem}\n"]
        for i, t in enumerate(pages_text):
            parts.append(f"\n<!-- PAGE {i + 1} -->\n\n{t}")
        (outdir / "paper_text.md").write_text(
            "".join(parts), encoding="utf-8")

        links = scan_links(doc)
        blocks = extract_code_blocks(
            doc, min_density=args.min_density, min_lines=args.min_lines)

        (outdir / "paper_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (outdir / "links.json").write_text(
            json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
        (outdir / "code_blocks.json").write_text(
            json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")

        # 摘要输出到 stdout（供 WorkBuddy 快速决策）
        print(json.dumps({
            "meta": {k: meta[k] for k in ("title", "authors", "page_count",
                                          "arxiv_id")},
            "candidate_code_blocks": len(blocks),
            "repo_links": len(links),
            "outputs": ["paper_meta.json", "paper_text.md",
                        "code_blocks.json", "links.json"],
            "note": ("未发现候选代码区块——若复核确认论文未提供源码，"
                     "在报告中标注『论文未提供源码』") if not blocks else "",
        }, ensure_ascii=False, indent=2))
        sys.exit(EXIT_OK)
    except SystemExit:
        raise
    except Exception as exc:
        eprint(f"[extract_pdf] 内部处理异常：{type(exc).__name__}: {exc}")
        sys.exit(EXIT_INTERNAL)


if __name__ == "__main__":
    main()
