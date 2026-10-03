"""
Grok 搜索结果卡片渲染器

基于 PIL/Pillow 纯本地渲染，将搜索结果渲染为分区面板风格的深色/浅色卡片图片。
支持 Markdown 子集：标题、列表（含嵌套/有序）、代码块、引用、表格、分隔线、
粗体、行内代码与链接。页眉展示插件名称与版本号，页脚展示模型、耗时与插件仓库地址。
默认以 2x 超采样输出，保证在手机端放大查看时文字清晰。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import font_loader

# ─── 主题配色 ────────────────────────────────────────────────

THEME_DARK = {
    "bg": (13, 15, 19),
    "panel": (22, 26, 33),
    "panel_border": (40, 46, 58),
    "text": (226, 231, 237),
    "dim": (150, 160, 172),
    "accent": (0, 214, 175),
    "accent_soft": (14, 52, 48),
    "bold": (255, 255, 255),
    "heading": (245, 248, 250),
    "code_bg": (15, 17, 22),
    "code_text": (170, 214, 255),
    "inline_code_bg": (38, 44, 57),
    "inline_code_text": (150, 210, 255),
    "quote_bar": (0, 214, 175),
    "quote_bg": (27, 32, 41),
    "quote_text": (190, 198, 208),
    "bullet": (0, 214, 175),
    "link": (110, 180, 255),
    "source_idx": (0, 214, 175),
    "source_panel": (19, 22, 28),
    "table_head": (31, 37, 47),
    "divider": (40, 46, 58),
}

THEME_LIGHT = {
    "bg": (243, 244, 247),
    "panel": (255, 255, 255),
    "panel_border": (220, 224, 232),
    "text": (31, 36, 48),
    "dim": (98, 108, 125),
    "accent": (0, 150, 122),
    "accent_soft": (222, 244, 239),
    "bold": (8, 10, 18),
    "heading": (14, 18, 28),
    "code_bg": (244, 246, 250),
    "code_text": (36, 76, 156),
    "inline_code_bg": (234, 238, 245),
    "inline_code_text": (36, 76, 156),
    "quote_bar": (0, 150, 122),
    "quote_bg": (246, 248, 250),
    "quote_text": (75, 85, 104),
    "bullet": (0, 150, 122),
    "link": (25, 100, 200),
    "source_idx": (0, 150, 122),
    "source_panel": (250, 251, 253),
    "table_head": (240, 243, 248),
    "divider": (220, 224, 232),
}


def _get_theme(theme: str = "auto") -> dict[str, tuple]:
    """获取主题配色。theme='auto' 时根据本地时间自动切换 (7:00-18:00 浅色)"""
    if theme == "light":
        return THEME_LIGHT
    if theme == "dark":
        return THEME_DARK
    hour = datetime.now().hour
    return THEME_LIGHT if 7 <= hour < 18 else THEME_DARK


# ─── 插件元信息（页眉版本号 / 页脚仓库地址） ─────────────────

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RE_META_LINE = re.compile(r"^([A-Za-z_]+):\s*(.*?)\s*$")


@lru_cache(maxsize=1)
def _plugin_meta() -> dict[str, str]:
    """读取插件根目录 metadata.yaml 的顶层标量字段。

    只需要 display_name / version / repo，按行解析即可，避免引入 YAML 依赖；
    文件缺失或损坏时返回空字典，卡片对应位置自动省略。
    """
    meta: dict[str, str] = {}
    try:
        with open(os.path.join(_PLUGIN_ROOT, "metadata.yaml"), encoding="utf-8") as f:
            for line in f:
                m = _RE_META_LINE.match(line)
                if m and m.group(2):
                    meta[m.group(1)] = m.group(2).strip("\"'")
    except OSError:
        pass
    return meta


@lru_cache(maxsize=4)
def _logo_mask(size: int) -> Image.Image | None:
    """加载插件 logo 并转为指定尺寸的灰度蒙版（形状=不透明区域），失败返回 None。"""
    try:
        with Image.open(os.path.join(_PLUGIN_ROOT, "logo.png")) as im:
            rgba = im.convert("RGBA")
        alpha = rgba.getchannel("A")
        if alpha.getextrema() == (255, 255):
            # 无透明通道：深色图形在浅底上，反相灰度作为形状
            alpha = ImageOps.invert(rgba.convert("L"))
        return alpha.resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        return None


# ─── 字体管理 ───────────────────────────────────────────────

# 运行时字体路径（由 init_fonts 设置）
_font_regular_path: str = ""
_font_bold_path: str = ""
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_fonts_ready = False


def init_fonts(font_dir: str | None = None, job=None) -> bool:
    """初始化字体。如果 font_dir 有字体就用，没有就自动下载（最新版本）。"""
    global _font_regular_path, _font_bold_path, _fonts_ready, _font_cache

    if font_dir is None:
        font_dir = os.path.join(os.path.dirname(__file__), "font")

    paths = font_loader.init_fonts(font_dir, job=job)
    if not paths:
        return False
    _font_regular_path, _font_bold_path = paths
    _font_cache.clear()
    _fonts_ready = True
    return True


def _get_font(bold: bool = False, size: int = 18) -> ImageFont.FreeTypeFont:
    if not _fonts_ready:
        init_fonts()
    path = _font_bold_path if bold else _font_regular_path
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


# ─── 文本预处理 ──────────────────────────────────────────────

# Sarasa 不含彩色 emoji 字形，直接剔除以免渲染成方块
_RE_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002b00-\U00002bff\U0000fe00-\U0000fe0f\u200d\u20e3]"
)


def _sanitize(text: str) -> str:
    return _RE_EMOJI.sub("", text.replace("\r\n", "\n").replace("\t", "    "))


def _strip_md(text: str) -> str:
    """去掉行内 Markdown 标记，只保留可读文本（用于标题、表格单元格）。"""
    text = _RE_CITE_LINK.sub("", text)
    text = _RE_LINK.sub(lambda m: m.group(1), text)
    text = _RE_BARE_URL.sub(lambda m: _url_host(m.group(0)), text)
    return text.replace("**", "").replace("`", "").strip()


def _short_url(url: str) -> str:
    """去掉协议、www. 与末尾斜杠"""
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")


def _url_host(url: str) -> str:
    return _short_url(url).split("/", 1)[0]


def _ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """单行截断，超宽时以 … 结尾"""
    if font.getlength(text) <= max_width:
        return text
    ell = "…"
    while text and font.getlength(text + ell) > max_width:
        text = text[:-1]
    return text + ell


# ─── 富文本工具 ──────────────────────────────────────────────

# 富文本片段: (text, style)
# style: "n"=normal, "b"=bold, "c"=inline_code, "l"=link, "cite"=引用角标
_RichSpan = tuple[str, str]
_URL_BODY = r"[^()\s]+"
# 引用：[[1]](url) / [1](url) / [^1](url)
_RE_CITE_LINK = re.compile(rf"\[\[?\^?(\d{{1,3}})\]?\]\(({_URL_BODY})\)")
_RE_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])+)\]\((?:[^()\s]|\([^)]*\))+\)")
# 裸 URL：遇到空白/括号/中文标点结束，且不以英文标点收尾
_RE_BARE_URL = re.compile(
    r"https?://[^\s<>\[\]()（）【】「」，。；！？、“”‘’\"'`]+(?<![.,;:!?])"
)
_RE_RICH = re.compile(
    r"(?P<bold>\*\*.+?\*\*)"
    r"|(?P<code>`[^`]+`)"
    rf"|\[\[?\^?(?P<cite>\d{{1,3}})\]?\]\({_URL_BODY}\)"
    r"|(?P<link>\[(?:[^\[\]]|\[[^\]]*\])+\]\((?:[^()\s]|\([^)]*\))+\))"
    r"|\[\^?(?P<cite_bare>\d{1,3})\](?!\()"
    r"|【(?P<cite_cn>\d{1,3})】"
    r"|(?P<url>https?://[^\s<>\[\]()（）【】「」，。；！？、“”‘’\"'`]+(?<![.,;:!?]))"
)
# 英文/数字/URL 片段作为整体换行，其余字符（CJK、标点、空白）逐个断行
_RE_TOKEN = re.compile(r"[A-Za-z0-9_\-./:%#@&?=+~'’]+|\s|.", re.S)
# 不允许出现在行首的标点（避头规则）
_NO_LINE_START = frozenset("，。、；：！？）》」』】〕〉…”’,;:!?)]}%")


def _parse_rich(text: str) -> list[_RichSpan]:
    """将行内 Markdown 解析为 [(text, style), ...]

    支持 **粗体**（内部可再含其他标记）、`行内代码`、[链接](url)、
    引用角标 [[1]](url) / [1](url) / [1] / 【1】，以及裸 URL（缩短为域名）。
    """
    spans: list[_RichSpan] = []
    last = 0
    for m in _RE_RICH.finditer(text):
        if m.start() > last:
            spans.append((text[last : m.start()], "n"))
        last = m.end()
        if m.group("bold"):
            # 粗体内部的引用/链接/代码照常解析，普通文本转为粗体
            for t, s in _parse_rich(m.group("bold")[2:-2]):
                spans.append((t, "b" if s == "n" else s))
        elif m.group("code"):
            spans.append((m.group("code")[1:-1], "c"))
        elif m.group("link"):
            label = _RE_LINK.match(m.group("link")).group(1)
            if _RE_BARE_URL.fullmatch(label.strip()):
                label = _url_host(label)
            spans.append((label, "l"))
        elif m.group("url"):
            spans.append((_url_host(m.group("url")), "l"))
        else:
            num = m.group("cite") or m.group("cite_bare") or m.group("cite_cn")
            # 引用前的空白去掉，让角标紧贴前文
            if spans and spans[-1][1] == "n":
                stripped = spans[-1][0].rstrip()
                spans[-1] = (stripped, "n")
            spans.append((num, "cite"))
    if last < len(text):
        spans.append((text[last:], "n"))
    return [(t, s) for t, s in spans if t]


def _collect_citations(text: str) -> list[tuple[str, str]]:
    """按编号收集正文中带 URL 的引用（同编号取首次出现的 URL），用于卡片底部引用列表"""
    found: dict[str, str] = {}
    for m in _RE_CITE_LINK.finditer(text):
        found.setdefault(str(int(m.group(1))), m.group(2))
    return sorted(found.items(), key=lambda kv: int(kv[0]))


def _wrap_rich(
    spans: list[_RichSpan],
    fonts: dict[str, ImageFont.FreeTypeFont],
    max_width: float,
    extras: dict[str, float] | None = None,
) -> list[list[_RichSpan]]:
    """将富文本片段按像素宽度换行，返回按行分组的片段列表。

    英文单词/URL 尽量整体换行，超长时再逐字符拆分；行首空白被丢弃。
    extras 为各样式每段额外占用的宽度（行内代码/引用角标的内边距）。
    """
    extras = extras or {}
    lines: list[list[_RichSpan]] = []
    cur: list[list] = []  # [text, style, span_index]
    cur_w = 0.0

    def flush() -> None:
        nonlocal cur, cur_w
        if cur:
            cur[-1][0] = cur[-1][0].rstrip()
        lines.append([(t, s) for t, s, _ in cur if t])
        cur = []
        cur_w = 0.0

    def hangs(text: str, style: str) -> bool:
        """避头标点、紧贴前文的右引号与首个引用角标放不下时悬挂在行尾"""
        if style == "cite":
            return bool(cur) and cur[-1][1] != "cite"
        if text in _NO_LINE_START:
            return True
        return text in "\"'" and bool(cur) and not cur[-1][0][-1:].isspace()

    def add(text: str, style: str, idx: int, width: float) -> None:
        nonlocal cur_w
        new_piece = not cur or cur[-1][2] != idx
        extra = extras.get(style, 0) if new_piece else 0
        if cur and cur_w + width + extra > max_width and not hangs(text, style):
            flush()
            if text.isspace():
                return
            new_piece, extra = True, extras.get(style, 0)
        if new_piece:
            cur.append([text, style, idx])
        else:
            cur[-1][0] += text
        cur_w += width + extra

    for idx, (seg, style) in enumerate(spans):
        font = fonts.get(style, fonts["n"])
        for tok in _RE_TOKEN.findall(seg):
            if tok.isspace() and not cur:
                continue
            w = font.getlength(tok)
            if len(tok) > 1 and w + extras.get(style, 0) > max_width:
                for ch in tok:
                    add(ch, style, idx, font.getlength(ch))
            else:
                add(tok, style, idx, w)

    if cur:
        flush()
    return lines or [[]]


def _wrap_plain(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """纯文本换行（不解析 Markdown 标记），空字符串返回 [""]"""
    out: list[str] = []
    for paragraph in text.split("\n"):
        for line in _wrap_rich([(paragraph, "n")], {"n": font}, max_width):
            out.append("".join(t for t, _ in line))
    return out or [""]


# ─── 渲染上下文 ─────────────────────────────────────────────


class _Ctx:
    def __init__(
        self,
        width: int = 800,
        scale: float = 2.0,
        theme: dict[str, tuple] | None = None,
    ):
        self.scale = scale
        self.width = self.p(width)
        self.margin = self.p(28)
        self.panel_pad = self.p(22)
        self.gap = self.p(14)
        self.cw = self.width - self.margin * 2 - self.panel_pad * 2  # 内容宽度
        self.theme = theme or THEME_DARK

        def f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
            return _get_font(bold=bold, size=self.p(size))

        self.f_title = f(24, True)
        self.f_sub = f(13)
        self.f_badge = f(14, True)
        self.f_section = f(21, True)
        self.f_heading = f(19, True)
        self.f_content = f(18)
        self.f_bold = f(18, True)
        self.f_code = f(15)
        self.f_table = f(16)
        self.f_table_bold = f(16, True)
        self.f_source = f(15)
        self.f_url = f(13)
        self.f_ui = f(14)
        self.f_ui_bold = f(14, True)
        self.f_cite = f(12, True)

        self.lh_content = self.p(29)
        self.lh_code = self.p(23)
        self.lh_table = self.p(25)
        # 行内代码 / 引用角标：每段内边距与左右间隔，换行测量与绘制共用
        self.code_pad, self.code_gap = self.p(5), self.p(2)
        self.cite_pad, self.cite_gap = self.p(4), self.p(2)
        self.extras = {
            "c": self.code_pad * 2 + self.code_gap,
            "cite": (self.cite_pad + self.cite_gap) * 2,
        }

        self._dummy = Image.new("RGB", (1, 1))
        self.draw = ImageDraw.Draw(self._dummy)

    def p(self, v: float) -> int:
        """逻辑像素 → 物理像素"""
        return max(1, round(v * self.scale)) if v > 0 else 0

    def rich_fonts(self) -> dict[str, ImageFont.FreeTypeFont]:
        return {
            "n": self.f_content,
            "b": self.f_bold,
            "c": self.f_code,
            "l": self.f_content,
            "cite": self.f_cite,
        }

    def create_canvas(self, height: int) -> None:
        self.img = Image.new("RGB", (self.width, height), color=self.theme["bg"])
        self.draw = ImageDraw.Draw(self.img)


def _draw_spans(
    ctx: _Ctx,
    spans: list[_RichSpan],
    x: float,
    y: float,
    lh: int,
    color: tuple,
    bold_color: tuple,
) -> None:
    """在行框 [y, y+lh) 内垂直居中绘制一行富文本"""
    fonts = ctx.rich_fonts()
    cy = y + lh / 2
    cx = x
    for text, style in spans:
        font = fonts.get(style, ctx.f_content)
        w = font.getlength(text)
        if style == "c":
            pad = ctx.code_pad
            half = ctx.f_content.size * 0.62
            ctx.draw.rounded_rectangle(
                [cx, cy - half, cx + w + pad * 2, cy + half],
                radius=ctx.p(4),
                fill=ctx.theme["inline_code_bg"],
            )
            ctx.draw.text(
                (cx + pad, cy),
                text,
                font=font,
                fill=ctx.theme["inline_code_text"],
                anchor="lm",
            )
            cx += w + ctx.extras["c"]
            continue
        if style == "cite":
            # 引用角标：上标位置的小圆角徽标
            bx = cx + ctx.cite_gap
            bw = w + ctx.cite_pad * 2
            bh = ctx.p(17)
            by = cy - bh / 2 - ctx.p(4)
            ctx.draw.rounded_rectangle(
                [bx, by, bx + bw, by + bh],
                radius=ctx.p(4),
                fill=ctx.theme["accent_soft"],
            )
            ctx.draw.text(
                (bx + bw / 2, by + bh / 2),
                text,
                font=font,
                fill=ctx.theme["accent"],
                anchor="mm",
            )
            cx += w + ctx.extras["cite"]
            continue
        if style == "l":
            fill = ctx.theme["link"]
            uy = cy + font.size * 0.58
            ctx.draw.line([(cx, uy), (cx + w, uy)], fill=fill, width=ctx.p(1))
        else:
            fill = bold_color if style == "b" else color
        ctx.draw.text((cx, cy), text, font=font, fill=fill, anchor="lm")
        cx += w


# ─── Markdown → Section 解析 ────────────────────────────────

_RE_HEADER = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RE_BULLET = re.compile(r"^(\s*)[\-\*\+]\s+(.*)")
_RE_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)")
_RE_QUOTE = re.compile(r"^\s*>\s?(.*)")
_RE_CODE_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)")
_RE_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


class _Element:
    """渲染元素基类"""

    def height(self, ctx: _Ctx) -> int:
        raise NotImplementedError

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        raise NotImplementedError


class _TextElem(_Element):
    def __init__(self, text: str):
        self.text = text
        self._lines: list[list[_RichSpan]] | None = None

    def _wrapped(self, ctx: _Ctx) -> list[list[_RichSpan]]:
        # height 阶段计算一次并缓存，render 阶段直接复用
        if self._lines is None:
            self._lines = _wrap_rich(
                _parse_rich(self.text), ctx.rich_fonts(), ctx.cw, ctx.extras
            )
        return self._lines

    def height(self, ctx: _Ctx) -> int:
        return len(self._wrapped(ctx)) * ctx.lh_content + ctx.p(2)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        for line in self._wrapped(ctx):
            _draw_spans(
                ctx, line, x, y, ctx.lh_content, ctx.theme["text"], ctx.theme["bold"]
            )
            y += ctx.lh_content
        return y + ctx.p(2)


class _HeadingElem(_Element):
    """面板内的小标题（### 及以下）"""

    def __init__(self, text: str):
        self.text = _strip_md(text)
        self._lines: list[str] | None = None

    def _wrapped(self, ctx: _Ctx) -> list[str]:
        if self._lines is None:
            self._lines = _wrap_plain(self.text, ctx.f_heading, ctx.cw)
        return self._lines

    def height(self, ctx: _Ctx) -> int:
        return ctx.p(6) + len(self._wrapped(ctx)) * ctx.lh_content + ctx.p(2)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        y += ctx.p(6)
        for line in self._wrapped(ctx):
            ctx.draw.text(
                (x, y + ctx.lh_content / 2),
                line,
                font=ctx.f_heading,
                fill=ctx.theme["heading"],
                anchor="lm",
            )
            y += ctx.lh_content
        return y + ctx.p(2)


class _BulletElem(_Element):
    def __init__(self, text: str, marker: str = "•", level: int = 0):
        self.text = text
        self.marker = marker
        self.level = min(level, 3)
        self._lines: list[list[_RichSpan]] | None = None

    def _indent(self, ctx: _Ctx) -> tuple[int, int]:
        """返回 (标记起点偏移, 正文起点偏移)"""
        base = ctx.p(22) * self.level
        if self.marker in ("•", "◦"):
            return base, base + ctx.p(22)
        mw = ctx.f_bold.getlength(self.marker)
        return base, base + max(ctx.p(22), int(mw + ctx.p(8)))

    def _wrapped(self, ctx: _Ctx) -> list[list[_RichSpan]]:
        # height 阶段计算一次并缓存，render 阶段直接复用
        if self._lines is None:
            _, text_x = self._indent(ctx)
            self._lines = _wrap_rich(
                _parse_rich(self.text),
                ctx.rich_fonts(),
                ctx.cw - text_x,
                ctx.extras,
            )
        return self._lines

    def height(self, ctx: _Ctx) -> int:
        return len(self._wrapped(ctx)) * ctx.lh_content + ctx.p(3)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        lh = ctx.lh_content
        mx, tx = self._indent(ctx)
        cy = y + lh / 2
        color = ctx.theme["bullet"]
        if self.marker in ("•", "◦"):
            r = ctx.p(3)
            cx = x + mx + ctx.p(7)
            if self.marker == "•":
                ctx.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
            else:
                ctx.draw.ellipse(
                    [cx - r, cy - r, cx + r, cy + r], outline=color, width=ctx.p(1.5)
                )
        else:
            ctx.draw.text(
                (x + mx, cy), self.marker, font=ctx.f_bold, fill=color, anchor="lm"
            )
        for line in self._wrapped(ctx):
            _draw_spans(ctx, line, x + tx, y, lh, ctx.theme["text"], ctx.theme["bold"])
            y += lh
        return y + ctx.p(3)


class _QuoteElem(_Element):
    def __init__(self, lines: list[str]):
        self.src = list(lines)
        self._lines: list[list[_RichSpan]] | None = None

    def _wrapped(self, ctx: _Ctx) -> list[list[_RichSpan]]:
        # height 阶段计算一次并缓存，render 阶段直接复用
        if self._lines is None:
            wrapped: list[list[_RichSpan]] = []
            for ln in self.src:
                wrapped.extend(
                    _wrap_rich(
                        _parse_rich(ln),
                        ctx.rich_fonts(),
                        ctx.cw - ctx.p(30),
                        ctx.extras,
                    )
                )
            self._lines = wrapped
        return self._lines

    def height(self, ctx: _Ctx) -> int:
        return len(self._wrapped(ctx)) * ctx.lh_content + ctx.p(16) + ctx.p(8)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        lines = self._wrapped(ctx)
        box_h = len(lines) * ctx.lh_content + ctx.p(16)
        top = y + ctx.p(4)
        ctx.draw.rounded_rectangle(
            [x, top, x + ctx.cw, top + box_h],
            radius=ctx.p(6),
            fill=ctx.theme["quote_bg"],
        )
        ctx.draw.rounded_rectangle(
            [x, top, x + ctx.p(4), top + box_h],
            radius=ctx.p(2),
            fill=ctx.theme["quote_bar"],
        )
        ty = top + ctx.p(8)
        for line in lines:
            _draw_spans(
                ctx,
                line,
                x + ctx.p(18),
                ty,
                ctx.lh_content,
                ctx.theme["quote_text"],
                ctx.theme["text"],
            )
            ty += ctx.lh_content
        return top + box_h + ctx.p(4)


class _CodeElem(_Element):
    def __init__(self, lines: list[str], lang: str = ""):
        self.code = "\n".join(lines)
        self.lang = lang
        self._lines: list[str] | None = None

    def _wrapped(self, ctx: _Ctx) -> list[str]:
        # height 阶段计算一次并缓存，render 阶段直接复用
        if self._lines is None:
            wrapped: list[str] = []
            for ln in self.code.split("\n"):
                wrapped.extend(_wrap_plain(ln, ctx.f_code, ctx.cw - ctx.p(28)))
            self._lines = wrapped
        return self._lines

    def _head(self, ctx: _Ctx) -> int:
        return ctx.lh_code if self.lang else 0

    def height(self, ctx: _Ctx) -> int:
        return len(self._wrapped(ctx)) * ctx.lh_code + self._head(ctx) + ctx.p(28)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        wrapped = self._wrapped(ctx)
        top = y + ctx.p(4)
        box_h = len(wrapped) * ctx.lh_code + self._head(ctx) + ctx.p(20)
        ctx.draw.rounded_rectangle(
            [x, top, x + ctx.cw, top + box_h],
            radius=ctx.p(8),
            fill=ctx.theme["code_bg"],
            outline=ctx.theme["panel_border"],
            width=ctx.p(1),
        )
        ty = top + ctx.p(10)
        if self.lang:
            ctx.draw.text(
                (x + ctx.p(14), ty + ctx.lh_code / 2),
                self.lang.upper(),
                font=ctx.f_url,
                fill=ctx.theme["dim"],
                anchor="lm",
            )
            ty += ctx.lh_code
        for line in wrapped:
            ctx.draw.text(
                (x + ctx.p(14), ty + ctx.lh_code / 2),
                line,
                font=ctx.f_code,
                fill=ctx.theme["code_text"],
                anchor="lm",
            )
            ty += ctx.lh_code
        return top + box_h + ctx.p(4)


class _TableElem(_Element):
    """简单网格表格：列宽按内容比例分配，单元格内自动换行"""

    def __init__(self, rows: list[list[str]]):
        ncol = max(len(r) for r in rows)
        self.ncol = ncol
        self.rows = [[_strip_md(c) for c in r] + [""] * (ncol - len(r)) for r in rows]
        self._layout: tuple[list[float], list[list[list[str]]], list[int]] | None = None

    def _compute(self, ctx: _Ctx):
        if self._layout is not None:
            return self._layout
        pad = ctx.p(10)
        natural: list[float] = []
        for c in range(self.ncol):
            w = max(
                (ctx.f_table_bold if i == 0 else ctx.f_table).getlength(r[c])
                for i, r in enumerate(self.rows)
            )
            natural.append(w + pad * 2 + 1)
        total = sum(natural)
        avail = ctx.cw
        if total <= avail:
            widths = [n + (avail - total) * n / total for n in natural]
        else:
            widths = [max(ctx.p(56), avail * n / total) for n in natural]
            s = sum(widths)
            widths = [w * avail / s for w in widths]
        cells: list[list[list[str]]] = []
        heights: list[int] = []
        for i, r in enumerate(self.rows):
            font = ctx.f_table_bold if i == 0 else ctx.f_table
            wrapped = [
                _wrap_plain(r[c], font, max(1.0, widths[c] - pad * 2))
                for c in range(self.ncol)
            ]
            cells.append(wrapped)
            heights.append(max(len(w) for w in wrapped) * ctx.lh_table + ctx.p(14))
        self._layout = (widths, cells, heights)
        return self._layout

    def height(self, ctx: _Ctx) -> int:
        return sum(self._compute(ctx)[2]) + ctx.p(12)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        widths, cells, heights = self._compute(ctx)
        top = y + ctx.p(6)
        bottom = top + sum(heights)
        border = ctx.theme["panel_border"]
        radius = ctx.p(6)
        ctx.draw.rounded_rectangle(
            [x, top, x + ctx.cw, top + heights[0]],
            radius=radius,
            fill=ctx.theme["table_head"],
            corners=(True, True, False, False),
        )
        ry = top
        for i, (row, h) in enumerate(zip(cells, heights)):
            font = ctx.f_table_bold if i == 0 else ctx.f_table
            color = ctx.theme["heading"] if i == 0 else ctx.theme["text"]
            cx = x
            for c, lines in enumerate(row):
                ly = ry + ctx.p(7)
                for line in lines:
                    ctx.draw.text(
                        (cx + ctx.p(10), ly + ctx.lh_table / 2),
                        line,
                        font=font,
                        fill=color,
                        anchor="lm",
                    )
                    ly += ctx.lh_table
                cx += widths[c]
            ry += h
            if i < len(cells) - 1:
                ctx.draw.line([(x, ry), (x + ctx.cw, ry)], fill=border, width=ctx.p(1))
        cx = x
        for w in widths[:-1]:
            cx += w
            ctx.draw.line([(cx, top), (cx, bottom)], fill=border, width=ctx.p(1))
        ctx.draw.rounded_rectangle(
            [x, top, x + ctx.cw, bottom], radius=radius, outline=border, width=ctx.p(1)
        )
        return bottom + ctx.p(6)


class _RuleElem(_Element):
    def height(self, ctx: _Ctx) -> int:
        return ctx.p(20)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        my = y + ctx.p(10)
        ctx.draw.line(
            [(x, my), (x + ctx.cw, my)], fill=ctx.theme["divider"], width=ctx.p(1)
        )
        return y + ctx.p(20)


class _GapElem(_Element):
    def height(self, ctx: _Ctx) -> int:
        return ctx.p(8)

    def render(self, ctx: _Ctx, x: int, y: int) -> int:
        return y + ctx.p(8)


class _Section:
    """一个面板区块 = 可选标题 + 多个元素"""

    def __init__(self, title: str = "", elements: list[_Element] | None = None):
        self.title = title
        self.elements = elements or []


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_to_sections(text: str) -> list[_Section]:
    """将 Markdown 文本解析为 Section 列表（# / ## 开启新面板，### 及以下为面板内小标题）"""
    sections: list[_Section] = []
    current_title = ""
    current_elems: list[_Element] = []
    lines = _sanitize(text).split("\n")
    i = 0
    quote_buf: list[str] = []

    def flush_quotes():
        nonlocal quote_buf
        if quote_buf:
            current_elems.append(_QuoteElem(quote_buf))
            quote_buf = []

    def add_gap():
        # 连续空行折叠为一个间距
        if current_elems and not isinstance(current_elems[-1], _GapElem):
            current_elems.append(_GapElem())

    def push_section():
        nonlocal current_title, current_elems
        while current_elems and isinstance(current_elems[-1], _GapElem):
            current_elems.pop()
        if current_elems or current_title:
            sections.append(_Section(current_title, current_elems))
        current_title = ""
        current_elems = []

    while i < len(lines):
        line = lines[i]

        # 代码块
        m = _RE_CODE_FENCE.match(line)
        if m:
            flush_quotes()
            fence = m.group(1)
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code_lines.append(lines[i].rstrip())
                i += 1
            i += 1
            current_elems.append(_CodeElem(code_lines, m.group(2)))
            continue

        # 引用
        m = _RE_QUOTE.match(line)
        if m:
            quote_buf.append(m.group(1))
            i += 1
            continue
        flush_quotes()

        # 表格（连续 | 行，跳过 |---| 分隔行）
        if _RE_TABLE_ROW.match(line):
            rows: list[list[str]] = []
            while i < len(lines) and _RE_TABLE_ROW.match(lines[i]):
                if not _RE_TABLE_SEP.match(lines[i]):
                    rows.append(_split_table_row(lines[i]))
                i += 1
            if rows:
                current_elems.append(_TableElem(rows))
            continue

        # 标题
        m = _RE_HEADER.match(line)
        if m:
            if len(m.group(1)) <= 2:
                push_section()
                current_title = _strip_md(m.group(2))
            else:
                current_elems.append(_HeadingElem(m.group(2)))
            i += 1
            continue

        # 分隔线（需在无序列表前判断，避免 "* * *" 被当作列表）
        if _RE_RULE.match(line):
            if current_elems:
                current_elems.append(_RuleElem())
            i += 1
            continue

        # 无序列表
        m = _RE_BULLET.match(line)
        if m:
            level = len(m.group(1)) // 2
            current_elems.append(
                _BulletElem(m.group(2), marker="•" if level == 0 else "◦", level=level)
            )
            i += 1
            continue

        # 有序列表
        m = _RE_NUMBERED.match(line)
        if m:
            level = len(m.group(1)) // 2
            current_elems.append(
                _BulletElem(m.group(3), marker=f"{m.group(2)}.", level=level)
            )
            i += 1
            continue

        # 空行
        if not line.strip():
            add_gap()
            i += 1
            continue

        # 普通文本
        current_elems.append(_TextElem(line.strip()))
        i += 1

    flush_quotes()
    push_section()
    return sections


# ─── 面板 ───────────────────────────────────────────────────


def _section_title_lines(sec: _Section, ctx: _Ctx) -> list[str]:
    return (
        _wrap_plain(sec.title, ctx.f_section, ctx.cw - ctx.p(16)) if sec.title else []
    )


def _section_panel_height(sec: _Section, ctx: _Ctx) -> int:
    h = ctx.panel_pad * 2
    title_lines = _section_title_lines(sec, ctx)
    if title_lines:
        h += len(title_lines) * ctx.p(30) + (ctx.p(10) if sec.elements else 0)
    for elem in sec.elements:
        h += elem.height(ctx)
    return h


def _render_section(sec: _Section, ctx: _Ctx, y: int) -> int:
    panel_h = _section_panel_height(sec, ctx)
    ctx.draw.rounded_rectangle(
        [ctx.margin, y, ctx.width - ctx.margin, y + panel_h],
        radius=ctx.p(10),
        fill=ctx.theme["panel"],
        outline=ctx.theme["panel_border"],
        width=ctx.p(1),
    )
    tx = ctx.margin + ctx.panel_pad
    ty = y + ctx.panel_pad
    title_lines = _section_title_lines(sec, ctx)
    if title_lines:
        lh = ctx.p(30)
        ctx.draw.rounded_rectangle(
            [tx, ty + ctx.p(6), tx + ctx.p(4), ty + lh - ctx.p(6)],
            radius=ctx.p(2),
            fill=ctx.theme["accent"],
        )
        for line in title_lines:
            ctx.draw.text(
                (tx + ctx.p(14), ty + lh / 2),
                line,
                font=ctx.f_section,
                fill=ctx.theme["heading"],
                anchor="lm",
            )
            ty += lh
        if sec.elements:
            ty += ctx.p(10)
    for elem in sec.elements:
        ty = elem.render(ctx, tx, ty)
    return y + panel_h


# ─── 来源区域 ───────────────────────────────────────────────


def _source_layout(
    sources: list[dict[str, str]], ctx: _Ctx
) -> list[tuple[str, list[str], list[str]]]:
    """返回 [(编号, 标题行, URL 行)]；compact 条目只占一行（缩短后的 URL，超宽截断）"""
    idx_w = ctx.p(30)
    out = []
    for i, src in enumerate(sources, 1):
        idx = str(src.get("idx") or i)
        title = _sanitize(src.get("title", "") or "").strip()
        url = (src.get("url", "") or "").strip()
        if src.get("compact"):
            out.append(
                (idx, [_ellipsize(_short_url(url), ctx.f_source, ctx.cw - idx_w)], [])
            )
            continue
        tl = _wrap_plain(title or url, ctx.f_source, ctx.cw - idx_w)
        ul = _wrap_plain(url, ctx.f_url, ctx.cw - idx_w) if (title and url) else []
        out.append((idx, tl, ul))
    return out


def _sources_panel_height(sources: list[dict[str, str]], ctx: _Ctx) -> int:
    if not sources:
        return 0
    h = ctx.panel_pad * 2 + ctx.p(30) + ctx.p(6)
    for _, tl, ul in _source_layout(sources, ctx):
        h += len(tl) * ctx.p(24) + len(ul) * ctx.p(20) + ctx.p(8)
    return h


def _render_sources_panel(
    sources: list[dict[str, str]], ctx: _Ctx, y: int, label: str = "参考来源"
) -> int:
    if not sources:
        return y
    panel_h = _sources_panel_height(sources, ctx)
    ctx.draw.rounded_rectangle(
        [ctx.margin, y, ctx.width - ctx.margin, y + panel_h],
        radius=ctx.p(10),
        fill=ctx.theme["source_panel"],
        outline=ctx.theme["panel_border"],
        width=ctx.p(1),
    )
    tx = ctx.margin + ctx.panel_pad
    ty = y + ctx.panel_pad
    lh = ctx.p(30)
    ctx.draw.rounded_rectangle(
        [tx, ty + ctx.p(7), tx + ctx.p(4), ty + lh - ctx.p(7)],
        radius=ctx.p(2),
        fill=ctx.theme["accent"],
    )
    ctx.draw.text(
        (tx + ctx.p(14), ty + lh / 2),
        f"{label} · {len(sources)}",
        font=ctx.f_ui_bold,
        fill=ctx.theme["heading"],
        anchor="lm",
    )
    ty += lh + ctx.p(6)

    idx_w = ctx.p(30)
    for idx, tl, ul in _source_layout(sources, ctx):
        badge = ctx.p(20)
        by = ty + (ctx.p(24) - badge) / 2
        ctx.draw.rounded_rectangle(
            [tx, by, tx + badge, by + badge],
            radius=ctx.p(5),
            fill=ctx.theme["accent_soft"],
        )
        ctx.draw.text(
            (tx + badge / 2, by + badge / 2),
            idx,
            font=ctx.f_url,
            fill=ctx.theme["source_idx"],
            anchor="mm",
        )
        has_title = bool(ul)
        for line in tl:
            ctx.draw.text(
                (tx + idx_w, ty + ctx.p(12)),
                line,
                font=ctx.f_source,
                fill=ctx.theme["text"] if has_title else ctx.theme["link"],
                anchor="lm",
            )
            ty += ctx.p(24)
        for line in ul:
            ctx.draw.text(
                (tx + idx_w, ty + ctx.p(10)),
                line,
                font=ctx.f_url,
                fill=ctx.theme["link"],
                anchor="lm",
            )
            ty += ctx.p(20)
        ty += ctx.p(8)

    return y + panel_h


# ─── 页眉 / 页脚 ────────────────────────────────────────────


def _header_height(ctx: _Ctx) -> int:
    return ctx.p(46) + ctx.p(30)


def _render_header(ctx: _Ctx, y: int, title: str, version: str, timestamp: str) -> int:
    logo = ctx.p(46)
    x = ctx.margin
    ctx.draw.rounded_rectangle(
        [x, y, x + logo, y + logo],
        radius=ctx.p(11),
        fill=ctx.theme["accent_soft"],
    )
    mask = _logo_mask(logo - ctx.p(16))
    if mask is not None:
        ctx.draw.bitmap((x + ctx.p(8), y + ctx.p(8)), mask, fill=ctx.theme["accent"])
    else:
        ctx.draw.text(
            (x + logo / 2, y + logo / 2),
            "G",
            font=ctx.f_title,
            fill=ctx.theme["accent"],
            anchor="mm",
        )

    tx = x + logo + ctx.p(14)
    ctx.draw.text(
        (tx, y + ctx.p(15)),
        title,
        font=ctx.f_title,
        fill=ctx.theme["heading"],
        anchor="lm",
    )
    sub = "GROK WEB SEARCH" + (f"  ·  {timestamp}" if timestamp else "")
    ctx.draw.text(
        (tx, y + ctx.p(36)), sub, font=ctx.f_sub, fill=ctx.theme["dim"], anchor="lm"
    )

    if version:
        bw = ctx.f_badge.getlength(version) + ctx.p(22)
        bh = ctx.p(26)
        bx = ctx.width - ctx.margin - bw
        by = y + (logo - bh) / 2
        ctx.draw.rounded_rectangle(
            [bx, by, bx + bw, by + bh],
            radius=bh / 2,
            fill=ctx.theme["accent_soft"],
            outline=ctx.theme["accent"],
            width=ctx.p(1),
        )
        ctx.draw.text(
            (bx + bw / 2, by + bh / 2),
            version,
            font=ctx.f_badge,
            fill=ctx.theme["accent"],
            anchor="mm",
        )

    ly = y + logo + ctx.p(16)
    ctx.draw.line(
        [(ctx.margin, ly), (ctx.width - ctx.margin, ly)],
        fill=ctx.theme["divider"],
        width=ctx.p(1),
    )
    return y + _header_height(ctx)


def _footer_height(ctx: _Ctx, has_meta: bool, repo: str) -> int:
    rows = int(has_meta) + int(bool(repo))
    return ctx.p(16) + rows * ctx.p(24) if rows else 0


def _render_footer(ctx: _Ctx, y: int, model: str, meta: str, repo: str) -> int:
    if not (model or meta or repo):
        return y
    ctx.draw.line(
        [(ctx.margin, y), (ctx.width - ctx.margin, y)],
        fill=ctx.theme["divider"],
        width=ctx.p(1),
    )
    y += ctx.p(16)
    row = ctx.p(24)
    left, right = ctx.margin, ctx.width - ctx.margin
    if model or meta:
        cy = y + row / 2
        if model:
            label = "MODEL"
            ctx.draw.text(
                (left, cy),
                label,
                font=ctx.f_ui_bold,
                fill=ctx.theme["accent"],
                anchor="lm",
            )
            ctx.draw.text(
                (left + ctx.f_ui_bold.getlength(label) + ctx.p(8), cy),
                model,
                font=ctx.f_ui,
                fill=ctx.theme["text"],
                anchor="lm",
            )
        if meta:
            ctx.draw.text(
                (right, cy), meta, font=ctx.f_ui, fill=ctx.theme["dim"], anchor="rm"
            )
        y += row
    if repo:
        cy = y + row / 2
        label = "REPO"
        ctx.draw.text(
            (left, cy),
            label,
            font=ctx.f_ui_bold,
            fill=ctx.theme["accent"],
            anchor="lm",
        )
        ctx.draw.text(
            (left + ctx.f_ui_bold.getlength("MODEL") + ctx.p(8), cy),
            repo,
            font=ctx.f_ui,
            fill=ctx.theme["dim"],
            anchor="lm",
        )
        y += row
    return y


# ─── 公开 API ───────────────────────────────────────────────

# 超采样后单张图片像素上限；超出时自动退回 1x，避免超长结果占用过多内存
_MAX_PIXELS = 24_000_000


def _layout_height(
    ctx: _Ctx,
    sections: list[_Section],
    sources: list[dict[str, str]],
    has_meta: bool,
    repo: str,
) -> int:
    total = ctx.margin + _header_height(ctx) + ctx.gap
    for sec in sections:
        total += _section_panel_height(sec, ctx) + ctx.gap
    if sources:
        total += _sources_panel_height(sources, ctx) + ctx.gap
    total += ctx.p(4) + _footer_height(ctx, has_meta, repo) + ctx.margin
    return total


def render_search_card(
    content: str,
    sources: list[dict[str, str]] | None = None,
    model: str = "",
    elapsed_ms: int = 0,
    total_tokens: int = 0,
    width: int = 800,
    output_path: str | None = None,
    theme: str = "auto",
    *,
    plugin_version: str | None = None,
    repo_url: str | None = None,
    title: str | None = None,
    timestamp: str | None = None,
    scale: float = 2.0,
    show_citations: bool = True,
) -> str | bytes:
    """将搜索结果渲染为面板风格卡片图片

    Args:
        content:        搜索结果正文（支持 Markdown 子集）
        sources:        来源列表 [{url, title, snippet}]（不传则不渲染来源区域）
        model:          模型名称
        elapsed_ms:     耗时毫秒
        total_tokens:   token 用量
        width:          图片逻辑宽度（实际像素 = width * scale）
        output_path:    保存路径；None 时返回 PNG bytes
        theme:          'auto'(按时间自动) / 'dark' / 'light'
        plugin_version: 页眉版本号；None 时读取 metadata.yaml，"" 不显示
        repo_url:       页脚仓库地址；None 时读取 metadata.yaml，"" 不显示
        title:          页眉标题；None 时使用 metadata.yaml 的 display_name
        timestamp:      页眉时间文本；None 时取当前本地时间，"" 不显示
        scale:          超采样倍率，默认 2x 保证手机端清晰
        show_citations: 正文含 [[n]](url) 引用且未传 sources 时，在底部列出引用地址

    Returns:
        文件路径 str 或 PNG bytes
    """
    sources = sources or []
    # 正文引用角标对应的地址：未单独传入来源时，以紧凑列表附在卡片底部
    panel_label = "参考来源"
    panel = sources
    if not sources and show_citations:
        panel_label = "文中引用"
        panel = [
            {"idx": n, "url": u, "compact": True}
            for n, u in _collect_citations(_sanitize(content))
        ]
    meta = _plugin_meta()
    if plugin_version is None:
        plugin_version = meta.get("version", "")
    if plugin_version and not plugin_version.lower().startswith("v"):
        plugin_version = f"v{plugin_version}"
    if repo_url is None:
        repo_url = meta.get("repo", "")
    repo = re.sub(r"^https?://", "", repo_url or "").rstrip("/")
    if title is None:
        title = meta.get("display_name", "") or "Grok 联网搜索"
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    meta_parts = []
    if elapsed_ms:
        meta_parts.append(f"{elapsed_ms / 1000:.1f}s")
    if total_tokens:
        meta_parts.append(f"{total_tokens:,} tokens")
    meta_text = "  ·  ".join(meta_parts)
    has_meta = bool(model or meta_text)

    palette = _get_theme(theme)
    ctx = _Ctx(width=width, scale=scale, theme=palette)
    sections = _parse_to_sections(content)
    total_h = _layout_height(ctx, sections, panel, has_meta, repo)
    if scale > 1 and ctx.width * total_h > _MAX_PIXELS:
        # 元素缓存了按旧倍率换行的结果，降级时需重新解析
        ctx = _Ctx(width=width, scale=1.0, theme=palette)
        sections = _parse_to_sections(content)
        total_h = _layout_height(ctx, sections, panel, has_meta, repo)

    # ── 正式绘制 ──
    ctx.create_canvas(total_h)
    y = ctx.margin

    # 1) 页眉：logo + 插件名 + 时间 + 版本号
    y = _render_header(ctx, y, title, plugin_version, timestamp) + ctx.gap

    # 2) 内容面板
    for sec in sections:
        y = _render_section(sec, ctx, y) + ctx.gap

    # 3) 来源面板
    if panel:
        y = _render_sources_panel(panel, ctx, y, panel_label) + ctx.gap

    # 4) 页脚：模型 / 耗时 / token / 仓库地址
    _render_footer(ctx, y + ctx.p(4), model, meta_text, repo)

    # ── 输出 ──
    if output_path:
        ctx.img.save(output_path)
        return output_path

    buf = BytesIO()
    ctx.img.save(buf, format="PNG")
    return buf.getvalue()
