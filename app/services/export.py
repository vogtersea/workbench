"""报告导出：Markdown → docx（公文格式）。

格式依据排版规则（中文公文通用约定）：
  页边距 上3.5 下3.3 左2.7 右2.7 cm
  正文 仿宋_GB2312 三号（16pt），固定行距 29.5 磅，首行缩进 2 字符，两端对齐
  大标题 方正小标宋简体 二号（22pt）居中
  一级标题 黑体 三号；二级标题 楷体_GB2312 三号
数字与汉字同字体：ascii / hAnsi / eastAsia 三个槽位写同一个字体名。

注意：字体只写名称不嵌入，打开端缺 `仿宋_GB2312` 时会回退到系统默认字体。
"""

from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

BODY_FONT = "仿宋_GB2312"
TITLE_FONT = "方正小标宋简体"
H1_FONT = "黑体"
H2_FONT = "楷体_GB2312"
BODY_SIZE = 16          # 三号
TITLE_SIZE = 22         # 二号
LINE_PT = 29.5
INDENT_PT = BODY_SIZE * 2   # 首行缩进 2 字符 = 32 磅

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,})\s*$")


def _clean(text: str) -> str:
    """去掉行内 Markdown 标记，保留纯文本。"""
    text = _LINK_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    return text.replace("`", "").strip()


def _set_font(run, name: str, size: int, bold: bool = False) -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)


def _format(paragraph, *, indent: bool, align=WD_ALIGN_PARAGRAPH.JUSTIFY) -> None:
    pf = paragraph.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = Pt(LINE_PT)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.first_line_indent = Pt(INDENT_PT if indent else 0)
    paragraph.alignment = align


def _add(doc, text, font, size, *, indent=False, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    paragraph = doc.add_paragraph()
    _format(paragraph, indent=indent, align=align)
    _set_font(paragraph.add_run(text), font, size)
    return paragraph


def build_docx(markdown: str, fallback_title: str = "") -> bytes:
    """把报告 Markdown 渲染为 docx 字节流。"""
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(3.5)
    section.bottom_margin = Cm(3.3)
    section.left_margin = Cm(2.7)
    section.right_margin = Cm(2.7)

    lines = (markdown or "").replace("\r\n", "\n").split("\n")

    # 首个一级标题提升为大标题，避免标题在正文里重复出现
    title = fallback_title
    start = 0
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("# "):
            title = _clean(raw.lstrip()[2:]) or fallback_title
            start = index + 1
        break

    if title:
        _add(doc, title, TITLE_FONT, TITLE_SIZE, align=WD_ALIGN_PARAGRAPH.CENTER)

    for raw in lines[start:]:
        line = raw.rstrip()
        if not line.strip() or _HR_RE.match(line):
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = _clean(stripped[level:])
            if not text:
                continue
            if level <= 2:
                _add(doc, text, H1_FONT, BODY_SIZE)
            elif level == 3:
                _add(doc, text, H2_FONT, BODY_SIZE)
            else:
                _add(doc, text, BODY_FONT, BODY_SIZE, indent=True)
            continue
        if stripped.startswith(">"):
            _add(doc, _clean(stripped[1:]), H2_FONT, BODY_SIZE, indent=True)
            continue
        if _LIST_RE.match(line):
            _add(doc, _clean(_LIST_RE.sub("", line, count=1)), BODY_FONT, BODY_SIZE, indent=True)
            continue
        _add(doc, _clean(line), BODY_FONT, BODY_SIZE, indent=True)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
