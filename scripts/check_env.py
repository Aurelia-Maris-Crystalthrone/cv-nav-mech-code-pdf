#!/usr/bin/env python3
"""
check_env.py — 环境自检（Python 版本 / 依赖 / 中文字体）

只检查、不安装、不修改系统。全部通过输出 READY；有缺失项输出修复指引。
退出码：0 就绪  1 有缺失
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

REQUIRED_PY = (3, 9)
DEPS = [("pymupdf", "pymupdf"), ("markdown", "markdown"),
        ("pygments", "pygments"), ("xhtml2pdf", "xhtml2pdf")]
FONT_CANDIDATES = [
    "assets/fonts/NotoSansSC-Regular.ttf",
    "assets/fonts/NotoSansCJKsc-Regular.otf",
    "assets/fonts/NotoSansCJK-Regular.ttc",
]
# Windows 系统字体兜底候选（思源黑体/微软雅黑，仅探测不依赖）
SYSTEM_FONT_FALLBACKS = [
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
]


def main():
    skill_root = Path(__file__).resolve().parent.parent
    problems = []
    report = {"python": "", "deps": {}, "fonts": {}, "ready": False}

    # Python 版本
    report["python"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < REQUIRED_PY:
        problems.append(
            f"Python {REQUIRED_PY[0]}.{REQUIRED_PY[1]}+ 需要，当前 "
            f"{report['python']}")

    # 依赖包
    for module, pip_name in DEPS:
        ok = importlib.util.find_spec(module) is not None
        report["deps"][module] = "OK" if ok else "MISSING"
        if not ok:
            problems.append(f"依赖 {pip_name} 未安装 → "
                            f"pip install {pip_name}")

    # 中文字体：assets/fonts 优先，系统字体兜底
    for rel in FONT_CANDIDATES:
        report["fonts"][rel] = ("OK" if (skill_root / rel).exists()
                                else "missing")
    font_found = any(v == "OK" for v in report["fonts"].values())
    if not font_found:
        for sysf in SYSTEM_FONT_FALLBACKS:
            if Path(sysf).exists():
                report["fonts"][sysf] = "OK (system fallback)"
                font_found = True
                break
    if not font_found:
        problems.append(
            "未找到中文字体（Noto Sans CJK SC）。下载 NotoSansSC-Regular.ttf "
            "与 NotoSansSC-Bold.ttf 放入 assets/fonts/；"
            "或确认系统存在 msyh.ttc 并在 config.json 指定绝对路径")

    report["ready"] = not problems
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        print("\n修复指引：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    print("READY：环境就绪，可运行 extract_pdf.py / generate_pdf.py")


if __name__ == "__main__":
    main()
