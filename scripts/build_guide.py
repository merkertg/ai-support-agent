"""Наглядный PDF-гайд AI Support Agent с практикой и результатами проверок."""

import argparse
import json
import shutil
import tempfile
from datetime import datetime
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def register_fonts():
    """Зарегистрировать кириллицу PT на Mac или DejaVu Sans на Linux."""
    mac_sans = Path("/System/Library/Fonts/Supplemental/PTSans.ttc")
    mac_mono = Path("/System/Library/Fonts/Supplemental/PTMono.ttc")
    if mac_sans.is_file() and mac_mono.is_file():
        font_files = {
            "PT": (mac_sans, 0),
            "PT-Bold": (mac_sans, 7),
            "PT-Italic": (mac_sans, 1),
            "PT-BoldItalic": (mac_sans, 6),
            "Mono": (mac_mono, 1),
            "Mono-Bold": (mac_mono, 0),
        }
    else:
        root = Path("/usr/share/fonts/truetype/dejavu")
        font_files = {
            "PT": (root / "DejaVuSans.ttf", 0),
            "PT-Bold": (root / "DejaVuSans-Bold.ttf", 0),
            "PT-Italic": (root / "DejaVuSans-Oblique.ttf", 0),
            "PT-BoldItalic": (root / "DejaVuSans-BoldOblique.ttf", 0),
            "Mono": (root / "DejaVuSansMono.ttf", 0),
            "Mono-Bold": (root / "DejaVuSansMono-Bold.ttf", 0),
        }
    missing = [str(path) for path, _ in font_files.values() if not path.is_file()]
    if missing:
        raise RuntimeError("Не найдены шрифты с кириллицей: " + ", ".join(missing))
    for name, (path, index) in font_files.items():
        pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=index))
    pdfmetrics.registerFontFamily(
        "PT",
        normal="PT",
        bold="PT-Bold",
        italic="PT-Italic",
        boldItalic="PT-BoldItalic",
    )
    pdfmetrics.registerFontFamily("Mono", normal="Mono", bold="Mono-Bold")


# ---------- Цвета ----------
HEX = colors.HexColor
INK = HEX("#0F172A")
TEXT = HEX("#1E293B")
MUTED = HEX("#64748B")
LINE = HEX("#CBD5E1")
SOFT = HEX("#F1F5F9")
WHITE = colors.white
BLUE, BLUE_L = HEX("#2563EB"), HEX("#DBEAFE")
TEAL, TEAL_L = HEX("#0F766E"), HEX("#CCFBF1")
ORANGE, ORANGE_L = HEX("#C2410C"), HEX("#FFEDD5")
VIOLET, VIOLET_L = HEX("#6D28D9"), HEX("#EDE9FE")
GREEN, GREEN_L = HEX("#15803D"), HEX("#DCFCE7")
AMBER, AMBER_L = HEX("#B45309"), HEX("#FEF3C7")
SLATE_L = HEX("#E2E8F0")
SERIES = HEX("#2A78D6")  # графики: одна серия, один синий

PAGE_W, PAGE_H = A4
MARGIN = 17 * mm
W = PAGE_W - 2 * MARGIN

# ---------- Стили текста ----------
BODY = ParagraphStyle("body", fontName="PT", fontSize=10.5, leading=15, textColor=TEXT)
BODY_S = ParagraphStyle("bodys", parent=BODY, fontSize=9.5, leading=13.5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.6, leading=12, textColor=MUTED)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=9, leading=12.2)
CELL_B = ParagraphStyle("cellb", parent=CELL, fontName="PT-Bold")
CELL_M = ParagraphStyle(
    "cellm", parent=CELL, fontName="Mono", fontSize=8.3, leading=11.5
)
HEAD = ParagraphStyle("head", parent=CELL, fontName="PT-Bold", textColor=WHITE)
H2 = ParagraphStyle(
    "h2",
    parent=BODY,
    fontName="PT-Bold",
    fontSize=13,
    leading=17,
    textColor=INK,
    spaceBefore=4,
    spaceAfter=4,
)
CODE = ParagraphStyle(
    "code", fontName="Mono", fontSize=8.6, leading=11.8, textColor=INK
)


def p(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    """Абзац с разметкой reportlab; стрелку берём из PT Mono (в PT Sans её нет)."""
    return Paragraph(text.replace("→", '<font name="Mono">→</font>'), style)


def draw_text(c, x, y, text, font, size, align="left"):
    """Нарисовать строку; символ → рисуется шрифтом Mono, остальное - font."""
    parts = text.split("→")
    widths = [pdfmetrics.stringWidth(part, font, size) for part in parts]
    arrow_w = pdfmetrics.stringWidth("→", "Mono", size)
    total = sum(widths) + arrow_w * (len(parts) - 1)
    if align == "center":
        x -= total / 2
    elif align == "right":
        x -= total
    for index, part in enumerate(parts):
        c.setFont(font, size)
        c.drawString(x, y, part)
        x += widths[index]
        if index < len(parts) - 1:
            c.setFont("Mono", size)
            c.drawString(x, y, "→")
            x += arrow_w


def mono(text: str) -> str:
    """Встроенный моноширинный фрагмент."""
    return f'<font name="Mono" size="8.8" color="#0F172A">{text}</font>'


# ---------- Примитивы рисования ----------
def rbox(c, x, y, w, h, fill, stroke=None, r=6, lw=1, dash=None):
    """Скруглённый прямоугольник."""
    c.saveState()
    c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
    if dash:
        c.setDash(dash)
    c.roundRect(x, y, w, h, r, stroke=1 if stroke is not None else 0, fill=1)
    c.restoreState()


def lines_in(
    c, cx, top, width, text, font="PT", size=9, leading=None, color=TEXT, align="center"
):
    """Перенести текст по ширине и нарисовать сверху вниз; вернуть нижнюю y."""
    leading = leading or size * 1.28
    c.saveState()
    c.setFillColor(color)
    c.setFont(font, size)
    y = top - size
    for paragraph in text.split("\n"):
        for line in simpleSplit(paragraph, font, size, width):
            draw_text(c, cx, y, line, font, size, align)
            y -= leading
    c.restoreState()
    return y + leading - size


def node(
    c,
    x,
    y,
    w,
    h,
    title,
    sub="",
    fill=BLUE_L,
    edge=BLUE,
    tsize=10,
    ssize=8,
    dash=None,
    title_color=INK,
):
    """Блок схемы: заголовок и пояснение по центру."""
    rbox(c, x, y, w, h, fill, edge, r=7, lw=1.1, dash=dash)
    sub_lines = []
    for paragraph in sub.split("\n") if sub else []:
        sub_lines += simpleSplit(paragraph, "PT", ssize, w - 12)
    title_lines = simpleSplit(title, "PT-Bold", tsize, w - 10)
    total = len(title_lines) * tsize * 1.2 + len(sub_lines) * ssize * 1.3
    if sub_lines:
        total += 3
    top = y + h / 2 + total / 2
    lines_in(
        c, x + w / 2, top, w - 10, title, "PT-Bold", tsize, tsize * 1.2, title_color
    )
    if sub_lines:
        top -= len(title_lines) * tsize * 1.2 + 3
        lines_in(c, x + w / 2, top, w - 12, sub, "PT", ssize, ssize * 1.3, MUTED)


def arrow(
    c,
    x1,
    y1,
    x2,
    y2,
    color=MUTED,
    lw=1.3,
    head=5.5,
    dash=None,
    label=None,
    label_dx=0,
    label_dy=4,
    label_color=None,
):
    """Линия со стрелкой на конце и необязательной подписью посередине."""
    import math

    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(lw)
    if dash:
        c.setDash(dash)
    angle = math.atan2(y2 - y1, x2 - x1)
    ex, ey = x2 - head * 0.9 * math.cos(angle), y2 - head * 0.9 * math.sin(angle)
    c.line(x1, y1, ex, ey)
    c.setDash([])
    path = c.beginPath()
    path.moveTo(x2, y2)
    path.lineTo(x2 - head * math.cos(angle - 0.42), y2 - head * math.sin(angle - 0.42))
    path.lineTo(x2 - head * math.cos(angle + 0.42), y2 - head * math.sin(angle + 0.42))
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    if label:
        c.setFont("PT", 7.8)
        c.setFillColor(label_color or MUTED)
        c.drawCentredString((x1 + x2) / 2 + label_dx, (y1 + y2) / 2 + label_dy, label)
    c.restoreState()


def poly_arrow(c, points, color=MUTED, lw=1.3, head=5.5, dash=None):
    """Ломаная со стрелкой на последнем отрезке."""
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(lw)
    if dash:
        c.setDash(dash)
    for (ax, ay), (bx, by) in zip(points[:-2], points[1:-1], strict=True):
        c.line(ax, ay, bx, by)
    c.restoreState()
    (ax, ay), (bx, by) = points[-2], points[-1]
    arrow(c, ax, ay, bx, by, color, lw, head, dash)


def circle_num(c, x, y, r, text, fill=BLUE, color=WHITE, size=10):
    """Кружок с номером шага."""
    c.saveState()
    c.setFillColor(fill)
    c.circle(x, y, r, stroke=0, fill=1)
    c.setFillColor(color)
    c.setFont("PT-Bold", size)
    c.drawCentredString(x, y - size * 0.35, text)
    c.restoreState()


def chip(c, x, y, text, fill, color, font="Mono", size=7.6, pad=4, h=13):
    """Плашка с текстом (метод HTTP, путь, имя функции); вернуть ширину."""
    width = pdfmetrics.stringWidth(text, font, size) + pad * 2
    rbox(c, x, y, width, h, fill, None, r=3)
    c.saveState()
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x + pad, y + (h - size) / 2 + 1.2, text)
    c.restoreState()
    return width


class Diagram(Flowable):
    """Флоуабл с собственной функцией рисования."""

    def __init__(self, height, draw, width=W):
        """Сохранить параметры блока и подготовить размеры для вёрстки."""
        super().__init__()
        self.width, self.height, self.draw_fn = width, height, draw

    def wrap(self, *_):
        """Вернуть занимаемые размеры блока для расчёта страницы."""
        return self.width, self.height

    def draw(self):
        """Нарисовать блок на холсте текущей страницы."""
        self.draw_fn(self.canv, self.width, self.height)


class SectionTitle(Flowable):
    """Номер раздела, заголовок и подзаголовок."""

    def __init__(self, number, title, subtitle, color=BLUE):
        """Сохранить параметры блока и подготовить размеры для вёрстки."""
        super().__init__()
        self.number, self.title, self.subtitle, self.color = (
            number,
            title,
            subtitle,
            color,
        )
        self.sub_lines = simpleSplit(subtitle, "PT", 10.5, W - 46)
        self.height = 36 + len(self.sub_lines) * 14

    def wrap(self, *_):
        """Вернуть занимаемые размеры блока для расчёта страницы."""
        return W, self.height

    def draw(self):
        """Нарисовать блок на холсте текущей страницы."""
        c = self.canv
        top = self.height
        rbox(c, 0, top - 30, 34, 30, self.color, None, r=7)
        c.setFillColor(WHITE)
        c.setFont("PT-Bold", 15)
        c.drawCentredString(17, top - 21, self.number)
        c.setFillColor(INK)
        c.setFont("PT-Bold", 21)
        c.drawString(46, top - 22, self.title)
        c.setFillColor(MUTED)
        c.setFont("PT", 10.5)
        y = top - 40
        for line in self.sub_lines:
            c.drawString(46, y, line)
            y -= 14


def callout(text, color=BLUE, fill=BLUE_L, style=BODY_S):
    """Выделенный блок с цветной полосой слева."""
    table = Table([[p(text, style)]], colWidths=[W])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), fill),
                ("LINEBEFORE", (0, 0), (0, -1), 3.5, color),
                ("LEFTPADDING", (0, 0), (-1, -1), 11),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def code_block(text, width=W):
    """Блок команд моноширинным шрифтом на светлом фоне."""
    table = Table([[Preformatted(text, CODE)]], colWidths=[width])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SOFT),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def grid_table(rows, widths, head_fill=INK, zebra=True, styles=None):
    """Таблица с тёмной шапкой и полосами."""
    table = Table(rows, colWidths=widths, repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), head_fill),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if zebra:
        for index in range(1, len(rows)):
            if index % 2 == 0:
                commands.append(("BACKGROUND", (0, index), (-1, index), SOFT))
    table.setStyle(TableStyle(commands + (styles or [])))
    return table


def cards(items, columns=3, height=None, gap=10):
    """Ряд карточек: (заголовок, текст, цвет, светлый цвет)."""
    width = (W - gap * (columns - 1)) / columns
    cells = []
    for title, text, color, fill in items:
        inner = Table(
            [
                [
                    p(
                        f'<font color="{color.hexval().replace("0x", "#")}">'
                        f"<b>{title}</b></font>",
                        ParagraphStyle("ct", parent=BODY, fontSize=10.5, leading=13),
                    )
                ],
                [p(text, ParagraphStyle("cb", parent=BODY, fontSize=9, leading=12.6))],
            ],
            colWidths=[width],
        )
        inner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), fill),
                    ("LINEABOVE", (0, 0), (-1, 0), 3, color),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, 0), 8),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        cells.append(inner)
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    widths = []
    for _ in range(columns):
        widths += [width, gap]
    grid_rows = []
    for row in rows:
        line = []
        for cell in row:
            line += [cell, ""]
        while len(line) < columns * 2:
            line += ["", ""]
        grid_rows.append(line[:-1])
    table = Table(grid_rows, colWidths=widths[:-1], rowHeights=height)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), gap),
            ]
        )
    )
    return table


def stat_tiles(c, x, y, w, h, items, fill, value_color, label_color, edge=None):
    """Ряд плиток «число + подпись»."""
    gap = 8
    width = (w - gap * (len(items) - 1)) / len(items)
    for index, (value, label) in enumerate(items):
        left = x + index * (width + gap)
        rbox(c, left, y, width, h, fill, edge, r=8, lw=0.8)
        c.setFillColor(value_color)
        c.setFont("PT-Bold", 22)
        c.drawCentredString(left + width / 2, y + h - 30, value)
        lines_in(
            c,
            left + width / 2,
            y + h - 38,
            width - 12,
            label,
            "PT",
            8.4,
            10.6,
            label_color,
        )


# ---------- Колонтитулы ----------
def cover_page(c, doc):
    """Обложка рисуется целиком на холсте."""
    c.saveState()
    c.setFillColor(INK)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    # Декоративные круги - приглушённые, не отвлекают от текста.
    c.setFillColor(HEX("#1E293B"))
    c.circle(PAGE_W - 40, PAGE_H - 60, 170, stroke=0, fill=1)
    c.setFillColor(HEX("#172554"))
    c.circle(PAGE_W - 120, PAGE_H - 250, 70, stroke=0, fill=1)
    left = MARGIN + 4
    c.setFillColor(HEX("#5EEAD4"))
    c.setFont("PT-Bold", 10)
    c.drawString(left, PAGE_H - 92, "УЧЕБНЫЙ ПРОЕКТ  ·  ВЫМЫШЛЕННАЯ КОМПАНИЯ TECHFLOW")
    c.setFillColor(WHITE)
    c.setFont("PT-Bold", 44)
    c.drawString(left, PAGE_H - 150, "AI Support Agent")
    c.setFont("PT", 17)
    c.setFillColor(HEX("#CBD5E1"))
    y = PAGE_H - 186
    for line in simpleSplit(
        "Наглядный гайд по проекту: как ИИ-помощник готовит ответ оператору "
        "поддержки - от обращения клиента до одобренного человеком ответа.",
        "PT",
        17,
        W - 30,
    ):
        c.drawString(left, y, line)
        y -= 23

    # Мини-схема пути обращения.
    steps = [
        ("Обращение", "клиент пишет"),
        ("Агент", "собирает факты"),
        ("База знаний", "находит статьи"),
        ("Сотрудник", "проверяет"),
        ("Ответ", "доставлен"),
    ]
    top = PAGE_H - 360
    box_w, box_h, gap = 86, 54, 13
    total = len(steps) * box_w + (len(steps) - 1) * gap
    x0 = (PAGE_W - total) / 2
    accents = [
        HEX("#60A5FA"),
        HEX("#5EEAD4"),
        HEX("#A78BFA"),
        HEX("#FDBA74"),
        HEX("#86EFAC"),
    ]
    for index, (title, sub) in enumerate(steps):
        x = x0 + index * (box_w + gap)
        rbox(c, x, top, box_w, box_h, HEX("#1E293B"), accents[index], r=9, lw=1.4)
        c.setFillColor(WHITE)
        c.setFont("PT-Bold", 11)
        c.drawCentredString(x + box_w / 2, top + 31, title)
        c.setFillColor(HEX("#94A3B8"))
        c.setFont("PT", 8.6)
        c.drawCentredString(x + box_w / 2, top + 16, sub)
        if index < len(steps) - 1:
            arrow(
                c,
                x + box_w + 2,
                top + box_h / 2,
                x + box_w + gap - 2,
                top + box_h / 2,
                HEX("#94A3B8"),
                1.2,
                4.5,
            )
    c.setFillColor(HEX("#94A3B8"))
    c.setFont("PT-Italic", 9.5)
    c.drawCentredString(
        PAGE_W / 2,
        top - 22,
        "Решение всегда за человеком: агент только готовит черновик.",
    )

    stat_tiles(
        c,
        MARGIN,
        342,
        W,
        74,
        [
            ("10 / 10", "этапов дорожной карты готово"),
            (str(doc.verification["tests"]["total"]), "автоматических тестов"),
            ("28 / 35", "пути / операции HTTP API"),
            ("5", "инструментов агента"),
            ("5", "миграции базы данных"),
        ],
        HEX("#1E293B"),
        WHITE,
        HEX("#94A3B8"),
        HEX("#334155"),
    )
    c.setFillColor(HEX("#CBD5E1"))
    c.setFont("PT-Bold", 10)
    c.drawString(MARGIN, 300, "Что внутри")
    c.setFont("PT", 9.5)
    c.setFillColor(HEX("#94A3B8"))
    contents = [
        "1  Проект за минуту",
        "2  Архитектура",
        "3  Путь обращения",
        "4  Как работает агент",
        "5  База знаний (RAG)",
        "6  LangGraph и решение человека",
        "7  Данные и миграции",
        "8  Шпаргалка API",
        "9  Как запустить",
        "10  Проверки и CI/CD",
        "11  Интерфейс оператора",
        "12  Оценки и демонстрация",
        "13  Дорожная карта",
        "14  Словарь",
        "15  Итоги аудита",
        "16  Матрица CI",
        "17  Защита и доставка",
        "18  Полный прогон",
        "19  Путь оператора",
        "20  Перезапуск и копии",
        "21  Диагностика",
        "22  Покрытие и recovery",
        "23  Обновления и релизы",
        "24  Следующие шаги",
    ]
    for index, item in enumerate(contents):
        column, row = divmod(index, 12)
        c.drawString(MARGIN + column * 250, 282 - row * 15, item)
    c.setStrokeColor(HEX("#334155"))
    c.line(MARGIN, 82, PAGE_W - MARGIN, 82)
    c.setFillColor(HEX("#64748B"))
    c.setFont("PT", 8.6)
    c.drawString(
        MARGIN,
        66,
        f"Аудит {doc.verification['checked_at']} · "
        f"основа {doc.verification['base_commit']} · mock без платных запросов",
    )
    c.drawString(
        MARGIN,
        53,
        "Подробные пошаговые инструкции - в docs/STAGE_01-10.md, "
        "docs/DEMO.md и docs/instructions/",
    )
    c.restoreState()


def content_page(c, doc):
    """Нижний колонтитул обычной страницы."""
    c.saveState()
    c.setStrokeColor(LINE)
    c.setLineWidth(0.5)
    c.line(MARGIN, 13 * mm, PAGE_W - MARGIN, 13 * mm)
    c.setFillColor(MUTED)
    c.setFont("PT", 8)
    c.drawString(MARGIN, 9 * mm, "AI Support Agent · наглядный гайд")
    c.drawRightString(PAGE_W - MARGIN, 9 * mm, str(doc.page))
    c.restoreState()


# ---------- Схемы ----------
def mini_actors(c, w, h):
    """Кто участвует: клиент, система, сотрудник; ответ возвращается клиенту."""
    bw, bh = 128, 62
    y = h - bh - 2
    sx = (w - bw) / 2
    node(
        c,
        0,
        y,
        bw,
        bh,
        "Клиент",
        "пишет обращение\nо проблеме с продуктом",
        ORANGE_L,
        ORANGE,
    )
    node(
        c,
        sx,
        y,
        bw,
        bh,
        "Система",
        "собирает факты, ищет\nстатьи, готовит черновик",
        BLUE_L,
        BLUE,
    )
    node(
        c,
        w - bw,
        y,
        bw,
        bh,
        "Сотрудник",
        "одобряет (можно поправить)\nили отклоняет",
        GREEN_L,
        GREEN,
    )
    mid = y + bh / 2
    arrow(c, bw + 4, mid, sx - 4, mid, ORANGE, label="обращение", label_dy=5)
    arrow(c, sx + bw + 4, mid, w - bw - 4, mid, BLUE, label="черновик", label_dy=5)
    poly_arrow(
        c,
        [(w - bw / 2, y - 2), (w - bw / 2, 14), (bw / 2, 14), (bw / 2, y - 3)],
        GREEN,
        dash=[3, 2],
    )
    c.setFillColor(GREEN)
    c.setFont("PT", 8.2)
    c.drawCentredString(w / 2, 19, "одобренный ответ - сообщение в обращении клиента")


def architecture(c, w, h):
    """Компоненты системы и их связи."""
    # Клиенты слева.
    cw = 96
    node(
        c,
        0,
        h - 118,
        cw,
        100,
        "Клиенты API",
        "Swagger UI /docs\ncurl, smoke-тест\nStreamlit UI :8502\ndemo_scenario.py",
        SLATE_L,
        MUTED,
    )
    # Контейнер API в центре.
    ax, aw = cw + 26, 214
    rbox(c, ax, 8, aw, h - 16, HEX("#F8FAFC"), BLUE, r=10, lw=1.3)
    c.setFillColor(BLUE)
    c.setFont("PT-Bold", 11)
    c.drawString(ax + 10, h - 24, "api · FastAPI + Uvicorn")
    c.setFillColor(MUTED)
    c.setFont("PT", 8)
    c.drawString(ax + 10, h - 35, "один Docker-образ, порт 8001")
    inner_x, inner_w = ax + 10, aw - 20
    node(
        c,
        inner_x,
        h - 76,
        inner_w,
        30,
        "Маршруты /api/* и схемы",
        "",
        BLUE_L,
        BLUE,
        tsize=9.5,
    )
    node(
        c,
        inner_x,
        h - 116,
        inner_w,
        30,
        "Сервисы и транзакции",
        "",
        BLUE_L,
        BLUE,
        tsize=9.5,
    )
    half = (inner_w - 8) / 2
    node(
        c,
        inner_x,
        h - 178,
        half,
        52,
        "LangGraph",
        "процесс проверки:\nанализ → решение",
        GREEN_L,
        GREEN,
        tsize=9.5,
        ssize=7.6,
    )
    node(
        c,
        inner_x + half + 8,
        h - 178,
        half,
        52,
        "Агент",
        "цикл и 5 инструментов\nтолько чтение",
        VIOLET_L,
        VIOLET,
        tsize=9.5,
        ssize=7.6,
    )
    node(
        c,
        inner_x,
        h - 240,
        half,
        52,
        "Поиск статей",
        "эмбеддинги +\nфильтр продукта",
        TEAL_L,
        TEAL,
        tsize=9.5,
        ssize=7.6,
    )
    node(
        c,
        inner_x + half + 8,
        h - 240,
        half,
        52,
        "LLM-провайдер",
        "mock по умолчанию\nили OpenAI",
        AMBER_L,
        AMBER,
        tsize=9.5,
        ssize=7.6,
    )
    arrow(c, inner_x + inner_w / 2, h - 77, inner_x + inner_w / 2, h - 85, BLUE)
    arrow(c, inner_x + inner_w / 2, h - 117, inner_x + half / 2, h - 125, BLUE)
    arrow(c, inner_x + half, h - 152, inner_x + half + 8, h - 152, VIOLET)
    arrow(
        c,
        inner_x + half + 8 + half / 2,
        h - 179,
        inner_x + half / 2 + 20,
        h - 187,
        TEAL,
    )
    arrow(
        c,
        inner_x + half + 8 + half / 2,
        h - 179,
        inner_x + half + 8 + half / 2,
        h - 187,
        AMBER,
    )
    c.setFillColor(MUTED)
    c.setFont("PT", 7.8)
    c.drawString(inner_x, 18, "/health - процесс жив · /ready - БД и миграции")
    arrow(c, cw + 2, h - 68, ax - 2, h - 68, MUTED, label="HTTP / JSON", label_dy=5)

    # Хранилища справа.
    sx = ax + aw + 26
    sw = w - sx
    node(
        c,
        sx,
        196,
        sw,
        92,
        "PostgreSQL 17  ·  db",
        "клиенты, обращения, лицензии,\nзапуски агента, предложения,\n"
        "реестр статей, checkpoint LangGraph",
        HEX("#E0E7FF"),
        HEX("#4338CA"),
        ssize=7.8,
    )
    node(
        c,
        sx,
        124,
        sw,
        58,
        "Qdrant 1.19  ·  qdrant",
        "векторы 27 фрагментов\n7 статей базы знаний",
        TEAL_L,
        TEAL,
        ssize=7.8,
    )
    node(
        c,
        sx,
        64,
        sw,
        46,
        "OpenAI API",
        "необязательно; выключено,\nключ не задан",
        WHITE,
        MUTED,
        dash=[3, 2],
        ssize=7.6,
        title_color=MUTED,
    )
    arrow(c, ax + aw + 2, 240, sx - 2, 240, HEX("#4338CA"), label="SQL")
    arrow(c, ax + aw + 2, h - 214, sx - 2, 150, TEAL, label="поиск", label_dy=4)
    arrow(c, ax + aw + 2, h - 228, sx - 2, 86, MUTED, dash=[3, 2])
    # Разовые сервисы.
    node(
        c,
        sx,
        8,
        (sw - 6) / 2,
        36,
        "migrate",
        "alembic upgrade head",
        SOFT,
        LINE,
        tsize=9,
        ssize=7.2,
    )
    node(
        c,
        sx + (sw + 6) / 2,
        8,
        (sw - 6) / 2,
        36,
        "ingest",
        "индексация статей",
        SOFT,
        LINE,
        tsize=9,
        ssize=7.2,
    )


def journey(c, w, h):
    """Вертикальная шкала: восемь шагов пути обращения."""
    steps = [
        (
            "Создать данные",
            "Клиент, продукт, лицензия и обращение (статус open).",
            "POST /api/tickets",
            ORANGE,
        ),
        (
            "Запустить анализ",
            "Стартует граф LangGraph; первый узел - агент.",
            "POST /api/tickets/{id}/analyze",
            BLUE,
        ),
        (
            "Агент читает факты",
            "Обращение, клиент без email, история, лицензия по ключу "
            "оператора - только чтение.",
            "get_ticket · get_license",
            VIOLET,
        ),
        (
            "Агент ищет статьи",
            "Статьи продукта обращения и общие статьи в Qdrant.",
            "search_knowledge",
            TEAL,
        ),
        (
            "Черновик со ссылками",
            "summary, category, draft_reply, sources. Запуск completed, "
            "предложение pending.",
            "GET /api/agent-runs/{id}",
            VIOLET,
        ),
        (
            "Граф ждёт человека",
            "interrupt(): состояние сохранено в PostgreSQL, ждать можно "
            "сколько угодно.",
            "GET …/workflow → awaiting_review",
            GREEN,
        ),
        (
            "Сотрудник решает",
            "approve - можно отредактировать текст; reject - с причиной.",
            "POST /api/suggestions/{id}/review",
            GREEN,
        ),
        (
            "Ответ доставлен",
            "Сообщение оператора в обращении, статус waiting_customer, граф delivered.",
            "ticket_messages",
            ORANGE,
        ),
    ]
    row = h / len(steps)
    lx = 18
    c.saveState()
    c.setStrokeColor(LINE)
    c.setLineWidth(2)
    c.line(lx, row / 2, lx, h - row / 2)
    c.restoreState()
    for index, (title, text, endpoint, color) in enumerate(steps):
        cy = h - row * index - row / 2
        circle_num(c, lx, cy, 12, str(index + 1), color)
        c.setFillColor(INK)
        c.setFont("PT-Bold", 11)
        c.drawString(lx + 24, cy + 4, title)
        lines_in(c, lx + 24, cy - 1, 250, text, "PT", 8.8, 11.2, MUTED, align="left")
        width = pdfmetrics.stringWidth(endpoint, "Mono", 7.8) + 10
        chip(c, w - width, cy - 6, endpoint, SOFT, INK, size=7.8, h=14)


def agent_loop(c, w, h):
    """Цикл агента: модель ↔ сервер, выход - проверенный черновик."""
    model_x, model_w = 0, 120
    node(
        c,
        model_x,
        h - 92,
        model_w,
        74,
        "Модель",
        "mock или OpenAI\nпросит инструменты\nили отдаёт итог",
        AMBER_L,
        AMBER,
    )
    server_x = 170
    server_w = 150
    node(
        c,
        server_x,
        h - 92,
        server_w,
        74,
        "Сервер проверяет",
        "имя из списка 5 функций,\nстрогий JSON аргументов,\nлимиты шагов и вызовов",
        BLUE_L,
        BLUE,
    )
    tools_x = server_x + server_w + 50
    tools_w = w - tools_x
    node(
        c,
        tools_x,
        h - 92,
        tools_w,
        74,
        "Инструменты",
        "PostgreSQL: READ ONLY\nQdrant: поиск статей\nобласть - от сервера",
        VIOLET_L,
        VIOLET,
    )
    arrow(
        c, model_w + 3, h - 44, server_x - 3, h - 44, AMBER, label="вызовы", label_dy=5
    )
    arrow(
        c,
        server_x + server_w + 3,
        h - 44,
        tools_x - 3,
        h - 44,
        BLUE,
        label="выполнить",
        label_dy=5,
    )
    poly_arrow(
        c,
        [
            (tools_x + tools_w / 2, h - 93),
            (tools_x + tools_w / 2, h - 116),
            (model_w / 2, h - 116),
            (model_w / 2, h - 94),
        ],
        VIOLET,
    )
    c.setFillColor(VIOLET)
    c.setFont("PT", 7.8)
    c.drawCentredString(
        w / 2, h - 112, "результаты добавляются в историю и снова идут модели"
    )
    # Выход.
    node(
        c,
        0,
        4,
        model_w,
        52,
        "Итоговый JSON",
        "summary · category\ndraft_reply · sources",
        GREEN_L,
        GREEN,
        ssize=7.8,
    )
    node(
        c,
        server_x,
        4,
        server_w,
        52,
        "Проверка итога",
        "схема DraftContent; sources -\nтолько найденные статьи",
        BLUE_L,
        BLUE,
        ssize=7.8,
    )
    node(
        c,
        tools_x,
        4,
        tools_w,
        52,
        "Сохранение",
        "run completed + 1 pending\nили failed без предложения",
        HEX("#E0E7FF"),
        HEX("#4338CA"),
        ssize=7.8,
    )
    arrow(
        c,
        model_w / 2,
        h - 128 + 4,
        model_w / 2,
        58,
        AMBER,
        dash=[3, 2],
        label="итог",
        label_dx=14,
        label_dy=0,
    )
    arrow(c, model_w + 3, 30, server_x - 3, 30, GREEN)
    arrow(c, server_x + server_w + 3, 30, tools_x - 3, 30, BLUE)


def ingest_pipeline(c, w, h):
    """Индексация: Markdown → фрагменты → эмбеддинги → Qdrant + реестр."""
    steps = [
        ("knowledge/*.md", "7 статей: заголовок\nkey: value + текст", ORANGE_L, ORANGE),
        ("Фрагменты", "деление по «## »\n27 фрагментов ≤ 1200 симв.", BLUE_L, BLUE),
        ("Эмбеддинги", "mock: 256 чисел\nпо основам слов", VIOLET_L, VIOLET),
        ("Qdrant", "точки: вектор +\nключ, раздел, продукт", TEAL_L, TEAL),
    ]
    gap = 22
    bw = (w - gap * (len(steps) - 1)) / len(steps)
    top = h - 6
    bh = 60
    for index, (title, sub, fill, edge) in enumerate(steps):
        x = index * (bw + gap)
        node(c, x, top - bh, bw, bh, title, sub, fill, edge, ssize=7.8)
        if index:
            arrow(c, x - gap + 3, top - bh / 2, x - 3, top - bh / 2, MUTED)
    # Реестр.
    reg_w = bw * 1.6
    node(
        c,
        w - reg_w,
        2,
        reg_w,
        46,
        "Реестр в PostgreSQL",
        "knowledge_documents: контрольная сумма,\nчисло фрагментов, подпись модели",
        HEX("#E0E7FF"),
        HEX("#4338CA"),
        ssize=7.6,
    )
    arrow(c, w - bw / 2, top - bh - 2, w - bw / 2, 50, HEX("#4338CA"))
    c.setFillColor(HEX("#4338CA"))
    c.setFont("PT", 7.8)
    c.drawRightString(w - bw / 2 - 6, (top - bh + 50) / 2 - 3, "после записи векторов")
    c.setFillColor(MUTED)
    y = 36
    for line in [
        "Повторный запуск: без изменений → unchanged;",
        "изменённая статья → переиндексирована;",
        "удалённая из каталога → убрана отовсюду.",
    ]:
        draw_text(c, 0, y, line, "PT", 8.4)
        y -= 11


def score_chart(c, w, h):
    """Сходство фрагментов с запросом и порог 0.2 (одна серия, один цвет)."""
    data = [
        ("KB-SERVER-LOGS · Журналы и архив", 0.470),
        ("KB-SERVER-LOGS · Как собрать архив", 0.289),
        ("KB-SERVER-LOGS · Где находятся журналы", 0.283),
        ("KB-SERVER-LOGS · Что удалить перед отправкой", 0.220),
        ("KB-DESKTOP-SYNC · Частые причины", 0.177),
        ("KB-DESKTOP-SYNC · Что проверить", 0.091),
        ("KB-PASSWORD-RESET · Самостоятельный сброс", 0.080),
    ]
    label_w = 196
    chart_x = label_w + 8
    chart_w = w - chart_x - 30
    top = h - 22
    bar_h = 12
    step = 19
    scale = chart_w / 0.5
    c.setFillColor(INK)
    c.setFont("PT-Bold", 9.5)
    c.drawString(0, h - 10, "Запрос: «Где найти журнал сервера и собрать диагностику»")
    # Сетка: приглушённая.
    c.saveState()
    c.setStrokeColor(SLATE_L)
    c.setLineWidth(0.5)
    for tick in (0, 0.1, 0.2, 0.3, 0.4, 0.5):
        x = chart_x + tick * scale
        c.line(x, top - step * len(data) + 4, x, top + 2)
        c.setFillColor(MUTED)
        c.setFont("PT", 7.4)
        c.drawCentredString(x, top - step * len(data) - 6, f"{tick:.1f}")
    c.restoreState()
    threshold = chart_x + 0.2 * scale
    c.saveState()
    c.setStrokeColor(ORANGE)
    c.setDash([3, 2])
    c.setLineWidth(1.1)
    c.line(threshold, top - step * len(data) + 2, threshold, top + 8)
    c.restoreState()
    for index, (label, value) in enumerate(data):
        y = top - step * (index + 1) + 6
        passed = value >= 0.2
        c.setFillColor(TEXT if passed else MUTED)
        c.setFont("PT", 8.3)
        c.drawRightString(label_w, y + 2.5, label)
        width = value * scale
        # Скруглённый конец данных, ровное основание.
        c.saveState()
        c.setFillColor(SERIES if passed else HEX("#CBD5E1"))
        c.roundRect(chart_x, y, width, bar_h, 3, stroke=0, fill=1)
        c.rect(chart_x, y, min(4, width), bar_h, stroke=0, fill=1)
        c.restoreState()
        value_text = f"{value:.3f}"
        value_font = "PT-Bold" if passed else "PT"
        text_w = pdfmetrics.stringWidth(value_text, value_font, 8.2)
        c.setFillColor(WHITE)
        c.rect(chart_x + width + 2, y, text_w + 4, bar_h, stroke=0, fill=1)
        c.setFillColor(TEXT if passed else MUTED)
        c.setFont(value_font, 8.2)
        c.drawString(chart_x + width + 4, y + 2.5, value_text)
    c.setFillColor(ORANGE)
    c.setFont("PT-Bold", 8)
    c.drawString(threshold + 4, top + 2, "порог KNOWLEDGE_MIN_SCORE = 0.2")


def review_graph(c, w, h):
    """Граф LangGraph: анализ → ожидание решения → доставка или отклонение."""
    cy = h - 84
    sx = 0
    rbox(c, sx, cy - 14, 54, 28, INK, None, r=14)
    c.setFillColor(WHITE)
    c.setFont("PT-Bold", 9)
    c.drawCentredString(sx + 27, cy - 3, "START")
    ax = 78
    node(
        c,
        ax,
        cy - 30,
        110,
        60,
        "analyze",
        "агент этапов 05-06\nсоздаёт run + pending",
        VIOLET_L,
        VIOLET,
        ssize=7.6,
    )
    arrow(c, sx + 55, cy, ax - 3, cy, MUTED)
    hx = 222
    node(
        c,
        hx,
        cy - 34,
        128,
        68,
        "human_review",
        "interrupt()\nграф сохранён и ждёт\nрешения сотрудника",
        GREEN_L,
        GREEN,
        ssize=7.6,
    )
    arrow(c, ax + 111, cy, hx - 3, cy, MUTED, label="успех")
    # Пауза.
    c.saveState()
    c.setFillColor(GREEN)
    c.rect(hx + 112, cy + 18, 3, 10, stroke=0, fill=1)
    c.rect(hx + 118, cy + 18, 3, 10, stroke=0, fill=1)
    c.restoreState()
    dx = hx + 172
    node(
        c,
        dx,
        cy + 6,
        w - dx,
        46,
        "deliver",
        "approved + сообщение\nwaiting_customer",
        BLUE_L,
        BLUE,
        ssize=7.4,
    )
    node(
        c,
        dx,
        cy - 62,
        w - dx,
        46,
        "reject",
        "rejected + причина\nобращение без изменений",
        ORANGE_L,
        ORANGE,
        ssize=7.4,
    )
    arrow(c, hx + 129, cy + 10, dx - 3, cy + 28, BLUE, label="approve", label_dy=6)
    arrow(c, hx + 129, cy - 10, dx - 3, cy - 38, ORANGE, label="reject", label_dy=-14)
    # END.
    ex = dx + (w - dx) / 2 - 24
    rbox(c, ex, 4, 48, 24, INK, None, r=12)
    c.setFillColor(WHITE)
    c.setFont("PT-Bold", 8.5)
    c.drawCentredString(ex + 24, 13, "END")
    arrow(c, ex + 24, cy - 63, ex + 24, 29, ORANGE)
    poly_arrow(
        c,
        [(dx + w - dx - 6, cy + 6), (w - 2, cy + 6), (w - 2, 16), (ex + 49, 16)],
        BLUE,
    )
    poly_arrow(
        c, [(ax + 55, cy - 31), (ax + 55, 16), (ex - 2, 16)], VIOLET, dash=[3, 2]
    )
    c.setFillColor(VIOLET)
    c.setFont("PT", 7.8)
    c.drawString(ax + 60, 20, "анализ не удался (failed) - решать нечего")
    # Подпись про checkpoint.
    c.setFillColor(MUTED)
    c.setFont("PT-Italic", 8.2)
    c.drawString(
        0,
        h - 10,
        "После каждого узла LangGraph сохраняет состояние потока "
        "(checkpoint) в PostgreSQL.",
    )
    c.setFillColor(GREEN)
    draw_text(
        c, hx + 64, cy - 46, "POST /review → Command(resume)", "PT", 7.8, "center"
    )


def er_diagram(c, w, h):
    """Таблицы PostgreSQL и связи между ними."""

    def table(x, y, bw, name, fields, fill, edge):
        """Нарисовать схему «table» для наглядного руководства."""
        rows = len(fields)
        bh = 18 + rows * 11 + 6
        rbox(c, x, y - bh, bw, bh, WHITE, edge, r=5, lw=1)
        c.saveState()
        c.setFillColor(fill)
        c.roundRect(x, y - 18, bw, 18, 5, stroke=0, fill=1)
        c.rect(x, y - 18, bw, 8, stroke=0, fill=1)
        c.restoreState()
        c.setFillColor(INK)
        c.setFont("Mono-Bold", 8.4)
        c.drawString(x + 6, y - 12.5, name)
        c.setFont("Mono", 7.4)
        c.setFillColor(TEXT)
        yy = y - 29
        for field in fields:
            c.drawString(x + 6, yy, field)
            yy -= 11
        return x, y - bh, bw, bh

    bw = 112
    col = [0, 128, 256, w - bw]
    customers = table(
        col[0], h, bw, "customers", ["id", "name, email", "company"], ORANGE_L, ORANGE
    )
    products = table(
        col[0], h - 72, bw, "products", ["id", "name, code"], ORANGE_L, ORANGE
    )
    licenses = table(
        col[1],
        h,
        bw,
        "licenses",
        ["customer_id →", "product_id →", "license_key", "status, сроки"],
        ORANGE_L,
        ORANGE,
    )
    tickets = table(
        col[1],
        h - 86,
        bw,
        "tickets",
        ["customer_id →", "product_id →", "subject, description", "priority, status"],
        ORANGE_L,
        ORANGE,
    )
    messages = table(
        col[2],
        h - 86,
        bw,
        "ticket_messages",
        ["ticket_id →", "user_id →", "author_type", "content"],
        BLUE_L,
        BLUE,
    )
    users = table(
        col[2], h, bw, "users", ["id", "name, email", "role, is_active"], BLUE_L, BLUE
    )
    runs = table(
        col[1],
        h - 172,
        bw,
        "agent_runs",
        ["ticket_id →", "status, provider", "tool_trace, result", "workflow_thread_id"],
        VIOLET_L,
        VIOLET,
    )
    suggestions = table(
        col[2],
        h - 172,
        bw,
        "agent_suggestions",
        [
            "agent_run_id →",
            "status, content",
            "final_content",
            "reviewed_by →",
            "delivered_message_id →",
        ],
        VIOLET_L,
        VIOLET,
    )
    table(
        col[3],
        h,
        bw,
        "knowledge_documents",
        ["document_key", "product_code", "checksum", "chunk_count"],
        TEAL_L,
        TEAL,
    )
    table(
        col[3],
        h - 86,
        bw,
        "checkpoint_*",
        [
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        ],
        GREEN_L,
        GREEN,
    )
    c.setFillColor(MUTED)
    c.setFont("PT", 7.8)
    lines_in(
        c,
        col[3],
        h - 172,
        bw,
        "Состояние графов LangGraph (миграция 0004). Без моделей "
        "SQLAlchemy - alembic check их пропускает.",
        "PT",
        7.6,
        9.6,
        MUTED,
        align="left",
    )
    lines_in(
        c,
        col[3],
        h - 222,
        bw,
        "Тексты и векторы статей - в Qdrant; здесь только реестр.",
        "PT",
        7.6,
        9.6,
        MUTED,
        align="left",
    )

    def link(a, b, color=MUTED):
        """Связь от правого края a к левому краю b."""
        ax, ay, aw, ah = a
        bx, by, bw2, bh2 = b
        arrow(c, ax + aw, ay + ah / 2, bx, by + bh2 / 2, color, lw=0.9, head=4.5)

    link(customers, licenses, ORANGE)
    link(products, tickets, ORANGE)
    link(tickets, messages, BLUE)
    ux, uy, uw, uh = users
    arrow(
        c,
        ux + uw / 2,
        uy,
        ux + uw / 2,
        messages[1] + messages[3],
        BLUE,
        lw=0.9,
        head=4.5,
    )
    link(runs, suggestions, VIOLET)
    # Обращение → запуск (вертикально).
    tx, ty, tw, th = tickets
    arrow(c, tx + tw / 2, ty, tx + tw / 2, runs[1] + runs[3], VIOLET, lw=0.9, head=4.5)
    # Предложение → сообщение (доставка).
    sx, sy, sw2, sh = suggestions
    mx, my, mw, mh = messages
    arrow(
        c, sx + sw2 / 2, sy + sh, mx + mw / 2, my, GREEN, lw=0.9, head=4.5, dash=[2, 2]
    )


def migrations_timeline(c, w, h):
    """Пять миграций Alembic по этапам."""
    items = [
        ("0001", "этап 02", "9 таблиц, ключи,\nограничения", ORANGE),
        ("0002", "этап 05", "поля запуска агента:\ntrace, result", VIOLET),
        ("0003", "этап 06", "реестр статей\nбазы знаний", TEAL),
        ("0004", "этап 07", "решение сотрудника +\ncheckpoint LangGraph", GREEN),
        ("0005", "доступ", "токены сотрудников,\nроль reviewer", BLUE),
    ]
    y = h / 2 + 6
    c.saveState()
    c.setStrokeColor(LINE)
    c.setLineWidth(2)
    c.line(14, y, w - 14, y)
    c.restoreState()
    step = (w - 28) / (len(items) - 1)
    for index, (code, stage, text, color) in enumerate(items):
        x = 14 + index * step
        circle_num(c, x, y, 13, code[-1], color, size=10)
        c.setFillColor(INK)
        c.setFont("Mono-Bold", 8.6)
        c.drawCentredString(x, y + 20, code)
        c.setFillColor(color)
        c.setFont("PT-Bold", 8.4)
        c.drawCentredString(x, y + 31, stage)
        lines_in(c, x, y - 18, 108, text, "PT", 7.8, 9.8, MUTED)


def tests_chart(c, w, h):
    """Число автотестов по этапам: одна серия, значение на вершине столбца."""
    data = [
        ("02", 77),
        ("03", 199),
        ("04", 289),
        ("05", 441),
        ("06", 537),
        ("07", 597),
        ("08-10", 767),
    ]
    base = 22
    top = h - 16
    chart_h = top - base
    left = 26
    chart_w = w - left
    slot = chart_w / len(data)
    bar_w = min(24, slot * 0.5)
    c.saveState()
    for tick in (0, 200, 400, 600, 800):
        y = base + tick / 800 * chart_h
        c.setStrokeColor(SLATE_L)
        c.setLineWidth(0.5)
        c.line(left, y, w, y)
        c.setFillColor(MUTED)
        c.setFont("PT", 7.4)
        c.drawRightString(left - 4, y - 2.5, str(tick))
    c.restoreState()
    for index, (stage, value) in enumerate(data):
        x = left + slot * index + (slot - bar_w) / 2
        bh = value / 800 * chart_h
        c.saveState()
        c.setFillColor(SERIES)
        c.roundRect(x, base, bar_w, bh, 3, stroke=0, fill=1)
        c.rect(x, base, bar_w, min(4, bh), stroke=0, fill=1)
        c.restoreState()
        c.setFillColor(TEXT)
        c.setFont("PT-Bold", 8.4)
        c.drawCentredString(x + bar_w / 2, base + bh + 4, str(value))
        c.setFillColor(MUTED)
        c.setFont("PT", 8)
        c.drawCentredString(x + bar_w / 2, base - 12, f"этап {stage}")


def ci_pipeline(c, w, h):
    """Задания проходят четыре барьера: три проверки идут параллельно."""
    jobs = [
        (
            "1. delivery",
            "проверить цель\nстрогий vX.Y.Z\nтег из истории main",
            TEAL_L,
            TEAL,
        ),
        (
            "2. checks / security / ОС",
            "uv 0.12.23, 14 дн., Pytest\nпокрытие ≥ 85%, pip-audit\n"
            "Linux, macOS, Windows:\nтесты и запуск API/UI",
            BLUE_L,
            BLUE,
        ),
        (
            "3. container",
            "API + UI, arm64 QEMU\nTrivy, SBOM, smoke\n"
            "демо, перезапуск, restore\n4 tested image ID",
            VIOLET_L,
            VIOLET,
        ),
        (
            "4. publish",
            "API/UI amd64+arm64\nmain: SHA и latest\n"
            "тег: vX.Y.Z, без сборки\nSBOM; подпись по выбору",
            GREEN_L,
            GREEN,
        ),
    ]
    gap = 12
    bw = (w - gap * 3) / 4
    for index, (title, text, fill, edge) in enumerate(jobs):
        x = index * (bw + gap)
        node(c, x, 4, bw, h - 8, title, text, fill, edge, tsize=9, ssize=7.6)
        if index:
            arrow(c, x - gap + 2, h / 2, x - 2, h / 2, MUTED, head=4)


def ui_flow(c, w, h):
    """Интерфейс оператора: браузер → Streamlit → HTTP API → хранилища."""
    gap = 46
    bw = (w - gap * 3) / 4
    # Правый столбец: три хранилища сверху вниз в пределах высоты схемы.
    sh, sgap = 40, 6
    stores_top = h - 2
    stores_bottom = stores_top - 3 * sh - 2 * sgap
    bh = 74
    y = (stores_top + stores_bottom) / 2 - bh / 2
    steps = [
        ("Браузер", "оператор работает\nс формами", ORANGE_L, ORANGE),
        ("Streamlit", "operator_ui/\nотдельный образ UI", VIOLET_L, VIOLET),
        ("FastAPI", "/api/* - проверки\nи транзакции", BLUE_L, BLUE),
    ]
    for index, (title, sub, fill, edge) in enumerate(steps):
        x = index * (bw + gap)
        node(c, x, y, bw, bh, title, sub, fill, edge, ssize=7.8)
        if index:
            label = "страница" if index == 1 else "HTTP JSON"
            arrow(c, x - gap + 3, y + bh / 2, x - 3, y + bh / 2, MUTED, label=label)
    stores = [
        ("PostgreSQL", "данные и checkpoint", TEAL_L, TEAL),
        ("Qdrant", "статьи базы знаний", TEAL_L, TEAL),
        ("mock / OpenAI", "черновик и анализ", GREEN_L, GREEN),
    ]
    x = 3 * (bw + gap)
    for index, (title, sub, fill, edge) in enumerate(stores):
        sy = stores_top - (index + 1) * sh - index * sgap
        node(c, x, sy, bw, sh, title, sub, fill, edge, tsize=9, ssize=7.4)
        arrow(c, x - gap + 3, y + bh / 2, x - 3, sy + sh / 2, MUTED, lw=1)
    lines = [
        "У процесса UI есть только API_URL: ни DATABASE_URL, ни ключа модели.",
        "Все решения, SQL и вызовы модели остаются на сервере FastAPI.",
    ]
    for index, line in enumerate(lines):
        draw_text(c, 0, stores_bottom - 16 - index * 12, line, "PT", 8.2)


def demo_flow(c, w, h):
    """Демонстрация: скрипт готовит черновик, решение остаётся за человеком."""
    steps = [
        ("demo_scenario.py", "проверяет mock и индекс,\nсоздаёт записи", BLUE_L, BLUE),
        ("pending", "агент подготовил\nчерновик с источниками", AMBER_L, AMBER),
        ("Сотрудник в UI", "читает, правит,\nодобряет или отклоняет", VIOLET_L, VIOLET),
        ("delivered", "одно сообщение,\nwaiting_customer", GREEN_L, GREEN),
    ]
    gap = 20
    bw = (w - gap * (len(steps) - 1)) / len(steps)
    for index, (title, sub, fill, edge) in enumerate(steps):
        x = index * (bw + gap)
        node(c, x, 2, bw, h - 4, title, sub, fill, edge, tsize=9.5, ssize=7.6)
        if index:
            arrow(c, x - gap + 3, h / 2, x - 3, h / 2, MUTED)


def roadmap(c, w, h):
    """Десять этапов учебного проекта: все готовы."""
    stages = [
        ("01", "FastAPI, /health, первые тесты", "основа API, uv, Ruff, Pytest", True),
        (
            "02",
            "PostgreSQL, модели, миграции",
            "9 таблиц, Alembic, /ready, Docker Compose",
            True,
        ),
        ("03", "REST API ресурсов", "клиенты, продукты, лицензии, обращения", True),
        ("04", "LLM-черновик, mock и OpenAI", "строгий JSON, безопасные ошибки", True),
        (
            "05",
            "Агент с инструментами",
            "цикл tool calling, лимиты, сохранённый запуск",
            True,
        ),
        (
            "06",
            "База знаний, RAG, Qdrant",
            "индексация статей, поиск, ссылки агента",
            True,
        ),
        (
            "07",
            "LangGraph и решение человека",
            "одобрение, доставка, checkpoint в БД",
            True,
        ),
        (
            "08",
            "Интерфейс оператора",
            "Streamlit поверх API, очередь проверки, отдельный образ",
            True,
        ),
        (
            "09",
            "Оценки качества и безопасность",
            "32 вопроса поиска, 12 сценариев агента, пороги в CI",
            True,
        ),
        (
            "10",
            "Упаковка и демонстрация",
            "Compose с UI, повторяемый демо-сценарий, DEMO.md",
            True,
        ),
    ]
    row = h / len(stages)
    for index, (number, title, note, done) in enumerate(stages):
        y = h - row * (index + 1)
        color = GREEN if done else HEX("#94A3B8")
        fill = GREEN_L if done else SOFT
        rbox(c, 0, y + 3, w, row - 6, fill, None, r=6)
        circle_num(c, 18, y + row / 2, 11, number, color, size=8.8)
        c.setFillColor(INK if done else MUTED)
        c.setFont("PT-Bold", 10.5)
        c.drawString(40, y + row / 2 + 1, title)
        c.setFillColor(MUTED)
        c.setFont("PT", 8.4)
        c.drawString(40, y + row / 2 - 10, note)
        status = "готово" if done else "в планах"
        c.setFont("PT-Bold", 8.4)
        width = pdfmetrics.stringWidth(status, "PT-Bold", 8.4) + 14
        rbox(c, w - width - 8, y + row / 2 - 8, width, 16, color, None, r=8)
        c.setFillColor(WHITE)
        c.drawCentredString(w - width / 2 - 8, y + row / 2 - 3, status)


def section(story, number, title, subtitle, color=BLUE):
    """Начать раздел с новой страницы."""
    if len(story) > 2:
        story.append(PageBreak())
    story.append(SectionTitle(number, title, subtitle, color))
    story.append(Spacer(1, 8))


def method(name):
    """Цветной метод HTTP."""
    color = "#0F766E" if name == "GET" else "#2563EB"
    return p(f'<font name="Mono-Bold" color="{color}">{name}</font>', CELL_M)


def build_story(verification):
    """Собрать исходные страницы и дополнение по проверенным данным JSON."""
    story = [NextPageTemplate("content"), PageBreak()]
    # 1. Проект за минуту
    section(
        story,
        "1",
        "Проект за одну минуту",
        "Зачем нужен помощник оператора и что он делает - без технических деталей.",
    )
    story += [
        p(
            "Оператор технической поддержки тратит много времени "
            "на сбор фактов: кто клиент, "
            "действует ли его лицензия, обращался ли он раньше, "
            "что сказано в документации. "
            "<b>AI Support Agent</b> собирает эти факты "
            "автоматически и готовит черновик ответа "
            "со ссылками на статьи базы знаний. <b>Отправлять ли "
            "ответ, решает человек.</b>"
        ),
        Spacer(1, 10),
        Diagram(104, mini_actors),
        Spacer(1, 12),
        cards(
            [
                (
                    "Что делает",
                    "Читает обращение, клиента, лицензию и историю; ищет статьи; "
                    "готовит черновик со ссылками; ждёт решения "
                    "сотрудника; доставляет одобренный "
                    "ответ сообщением в обращении.",
                    BLUE,
                    BLUE_L,
                ),
                (
                    "Чего не делает",
                    "Не отправляет письма; не меняет обращение без человека; "
                    "не вызывает платную модель по умолчанию (режим "
                    "mock); не принимает запросы /api/* без токена "
                    "сотрудника.",
                    ORANGE,
                    ORANGE_L,
                ),
                (
                    "Главные принципы",
                    "Человек в контуре; агент только читает данные; "
                    "безопасные ошибки без секретов; каждое изменение "
                    "проверяют тесты и CI "
                    "перед публикацией образа.",
                    GREEN,
                    GREEN_L,
                ),
            ]
        ),
        p("Как устроен проект", H2),
        grid_table(
            [
                [p("Слой", HEAD), p("Технология", HEAD), p("Зачем", HEAD)],
                [
                    p("HTTP API", CELL_B),
                    p("FastAPI + Uvicorn", CELL),
                    p("Маршруты, проверка JSON, документация Swagger", CELL),
                ],
                [
                    p("Данные", CELL_B),
                    p("PostgreSQL 17, SQLAlchemy 2, Alembic", CELL),
                    p("Записи, транзакции, версии схемы", CELL),
                ],
                [
                    p("Агент", CELL_B),
                    p("Свой цикл tool calling", CELL),
                    p("5 инструментов только для чтения, лимиты, проверка итога", CELL),
                ],
                [
                    p("Поиск", CELL_B),
                    p("Qdrant 1.19 + эмбеддинги", CELL),
                    p("Статьи базы знаний по смыслу (mock - по словам)", CELL),
                ],
                [
                    p("Процесс", CELL_B),
                    p("LangGraph + PostgresSaver", CELL),
                    p("Ожидание решения человека, переживает перезапуск", CELL),
                ],
                [
                    p("Доставка", CELL_B),
                    p("Docker Compose, GitHub Actions, GHCR", CELL),
                    p("Запуск одной командой и проверенный образ", CELL),
                ],
            ],
            [70, 170, W - 240],
        ),
        Spacer(1, 10),
        callout(
            "<b>Важно:</b> TechFlow, клиенты и статьи - вымышленные. Режим <b>mock</b> "
            "по умолчанию работает без интернета и ключей: он показывает путь данных, "
            "но не качество настоящей модели. Подключение OpenAI "
            "подготовлено, но в проекте "
            "ещё не включалось.",
            AMBER,
            AMBER_L,
        ),
    ]

    # 2. Архитектура
    section(
        story,
        "2",
        "Архитектура",
        "Из каких частей состоит система и как они связаны.",
        BLUE,
    )
    story += [
        Diagram(300, architecture),
        Spacer(1, 12),
        p("Сервисы Docker Compose", H2),
        grid_table(
            [
                [
                    p("Сервис", HEAD),
                    p("Что это", HEAD),
                    p("Порт на Mac", HEAD),
                    p("Данные", HEAD),
                ],
                [
                    p(mono("db"), CELL),
                    p("PostgreSQL 17", CELL),
                    p("5433", CELL),
                    p("том postgres_data", CELL),
                ],
                [
                    p(mono("qdrant"), CELL),
                    p("Qdrant 1.19.1, телеметрия выключена", CELL),
                    p("6335", CELL),
                    p("том qdrant_data", CELL),
                ],
                [
                    p(mono("migrate"), CELL),
                    p("разовый: alembic upgrade head", CELL),
                    p("-", CELL),
                    p("-", CELL),
                ],
                [
                    p(mono("api"), CELL),
                    p("FastAPI; стартует после migrate и qdrant", CELL),
                    p("8001", CELL),
                    p("-", CELL),
                ],
                [
                    p(mono("ui"), CELL),
                    p("Streamlit; стартует после healthy api", CELL),
                    p("8502", CELL),
                    p("-", CELL),
                ],
                [
                    p(mono("db-test"), CELL),
                    p("тестовая PostgreSQL (профиль test)", CELL),
                    p("5434", CELL),
                    p("в памяти", CELL),
                ],
                [
                    p(mono("qdrant-test"), CELL),
                    p("тестовый Qdrant (профиль test)", CELL),
                    p("6336", CELL),
                    p("в памяти", CELL),
                ],
            ],
            [76, 210, 72, W - 358],
        ),
        Spacer(1, 10),
        callout(
            "Порядок запуска: <b>db healthy → migrate завершился "
            "→ qdrant healthy → api healthy "
            '→ ui healthy</b>. Обычный <font name="Mono">docker '
            "compose down</font> сохраняет "
            "тома: записи, индекс статей и ожидающие решения переживают пересоздание "
            'контейнеров. <font name="Mono">down --volumes</font> удаляет всё.'
        ),
    ]

    # 3. Путь обращения
    section(
        story,
        "3",
        "Путь обращения: от создания до ответа",
        "Восемь шагов, через которые проходит каждое "
        "обращение. Справа - где это видно в API.",
        ORANGE,
    )
    story += [
        Diagram(560, journey),
        Spacer(1, 10),
        callout(
            'Очередь на проверку: <font name="Mono">GET '
            "/api/suggestions?status=pending</font>. "
            "Каждый повторный анализ создаёт новый запуск и новое предложение; решение "
            "по предложению принимается один раз.",
            ORANGE,
            ORANGE_L,
        ),
    ]

    # 4. Агент
    section(
        story,
        "4",
        "Как работает агент",
        "Модель просит факты, сервер решает, что можно выполнить, и проверяет итог.",
        VIOLET,
    )
    story += [
        Diagram(196, agent_loop),
        Spacer(1, 10),
        grid_table(
            [
                [
                    p("Инструмент", HEAD),
                    p("Что возвращает", HEAD),
                    p("Ограничение", HEAD),
                ],
                [
                    p(mono("get_ticket"), CELL),
                    p("Текущее обращение и продукт", CELL),
                    p("только обращение этого запуска", CELL),
                ],
                [
                    p(mono("get_customer"), CELL),
                    p("Имя и компания клиента", CELL),
                    p("email не выдаётся", CELL),
                ],
                [
                    p(mono("get_license"), CELL),
                    p("Статус и признак valid по датам", CELL),
                    p("только клиент и продукт обращения; ключ в журнале - ***", CELL),
                ],
                [
                    p(mono("get_ticket_history"), CELL),
                    p("До 5 других обращений", CELL),
                    p("тот же клиент и продукт, без описаний", CELL),
                ],
                [
                    p(mono("search_knowledge"), CELL),
                    p("До 5 статей, по фрагменту от каждой", CELL),
                    p("продукт задаёт сервер: свои статьи + общие", CELL),
                ],
            ],
            [118, 156, W - 274],
        ),
        Spacer(1, 10),
        Diagram(
            66,
            lambda c, w, h: stat_tiles(
                c,
                0,
                0,
                w,
                h,
                [
                    ("≤ 6", "шагов модели за запуск"),
                    ("≤ 12", "вызовов инструментов"),
                    ("≤ 60 с", "на весь цикл"),
                    ("64 000", "символов истории максимум"),
                ],
                VIOLET_L,
                VIOLET,
                TEXT,
            ),
        ),
        Spacer(1, 10),
        callout(
            "<b>Безопасность:</b> имя функции и JSON модели "
            "никогда не превращаются в код "
            "или SQL. Транзакции инструментов - <b>READ ONLY</b> на уровне PostgreSQL. "
            'Ссылка в <font name="Mono">sources</font> на статью, '
            "которую поиск не находил, "
            "останавливает запуск с ошибкой 502. Ошибка после старта сохраняется как "
            '<b>failed</b> с <font name="Mono">run_id</font>, без предложения.',
            VIOLET,
            VIOLET_L,
        ),
    ]

    # 5. База знаний
    section(
        story,
        "5",
        "База знаний и поиск (RAG)",
        "RAG - ответ с опорой на найденные документы. Сначала поиск, потом черновик "
        "со ссылками.",
        TEAL,
    )
    story += [
        Diagram(150, ingest_pipeline),
        Spacer(1, 12),
        p("Как работает поиск", H2),
        p(
            "Текст запроса превращается в вектор той же моделью, "
            "что и статьи. Qdrant сравнивает "
            "его с векторами фрагментов (косинусное сходство: 1 - "
            "одинаковое направление векторов, около 0 - "
            "слабая связь векторов) и отбрасывает всё ниже порога. С "
            "кодом продукта ищутся статьи этого "
            "продукта и общие; агенту продукт подставляет сервер.",
            BODY_S,
        ),
        Spacer(1, 8),
        Diagram(178, score_chart),
        p(
            "Серые столбцы ниже порога в выдачу не попадают. "
            "Данные - настоящий поиск mock-"
            "эмбеддингов по 27 фрагментам без фильтра продукта.",
            SMALL,
        ),
        Spacer(1, 8),
        callout(
            "<b>Mock-эмбеддинг - лексический:</b> слова приводятся к основе из 5 букв "
            "(«ошибка», «ошибке» → «ошибк»), каждая основа через "
            "SHA-256 попадает в одну "
            "из 256 координат. Он находит тексты с общими "
            "словами, но не понимает синонимы. "
            "Адаптер OpenAI-эмбеддингов готов; после смены модели статьи нужно "
            "переиндексировать.",
            TEAL,
            TEAL_L,
        ),
    ]

    # 6. LangGraph
    section(
        story,
        "6",
        "LangGraph и решение человека",
        "Процесс как граф: после анализа он останавливается и ждёт сотрудника.",
        GREEN,
    )
    story += [
        Diagram(226, review_graph),
        Spacer(1, 10),
        grid_table(
            [
                [
                    p("Решение", HEAD),
                    p("Что передать", HEAD),
                    p("Что произойдёт", HEAD),
                ],
                [
                    p('<font color="#2563EB"><b>approve</b></font>', CELL),
                    p("токен reviewer/admin; можно final_content - новый текст", CELL),
                    p(
                        "Предложение approved, сообщение оператора в обращении, статус "
                        "waiting_customer, граф delivered",
                        CELL,
                    ),
                ],
                [
                    p('<font color="#C2410C"><b>reject</b></font>', CELL),
                    p("токен reviewer/admin и обязательный comment с причиной", CELL),
                    p(
                        "Предложение rejected, обращение без изменений, граф rejected",
                        CELL,
                    ),
                ],
            ],
            [70, 170, W - 240],
        ),
        Spacer(1, 10),
        cards(
            [
                (
                    "Проверка до продолжения",
                    "Без токена - 401, роль operator - 403, одобрение закрытого "
                    "обращения - 409, а граф продолжает ждать.",
                    GREEN,
                    GREEN_L,
                ),
                (
                    "Одно решение",
                    "Advisory lock и SELECT … FOR UPDATE: из двух одновременных "
                    "решений применяется одно, второе получает 409.",
                    BLUE,
                    BLUE_L,
                ),
                (
                    "Перезапуск не страшен",
                    "Состояние в PostgreSQL. CI пересоздаёт контейнеры "
                    "и продолжает отложенный граф.",
                    VIOLET,
                    VIOLET_L,
                ),
                (
                    "Без лишних данных",
                    "В checkpoint только id и итоги; ключ лицензии туда не попадает.",
                    TEAL,
                    TEAL_L,
                ),
                (
                    "Неудачный анализ",
                    "Запуск failed завершает граф: состояние failed, предложения нет.",
                    ORANGE,
                    ORANGE_L,
                ),
                (
                    "Автор из токена",
                    "Автора решения API берёт из токена сотрудника; поле "
                    "reviewer_id в теле запрещено (422).",
                    AMBER,
                    AMBER_L,
                ),
            ],
            columns=3,
        ),
    ]

    # 7. Данные
    section(
        story,
        "7",
        "Данные: таблицы и миграции",
        "Что хранится в PostgreSQL и как схема менялась от этапа к этапу.",
        HEX("#4338CA"),
    )
    story += [
        Diagram(268, er_diagram),
        Spacer(1, 6),
        p(
            "Стрелки - внешние ключи; пунктир - сообщение, "
            "созданное доставкой одобренного ответа. Таблица api_tokens "
            "(миграция 0005) хранит только SHA-256 токенов и ссылается на users.",
            SMALL,
        ),
        Spacer(1, 10),
        p("Миграции Alembic", H2),
        Diagram(96, migrations_timeline),
        Spacer(1, 6),
        callout(
            "Схему меняет <b>только Alembic</b>: сервис migrate "
            "применяет миграции до старта "
            'API, а <font name="Mono">/ready</font> отвечает 200, лишь когда версия БД '
            "совпадает с кодом. Старая миграция не переписывается - каждое изменение "
            "получает новую версию. Проверки отката выполняются только на одноразовой "
            "тестовой БД.",
            HEX("#4338CA"),
            HEX("#E0E7FF"),
        ),
    ]

    # 8. API
    section(
        story,
        "8",
        "Шпаргалка API",
        "28 путей, 35 операций; /api/* - только с токеном. Документация - /docs.",
        BLUE,
    )

    api_rows = [
        [p("Раздел", HEAD), p("Метод", HEAD), p("Путь", HEAD), p("Что делает", HEAD)]
    ]
    for area, verb, path, text in [
        ("Состояние", "GET", "/health · /ready", "API жив · БД и миграции готовы"),
        ("Ресурсы", "POST/GET", "/api/customers", "Создать или список клиентов"),
        ("", "POST/GET", "/api/products", "Продукты"),
        (
            "",
            "POST/GET",
            "/api/licenses",
            "Лицензии (фильтры клиента, продукта, ключа)",
        ),
        ("", "POST/GET", "/api/tickets", "Обращения (новые всегда open)"),
        ("", "POST/GET", "/api/users", "Сотрудники и их роли (создаёт admin)"),
        ("", "GET", "/api/{ресурс}/{id}", "Одна запись любого ресурса выше"),
        ("LLM", "GET", "/api/llm/status", "Какой провайдер настроен"),
        (
            "",
            "POST",
            "/api/tickets/{id}/draft",
            "Предварительный черновик без сохранения",
        ),
        (
            "База знаний",
            "GET",
            "/api/knowledge/status",
            "Индекс, точки, статьи реестра",
        ),
        ("", "POST", "/api/knowledge/search", "Поиск фрагментов, фильтр product_code"),
        (
            "Агент",
            "POST",
            "/api/tickets/{id}/analyze",
            "Анализ и граф проверки; 409 - закрыто или ждёт решения",
        ),
        (
            "",
            "GET",
            "/api/agent-runs[/{id}]",
            "История (фильтр ticket_id) или один запуск",
        ),
        (
            "Проверка",
            "GET",
            "/api/agent-runs/{id}/workflow",
            "Состояние графа LangGraph",
        ),
        ("", "GET", "/api/suggestions[/{id}]", "Очередь (status, ticket_id) или одно"),
        ("", "POST", "/api/suggestions/{id}/review", "Одобрить или отклонить"),
        ("Оператор", "GET", "/api/dashboard", "Агрегаты всей БД для обзора"),
        ("", "GET", "/api/tickets/{id}/messages", "Сообщения обращения"),
        ("", "PATCH", "/api/tickets/{id}", "Только смена статуса обращения"),
        ("Доступ", "GET", "/api/auth/me", "Сотрудник, роль и срок своего токена"),
        ("", "POST/GET", "/api/users/{id}/tokens", "Выдать токен или список (admin)"),
        ("", "DELETE", "/api/tokens/{id}", "Отозвать токен (admin)"),
    ]:
        verbs = p(
            " / ".join(
                f'<font name="Mono-Bold" color="'
                f'{"#0F766E" if v == "GET" else "#2563EB"}">{v}</font>'
                for v in verb.split("/")
            ),
            CELL_M,
        )
        api_rows.append(
            [p(f"<b>{area}</b>", CELL), verbs, p(path, CELL_M), p(text, CELL)]
        )
    story += [
        grid_table(api_rows, [72, 62, 168, W - 302]),
        Spacer(1, 10),
        p("Коды ответов", H2),
        grid_table(
            [
                [
                    p("Код", HEAD),
                    p("Значение", HEAD),
                    p("Код", HEAD),
                    p("Значение", HEAD),
                ],
                [
                    p("<b>200 / 201</b>", CELL),
                    p("успех / запись создана", CELL),
                    p("<b>401 / 403</b>", CELL),
                    p("нет действующего токена / роль ниже нужной", CELL),
                ],
                [
                    p("<b>404</b>", CELL),
                    p("запись не найдена", CELL),
                    p("<b>413 / 429</b>", CELL),
                    p("контекст велик / лимит изменений в минуту", CELL),
                ],
                [
                    p("<b>409</b>", CELL),
                    p("дубликат, решение принято, обращение закрыто", CELL),
                    p("<b>502</b>", CELL),
                    p("непригодный ответ модели или чужая ссылка", CELL),
                ],
                [
                    p("<b>422</b>", CELL),
                    p("неверный запрос: поля, типы, лишнее", CELL),
                    p("<b>503 / 504</b>", CELL),
                    p("БД, Qdrant, провайдер недоступны / тайм-аут", CELL),
                ],
            ],
            [54, W / 2 - 54, 54, W / 2 - 54],
        ),
    ]

    # 9. Запуск
    section(
        story,
        "9",
        "Как запустить у себя",
        "Python 3.12+, uv 0.12.23 и Docker. Linux, macOS и Windows - "
        "команды из корня проекта.",
        TEAL,
    )
    for number, title, command, note in [
        (
            "1",
            "Настройки и зависимости",
            "cp -n .env.example .env\nuv sync --locked",
            "cp -n не перезапишет существующий .env.",
        ),
        (
            "2",
            "Запустить стенд",
            "docker compose up --build --detach --wait --wait-timeout 180 ui",
            "Поднимет db и qdrant, применит миграции, запустит API и UI на :8502.",
        ),
        (
            "3",
            "Проиндексировать статьи",
            "docker compose exec -T api python -m app.knowledge.ingest",
            "Повторный запуск ничего не перепишет.",
        ),
        (
            "4",
            "Выдать токен администратора",
            'export API_TOKEN="$(docker compose exec -T api python -m \\\n'
            '  app.tokens issue --email admin@example.com --name Admin --role admin)"',
            "Показывается один раз; им же входят в UI и Swagger (Authorize).",
        ),
        (
            "5",
            "Проверить всё одной командой",
            "uv run --locked python scripts/smoke_test.py \\\n"
            "  --base-url http://127.0.0.1:8001 --timeout 60 --check-review",
            "Токен берёт из API_TOKEN; создаёт учебные записи - не на рабочей базе.",
        ),
    ]:
        story.append(
            Table(
                [
                    [
                        Diagram(
                            26,
                            lambda c, w, h, n=number: circle_num(
                                c, 13, 13, 12, n, TEAL
                            ),
                            width=26,
                        ),
                        [
                            p(f"<b>{title}</b>", BODY),
                            Spacer(1, 3),
                            code_block(command, W - 40),
                            Spacer(1, 2),
                            p(note, SMALL),
                        ],
                    ]
                ],
                colWidths=[34, W - 34],
                style=TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                ),
            )
        )
    story += [
        Spacer(1, 4),
        grid_table(
            [
                [p("Адрес", HEAD), p("Что там", HEAD)],
                [
                    p("http://127.0.0.1:8001/docs", CELL_M),
                    p("Swagger UI - все операции", CELL),
                ],
                [
                    p("http://127.0.0.1:8502", CELL_M),
                    p("Интерфейс оператора Streamlit", CELL),
                ],
                [
                    p("http://127.0.0.1:8001/ready", CELL_M),
                    p('{"status":"ok","database":"ok"} - БД и миграции готовы', CELL),
                ],
                [
                    p("127.0.0.1:5433 / 127.0.0.1:6335", CELL_M),
                    p("PostgreSQL / Qdrant для инструментов на Mac", CELL),
                ],
            ],
            [190, W - 190],
        ),
        Spacer(1, 8),
        callout(
            "<b>Windows:</b> PowerShell 7, Git Bash или WSL 2; аналоги команд - "
            '<font name="Mono">docs/instructions/windows.md</font>. Проверить '
            "запуск без Docker на любой ОС: "
            '<font name="Mono">uv run --locked python '
            "scripts/check_local_start.py</font>.",
            VIOLET,
            VIOLET_L,
        ),
        Spacer(1, 8),
        callout(
            'Остановить: <font name="Mono">docker compose '
            "down</font> - данные сохранятся. "
            "Чтобы подключить OpenAI, задай в .env <font "
            'name="Mono">LLM_PROVIDER=openai</font>, '
            "ключ и модель; это платно и отправляет данные провайдеру.",
            TEAL,
            TEAL_L,
        ),
    ]

    # 10. Проверки
    section(
        story,
        "10",
        "Проверки качества и CI/CD",
        "Каждое изменение проверяется автоматически, прежде "
        "чем API и UI попадут в реестр.",
        BLUE,
    )
    story += [
        Diagram(
            70,
            lambda c, w, h: stat_tiles(
                c,
                0,
                0,
                w,
                h,
                [
                    (
                        str(verification["tests"]["unit"]),
                        "unit-тестов: без сети, БД - заглушки и Qdrant в памяти",
                    ),
                    (
                        str(verification["tests"]["integration"]),
                        "интеграционных: настоящие PostgreSQL и Qdrant",
                    ),
                    (
                        str(verification["tests"]["smoke_cli"]),
                        "включены в unit: тесты smoke CLI; сам smoke - на живом стенде",
                    ),
                ],
                BLUE_L,
                BLUE,
                TEXT,
            ),
        ),
        Spacer(1, 12),
        p("Сколько тестов было после каждого этапа", H2),
        Diagram(150, tests_chart),
        Spacer(1, 10),
        p("Конвейер GitHub Actions", H2),
        Diagram(118, ci_pipeline),
        Spacer(1, 10),
        callout(
            "Тесты никогда не трогают рабочую БД: интеграционные требуют явный адрес "
            'базы с суффиксом <font name="Mono">_test</font>, Qdrant - коллекции '
            '<font name="Mono">test_*</font>. Unit-графы используют память; '
            "интеграционные review - PostgresSaver в отдельной тестовой БД. "
            "Итог удалённых запусков смотри в GitHub "
            "Actions: локальная проверка "
            "его не заменяет."
        ),
    ]

    # 11. Интерфейс оператора
    section(
        story,
        "11",
        "Интерфейс оператора",
        "Streamlit-панель поверх того же HTTP API: "
        "обращения, очередь проверки и поиск в браузере.",
        VIOLET,
    )
    story += [
        Diagram(166, ui_flow),
        Spacer(1, 10),
        grid_table(
            [
                [p("Раздел", HEAD), p("Что делает оператор", HEAD)],
                [
                    p("<b>Обзор</b>", CELL),
                    p(
                        "Агрегаты всей БД: обращения, открытые и срочные, "
                        "время анализа, доля одобрений",
                        CELL,
                    ),
                ],
                [
                    p("<b>Обращения</b>", CELL),
                    p(
                        "Создание с выбором клиента и продукта по названию; "
                        "фильтры, карточка, анализ, история и смена статуса",
                        CELL,
                    ),
                ],
                [
                    p("<b>Очередь проверки</b>", CELL),
                    p(
                        "Предложения pending / approved / rejected; правка текста, "
                        "одобрение или отказ с причиной",
                        CELL,
                    ),
                ],
                [
                    p("<b>База знаний</b>", CELL),
                    p("Поиск фрагментов по запросу и продукту, путь к статье", CELL),
                ],
                [
                    p("<b>Справочники</b>", CELL),
                    p(
                        "Клиенты, продукты, лицензии (ключи скрыты) и сотрудники",
                        CELL,
                    ),
                ],
            ],
            [120, W - 120],
        ),
        Spacer(1, 10),
        p(
            "Создание обращения: сначала выбери клиента (имя и email) и продукт "
            "(название и код). В каждом списке по 20 записей; поиск действует "
            "на текущей странице, остальные доступны кнопками Назад/Далее. "
            "Затем заполни тему, описание и приоритет и нажми Создать обращение. "
            "До выбора обеих записей кнопка недоступна. Смена списков обновляет "
            "страницу и может сбросить ещё не отправленный текст; сначала выбери "
            "обе записи. Пустой список пополняется в разделе Справочники.",
            BODY_S,
        ),
        Spacer(1, 8),
        p("Запуск", H2),
        code_block(
            "docker compose up --build --detach --wait --wait-timeout 180 ui\n"
            "docker compose exec -T api python -m app.knowledge.ingest\n"
            "# интерфейс: http://127.0.0.1:8502"
        ),
        Spacer(1, 9),
        callout(
            "<b>Изменения - только по явной форме.</b> Перерисовка страницы не "
            "повторяет анализ и решение; вызов реальной модели требует отдельного "
            "подтверждения; POST не повторяется автоматически. Вход - токеном "
            "сотрудника в боковой панели; форма решения - только reviewer и admin.",
            VIOLET,
            VIOLET_L,
        ),
    ]

    # 12. Оценки и демонстрация
    evaluations = verification["evaluations"]
    section(
        story,
        "12",
        "Оценки и демонстрация",
        "Числа вместо впечатлений и повторяемый показ без ключа модели.",
        TEAL,
    )
    story += [
        Diagram(
            70,
            lambda c, w, h: stat_tiles(
                c,
                0,
                0,
                w,
                h,
                [
                    (
                        f"{evaluations['recall_at_5']:.2f}",
                        "Recall@5: нужные статьи в первых пяти (порог 0.75)",
                    ),
                    (
                        f"{evaluations['mrr_at_5']:.2f}",
                        "MRR@5: первая верная статья наверху (порог 0.65)",
                    ),
                    (
                        f"{evaluations['no_hit_accuracy']:.2f}",
                        "молчание на вопросы без ответа (порог 0.80)",
                    ),
                    (
                        f"{evaluations['agent_passed']}/{evaluations['agent_total']}",
                        "сценариев безопасности агента (нужны все)",
                    ),
                ],
                TEAL_L,
                TEAL,
                TEXT,
            ),
        ),
        Spacer(1, 8),
        p(
            f"Набор: {evaluations['questions']} размеченных вопроса к семи статьям, "
            "из них шесть без ответа в базе. Qdrant строится в памяти тем же кодом, "
            "что и в API; сеть, ключи и PostgreSQL не нужны. Нарушение любого "
            "порога делает CI красным.",
            BODY_S,
        ),
        Spacer(1, 8),
        evidence_table(
            ["Что пытается сделать модель", "Что делает сервер"],
            [
                ("Сослаться на статью, которую не искала", "Запуск отклонён"),
                (
                    "По injection вызвать send_email или save_draft",
                    "unknown_tool: таких инструментов нет",
                ),
                (
                    "Прочитать чужое обращение или чужой продукт",
                    "out_of_scope / invalid_arguments",
                ),
                (
                    "Превысить лимит шагов или вызовов",
                    "Остановка до выполнения лишнего",
                ),
            ],
            [230, W - 230],
        ),
        Spacer(1, 9),
        p("Демонстрация за 5-7 минут", H2),
        code_block(
            "uv run --locked python scripts/demo_scenario.py \\\n"
            "  --url http://127.0.0.1:8001"
        ),
        Spacer(1, 6),
        Diagram(62, demo_flow),
        Spacer(1, 6),
        p(
            "Скрипт работает от имени сотрудника токена API_TOKEN, проверяет mock "
            "и индекс, создаёт или переиспользует вымышленные "
            "записи и оставляет одно предложение pending. Решение принимает человек "
            "в интерфейсе; повтор скрипта ничего не дублирует. Порядок показа - "
            "docs/DEMO.md.",
            BODY_S,
        ),
        Spacer(1, 8),
        callout(
            "Оценки проверяют учебный mock-поиск и серверные гарантии. Качество, "
            "стоимость и задержка реальной модели не измерялись: в отчёте они "
            "явно помечены как not measured.",
            AMBER,
            AMBER_L,
        ),
    ]

    # 13. Дорожная карта
    section(
        story,
        "13",
        "Дорожная карта",
        "Десять этапов учебного проекта: все готовы.",
        GREEN,
    )
    story += [
        Diagram(500, roadmap),
        Spacer(1, 10),
        callout(
            "Каждый этап - отдельная пошаговая инструкция на русском: "
            '<font name="Mono">docs/STAGE_01.md … STAGE_10.md</font>, '
            'сценарий показа - <font name="Mono">docs/DEMO.md</font>. В журнале '
            '<font name="Mono">docs/DEVELOPMENT_LOG.md</font> записано, что именно '
            "проверено и чем.",
            GREEN,
            GREEN_L,
        ),
    ]

    # 14. Словарь
    section(
        story,
        "14",
        "Словарь и куда читать дальше",
        "Термины, которые встречаются в проекте.",
        MUTED,
    )
    glossary = [
        (
            "API",
            "Набор адресов (маршрутов), через которые программы общаются с сервером.",
        ),
        ("FastAPI", "Фреймворк Python для HTTP API; сам строит документацию /docs."),
        ("Миграция", "Версия изменения схемы БД; Alembic применяет их по порядку."),
        ("Docker Compose", "Запуск нескольких связанных контейнеров одной командой."),
        ("LLM / mock", "Языковая модель / локальная заглушка без сети и платы."),
        (
            "Агент",
            "Программа, которая просит инструменты, получает факты и готовит итог.",
        ),
        (
            "Tool calling",
            "Протокол: модель просит вызвать функцию, сервер её выполняет.",
        ),
        ("RAG", "Ответ с опорой на найденные документы и ссылками на них."),
        (
            "Эмбеддинг",
            "Список чисел, описывающий текст; близкие тексты - близкие векторы.",
        ),
        ("Qdrant", "Векторная БД: быстро находит похожие векторы с фильтрами."),
        ("LangGraph", "Библиотека для процессов-графов с состоянием и остановками."),
        ("Checkpoint", "Сохранённое состояние графа после шага; здесь - в PostgreSQL."),
        (
            "Interrupt / resume",
            "Остановка графа до решения и продолжение с этим решением.",
        ),
        ("Human in the loop", "Шаг, где решение принимает человек, а не модель."),
        ("Smoke-тест", "Быстрая проверка, что запущенная система отвечает как надо."),
        ("CI/CD, GHCR", "Автопроверки на GitHub и публикация образов API/UI в реестр."),
        ("Streamlit", "Библиотека Python для веб-интерфейса из обычного скрипта."),
        (
            "Recall@5 / MRR",
            "Доля нужных статей в первых пяти / насколько высоко первая из них.",
        ),
    ]
    rows = [[p("Термин", HEAD), p("Простыми словами", HEAD)]]
    rows += [[p(f"<b>{term}</b>", CELL), p(text, CELL)] for term, text in glossary]
    story += [
        grid_table(rows, [112, W - 112]),
        Spacer(1, 12),
        p("Куда читать дальше", H2),
        grid_table(
            [
                [p("Файл", HEAD), p("Что там", HEAD)],
                [
                    p("README.md", CELL_M),
                    p("Краткое описание, дорожная карта, оглавление", CELL),
                ],
                [
                    p("docs/instructions/", CELL_M),
                    p(
                        "Быстрый запуск, Windows и PowerShell, работа с API, тесты, "
                        "CI, структура проекта",
                        CELL,
                    ),
                ],
                [
                    p("docs/STAGE_01-10.md", CELL_M),
                    p("Пошаговые учебные инструкции по этапам; DEMO.md - показ", CELL),
                ],
                [
                    p("docs/APPROACH.md", CELL_M),
                    p("Архитектура и решения (англ.)", CELL),
                ],
                [
                    p("docs/CI_CD.md", CELL_M),
                    p("Подробно о GitHub Actions и GHCR", CELL),
                ],
                [
                    p("docs/DEVELOPMENT_LOG.md", CELL_M),
                    p("Что сделано и как проверено на каждом этапе", CELL),
                ],
            ],
            [150, W - 150],
        ),
    ]

    append_audit_pages(story, verification)
    return story


def evidence_table(headers, rows, widths):
    """Преобразовать проверенные значения в таблицу с экранированным текстом."""
    prepared = [[p(escape(str(value)), HEAD) for value in headers]]
    prepared += [[p(escape(str(value)), CELL) for value in row] for row in rows]
    return grid_table(prepared, widths)


def append_audit_pages(story, verification):
    """Добавить десять практических разделов после исходного наглядного гайда."""
    tests = verification["tests"]
    section(
        story,
        "15",
        "Итоги полной проверки",
        "Результаты относятся к этой версии исходников; "
        "удалённый CI проверяется отдельно.",
        TEAL,
    )
    story += [
        p(
            f"Дата аудита: <b>{escape(verification['checked_at'])}</b>. "
            'Исходный коммит: <font name="Mono">'
            f"{escape(verification['base_commit'])}</font>. "
            "Проверяются приложение, миграции, поиск, процесс "
            "решения, образы API/UI и документация."
        ),
        Spacer(1, 9),
        evidence_table(
            ["Группа", "Результат"],
            [
                (
                    "Полный Pytest",
                    f"{tests['total']} passed; {tests['skipped']} skipped",
                ),
                ("Unit / интеграционные", f"{tests['unit']} / {tests['integration']}"),
                ("Smoke CLI (входит в unit)", tests["smoke_cli"]),
                ("Контракт HTTP", "28 URL-путей, 35 операций"),
            ],
            [210, W - 210],
        ),
        Spacer(1, 9),
        evidence_table(
            ["Проверка", "Фактическое свидетельство"],
            [(item["name"], item["result"]) for item in verification["checks"]],
            [145, W - 145],
        ),
        Spacer(1, 9),
        callout(
            escape(verification["remote_ci"]["detail"]),
            GREEN if verification["remote_ci"]["status"] == "passed" else AMBER,
            GREEN_L if verification["remote_ci"]["status"] == "passed" else AMBER_L,
        ),
        Spacer(1, 8),
        p("Как читать эту страницу", H2),
        p(
            "Тесты проверяют сценарии, а не качество настоящей модели. "
            "Публикацию подтверждает успешный Actions итогового SHA; "
            "локального прогона для этого недостаточно.",
            BODY_S,
        ),
    ]

    section(
        story,
        "16",
        "CI: матрица проверок",
        "Каждый push в main проходит последовательные барьеры "
        "перед публикацией API и UI для amd64 и arm64.",
        BLUE,
    )
    story += [
        evidence_table(
            ["Событие", "Проверки / образ", "GHCR"],
            [
                ("Локальный git commit", "Не запускаются на GitHub", "Нет"),
                ("Push в main", "Все обязательные задания", "После успеха"),
                (
                    "Push vX.Y.Z из main",
                    "Все шесть заданий; проверка истории",
                    "Версия API/UI",
                ),
                ("PR с целью main", "delivery → checks/security/ОС → container", "Нет"),
                (
                    "Run workflow вручную",
                    "delivery → checks/security/ОС → container",
                    "Нет",
                ),
            ],
            [150, 230, W - 380],
        ),
        Spacer(1, 10),
        evidence_table(
            ["Барьер", "Что проверяется", "Зачем"],
            [
                (
                    "delivery",
                    "Строгий vX.Y.Z; commit достижим из origin/main",
                    "Нельзя выпустить стороннюю ветку",
                ),
                (
                    "Зависимости",
                    "uv 0.12.23; lock; возраст ≥ 14 дн.; pip-audit",
                    "Нет слишком свежих и известных уязвимых выпусков",
                ),
                (
                    "checks: код и документы",
                    "Ruff, формат, документы, actionlint всех workflow",
                    "Ошибки кода, комментарии, ссылки",
                ),
                (
                    "PostgreSQL + Qdrant",
                    "Миграции, alembic check, Pytest; coverage ≥ 85%",
                    "Схема, поиск, строки и ветви кода",
                ),
                (
                    "Оценки",
                    "32 вопроса поиска, 12 сценариев агента",
                    "Пороги качества и безопасности",
                ),
                (
                    "Контейнер",
                    "Образы API и UI: readiness, UID, состав",
                    "Готовые образы без root и лишнего",
                ),
                (
                    "Полный сценарий",
                    "Ingest; агент; review; повтор решения",
                    "Путь от обращения до сообщения",
                ),
                (
                    "Пересоздание, копия",
                    "Тома, pending; backup → restore",
                    "Данные в томах и в копии",
                ),
                (
                    "UI и демо",
                    "Health UI; демо с обеими ветвями; повтор",
                    "Показ воспроизводится без ручных шагов",
                ),
                (
                    "Сканирование",
                    "Trivy 0.75.0 по digest; HIGH/CRITICAL с исправлением",
                    "Известные уязвимости ОС и Python в образах",
                ),
                (
                    "Журнал",
                    "JSON-строки запросов; нет ключей smoke и демо",
                    "Секреты не попадают в логи",
                ),
                (
                    "Переносимость",
                    "Linux, macOS, Windows: uv.lock, тесты, запуск API/UI",
                    "Проект работает на любой ОС разработчика",
                ),
                (
                    "arm64",
                    "Сборка, запуск и Trivy под QEMU",
                    "Родной образ на Apple Silicon и ARM",
                ),
                (
                    "Доставка",
                    "4 tested образа и SBOM; cosign по выбору",
                    "Проверенные сборки под общим тегом",
                ),
            ],
            [104, 208, W - 312],
        ),
        Spacer(1, 10),
        callout(
            "Ошибка обязательного задания останавливает цепочку. Следующие задания "
            "могут стать Skipped: сначала ищи первый неуспешный шаг. На PR пропуск "
            "publish ожидаем. Полный состав шагов определяет .github/workflows/ci.yml."
        ),
        Spacer(1, 8),
        p("Проверенные образы на весь путь", H2),
        p(
            "container собирает и проверяет API и UI для amd64 и arm64. В publish "
            "передаются tested-images.tar и JSON с SHA коммита и четырьмя image ID; "
            "после загрузки проверяются ID и платформы. Теги -amd64 и -arm64 "
            "получают те же образы, общий тег — index из их digest. "
            "Это защищает от ситуации, когда тестируется одна "
            "сборка, а доставляется другая.",
            BODY_S,
        ),
    ]

    section(
        story,
        "17",
        "Защита и доставка",
        "Технические ограничения проекта, права GitHub и "
        "воспроизводимые версии API и UI для двух архитектур.",
        VIOLET,
    )
    story += [
        evidence_table(
            ["Область", "Как устроено"],
            [
                (
                    "Права GitHub",
                    "CI: contents: read; packages: write и id-token: write "
                    "только в publish",
                ),
                (
                    "Внешние Actions",
                    "Версии закреплены полным SHA; actionlint 1.7.12 "
                    "игнорирует только новое queue: max",
                ),
                (
                    "Секреты",
                    "GITHUB_TOKEN выдаётся заданию; .env и ключи не входят в образ",
                ),
                ("Режим CI", "LLM и embeddings = mock; OpenAI credentials пустые"),
                (
                    "Процесс API",
                    "Постоянный пользователь UID 10001; dev-инструментов в runtime нет",
                ),
                (
                    "Агент",
                    "Строгие аргументы, серверная область данных, READ "
                    "ONLY инструменты",
                ),
                ("Тесты", "Отдельная *_test БД, временные test_* коллекции Qdrant"),
                (
                    "Образы",
                    "Сторонние образы по digest; Trivy без trivy-action "
                    "(CVE-2026-33634)",
                ),
                (
                    "Журнал API",
                    "JSON с X-Request-ID; без query string, SQL, тел запросов и ключей",
                ),
                (
                    "Публикация API/UI",
                    "main: sha-<полный SHA> для amd64 и arm64; "
                    "latest только для вершины",
                ),
                (
                    "Версия vX.Y.Z",
                    "Push тега из истории main; конфликт образа запрещён; "
                    "latest не меняется",
                ),
                (
                    "Подпись и SBOM",
                    "SBOM Trivy всегда проверяется. Публичная подпись выключена; "
                    "включение: GHCR_PUBLIC_SIGNING=true (Rekor)",
                ),
                (
                    "Очистка GHCR",
                    "SHA-версии старше 30 дней: план по воскресеньям, удаление "
                    "по явному разрешению; latest и vX.Y.Z не трогаются",
                ),
            ],
            [130, W - 130],
        ),
        Spacer(1, 11),
        p("Как получить ровно проверенную версию", H2),
        p(
            "Actions → CI and GHCR → нужный коммит → Publish tested image to GHCR. "
            "В Summary скопируй полный тег sha-… или digest index: Docker сам "
            "возьмёт arm64 или amd64 под свой компьютер. "
            "latest меняется со следующими "
            "версиями; digest обозначает содержимое конкретного образа.",
            BODY_S,
        ),
        Spacer(1, 6),
        code_block(
            'read -r -p "Полный SHA из Summary: " IMAGE_SHA\n'
            'export APP_IMAGE="ghcr.io/merkertg/ai-support-agent:sha-$IMAGE_SHA"\n'
            'export UI_IMAGE="ghcr.io/merkertg/ai-support-agent-ui:sha-$IMAGE_SHA"\n'
            'docker pull "$APP_IMAGE"\n'
            'docker pull "$UI_IMAGE"\n'
            "docker compose up --detach --no-build --wait ui"
        ),
        Spacer(1, 9),
        callout(
            "Для закрытого GHCR-пакета требуется вход аккаунтом с доступом. Локальный "
            "токен read:packages передавай через скрытый ввод/--password-stdin. "
            "Не добавляй его в .env.example, код или историю команд.",
            VIOLET,
            VIOLET_L,
        ),
        Spacer(1, 8),
        p("Граница реализованной защиты", H2),
        p(
            "Аудит зависимостей ищет известные уязвимости на дату "
            "проверки. Оценки не измеряют качество реальной модели. "
            "API закрыт токенами сотрудников (хранится только SHA-256) "
            "с ролями operator/reviewer/admin и лимитом изменений; входа "
            "через SSO нет. CD "
            "заканчивается в GHCR: внешняя "
            "площадка развёртывания пока не выбрана.",
            BODY_S,
        ),
    ]

    section(
        story,
        "18",
        "Полный локальный прогон",
        "Команды из корня проекта. Для сброса схемы "
        "используются отдельные тестовые сервисы.",
        TEAL,
    )
    story += [
        p("1. Подготовить окружение и одноразовые зависимости", H2),
        code_block(
            "cp -n .env.example .env\nuv sync --locked\n"
            "docker compose --profile test up --detach --wait \\\n  db-test qdrant-test"
        ),
        Spacer(1, 8),
        p("2. Проверить код и документацию", H2),
        code_block(
            "uv run --locked ruff check .\n"
            "uv run --locked ruff format --check .\n"
            "uv run --locked python scripts/check_dependency_age.py\n"
            "uv run --locked python scripts/check_project.py\n"
            "uv run --locked python -m evaluations.run"
        ),
        Spacer(1, 8),
        p("3. Выполнить весь Pytest без пропусков", H2),
        code_block(
            "export TEST_DATABASE_URL='postgresql+psycopg://support:'\\\n"
            "'support_ci@127.0.0.1:5434/ai_support_test'\n"
            "export TEST_QDRANT_URL=http://127.0.0.1:6336\n"
            "CI=true uv run --locked pytest --cov \\\n"
            "  --cov-report=term-missing:skip-covered \\\n"
            "  --cov-report=xml --cov-report=json --cov-report=html\n"
            "unset TEST_DATABASE_URL TEST_QDRANT_URL"
        ),
        Spacer(1, 8),
        p("4. Проверить запуск API и UI на этом компьютере", H2),
        code_block("uv run --locked python scripts/check_local_start.py"),
        Spacer(1, 8),
        p("5. Проверить собранный стенд через HTTP", H2),
        code_block(
            "docker compose up --build --detach --wait --wait-timeout 180 ui\n"
            "docker compose exec -T api python -m app.knowledge.ingest\n"
            "docker compose exec -T api python -m alembic check\n"
            'export API_TOKEN="$(docker compose exec -T api python -m app.tokens \\\n'
            '  issue --email admin@example.com --name Admin --role admin)"\n'
            "uv run --locked python scripts/smoke_test.py \\\n"
            "  --base-url http://127.0.0.1:8001 \\\n"
            "  --timeout 60 --check-review"
        ),
        Spacer(1, 9),
        callout(
            "Ожидается код завершения 0 у всех команд, ноль пропусков в полном Pytest, "
            "миграции без различий и успешные шаги smoke. --check-review нужен "
            "токен admin; он проверяет 401/403 и создаёт учебные записи: выполняй "
            "на учебном стенде. Без TEST URLs локальные "
            "интеграционные тесты пропускаются; CI=true "
            "превращает отсутствие адресов в ошибку.",
            TEAL,
            TEAL_L,
        ),
    ]

    section(
        story,
        "19",
        "Практика оператора",
        "Полный путь через Swagger: можно повторить без "
        "программирования и ключа модели.",
        ORANGE,
    )
    story += [
        p(
            "Открой http://127.0.0.1:8001/docs, нажми Authorize и вставь токен "
            "администратора (python -m app.tokens issue). В нужной операции нажми "
            "Try it out, заполни JSON и Execute. ID бери из ответов: они "
            "различаются на разных стендах.",
            BODY_S,
        ),
        Spacer(1, 8),
        evidence_table(
            ["Шаг и операция", "Что передать / что увидеть"],
            [
                (
                    "1. GET /api/llm/status",
                    "provider=mock, configured=true; затем GET "
                    "/api/knowledge/status: index=ready",
                ),
                (
                    "2. POST /api/customers",
                    "name: Учебный клиент; email: новый адрес "
                    "@example.com. Сохранить customer.id",
                ),
                (
                    "3. Продукт и лицензия",
                    "GET /api/products: взять techflow-server. Если его нет, "
                    "POST /api/products: name=TechFlow Server, code=techflow-server. "
                    "Затем POST /api/licenses: customer_id, product_id, "
                    "новый license_key",
                ),
                (
                    "4. POST /api/users",
                    "name, новый email и role=reviewer; затем POST "
                    "/api/users/{id}/tokens - токен рецензента (виден один раз)",
                ),
                (
                    "5. POST /api/tickets",
                    "customer_id, product_id, subject и description "
                    "проблемы. Начальный статус open",
                ),
                (
                    "6. POST …/{id}/analyze",
                    '{"license_key":"ключ шага 3"}. Запуск completed, '
                    "предложение pending, sources из найденных статей",
                ),
                (
                    "7. GET …/{run_id}/workflow",
                    "state=awaiting_review, next=[human_review]. Это "
                    "ожидание сотрудника",
                ),
                (
                    "8. POST /api/suggestions/{id}/review",
                    "Authorize токеном рецензента; decision=approve, можно "
                    "final_content. Автор решения - владелец токена",
                ),
                (
                    "9. Проверить результат",
                    "suggestion approved, message operator, ticket_status "
                    "waiting_customer, workflow delivered",
                ),
                (
                    "10. Повторить решение",
                    "HTTP 409: предложение уже рассмотрено, второго сообщения нет",
                ),
            ],
            [174, W - 174],
        ),
        Spacer(1, 10),
        p("Отдельно пройти отклонение", H2),
        p(
            "Создай второе обращение и запусти анализ. Для pending предложения передай "
            "decision=reject и обязательный comment "
            "с причиной (токен reviewer или admin); final_content "
            "не передавай. Ожидаются rejected, message=null, обращение остаётся open. "
            "Пошаговые curl-блоки с сохранением ID находятся в "
            "docs/STAGE_07.md, разделы 9-10.",
            BODY_S,
        ),
        Spacer(1, 9),
        callout(
            "Перед одобрением прочитай источники и исправь ответ. Mock показывает путь "
            "данных, а не качество технической рекомендации. «Доставка» здесь означает "
            "сообщение в PostgreSQL, не письмо и не ответ во внешней системе.",
            ORANGE,
            ORANGE_L,
        ),
    ]

    section(
        story,
        "20",
        "Перезапуск, данные и копии",
        "Тома при down/up, резервная копия с восстановлением и ожидающее решение.",
        GREEN,
    )
    story += [
        evidence_table(
            ["Состояние", "Где хранится", "После обычного down / up"],
            [
                ("Предметные записи и run", "Том postgres_data", "Сохраняются"),
                ("Решение и сообщение", "PostgreSQL", "Сохраняются"),
                (
                    "Ожидание human_review",
                    "checkpoint_* в PostgreSQL",
                    "Можно продолжить",
                ),
                ("Векторы статей", "Том qdrant_data", "Сохраняются"),
                (
                    "Учебные db-test/qdrant-test",
                    "tmpfs / временные данные",
                    "Одноразовые",
                ),
            ],
            [160, 155, W - 315],
        ),
        Spacer(1, 11),
        p("Сравнить предметные данные до и после", H2),
        code_block(
            "docker compose exec -T api python -m app.db.demo seed "
            "> /tmp/ai-support-before.json\n"
            "docker compose down\n"
            "docker compose up --detach --no-build --wait ui\n"
            "docker compose exec -T api python -m app.db.demo show "
            "> /tmp/ai-support-after.json\n"
            "cmp /tmp/ai-support-before.json /tmp/ai-support-after.json"
        ),
        Spacer(1, 7),
        p(
            "cmp с кодом 0 без вывода подтверждает совпадение "
            "снимков. После up не выполняй "
            "повторный seed до сравнения: он мог бы заново создать потерянные записи.",
            BODY_S,
        ),
        Spacer(1, 9),
        p("Обновить стенд до новой версии", H2),
        code_block("docker compose up --build --detach --wait --wait-timeout 180 ui"),
        Spacer(1, 5),
        p(
            "Без down: Compose пересобирает образы, применяет миграции и пересоздаёт "
            "только изменившиеся контейнеры; тома остаются. Так обновлён "
            "development-стенд: снимок 14 таблиц до и после совпал.",
            BODY_S,
        ),
        Spacer(1, 9),
        p("Резервная копия и восстановление", H2),
        code_block(
            "uv run --locked python scripts/backup.py create "
            '--output-dir "$HOME/ai-support-backups"\n'
            "docker compose up --detach --wait db qdrant   # чистые тома\n"
            'uv run --locked python scripts/backup.py restore "$backup"'
        ),
        Spacer(1, 5),
        p(
            "pg_dump в контейнере db, snapshot Qdrant и manifest.json (Alembic, "
            "SHA256, строки). restore сверяет копию и версию Alembic, требует "
            "остановленный api и пустую цель (иначе --force) и заменяет рабочую БД "
            "последней. Копия секретна: храни вне Git и репозитория "
            "(docs/instructions/backup.md).",
            BODY_S,
        ),
        Spacer(1, 9),
        p("Проверить ожидающий процесс", H2),
        p(
            "Создай анализ и сохрани run_id, оставив предложение "
            "pending. Пересоздай контейнеры "
            "обычным down/up. GET /api/agent-runs/{id}/workflow должен снова показать "
            "awaiting_review. Прими решение через review: граф "
            "продолжится из checkpoint. "
            "CI повторяет этот сценарий автоматически и "
            "дополнительно сравнивает индекс поиска. "
            "При сбое saver чтение workflow восстанавливает "
            "checkpoint из AgentRun/AgentSuggestion "
            "под advisory lock. Анализ и доставка при восстановлении не повторяются.",
            BODY_S,
        ),
        Spacer(1, 10),
        callout(
            "Обычный down сохраняет тома; --volumes удаляет данные и применяется "
            "CI только к своему проекту, где копия восстанавливается в чистые тома. "
            "Брошенные running завершаются автоматически: порог 180 с, таймер 30 с "
            "(раздел 22).",
            GREEN,
            GREEN_L,
        ),
    ]

    section(
        story,
        "21",
        "Диагностика ошибок",
        "Начинай с первого неуспешного шага; повторная "
        "публикация не исправляет ошибку тестов.",
        AMBER,
    )
    story += [
        evidence_table(
            ["Симптом", "Что проверить", "Следующее действие"],
            [
                (
                    "Нет docker.sock, dependency failed",
                    "Docker Desktop и docker version",
                    "Запустить Docker и повторить up; тома целы",
                ),
                (
                    "UI: нет соединения с API",
                    "compose ps; API_URL=http://api:8000",
                    "Дождаться healthy api, обновить страницу",
                ),
                (
                    "UI: код запроса",
                    "docker compose logs api, поиск кода",
                    "Класс ошибки, SQLSTATE, ответ провайдера",
                ),
                (
                    "Порт занят",
                    "compose ps; API_PORT, POSTGRES_PORT",
                    "Выбрать свободный порт и согласовать URL",
                ),
                (
                    "/health 200, /ready 503",
                    "Логи db/migrate; Alembic current",
                    "Исправить БД или применить миграцию",
                ),
                (
                    "Индекс missing",
                    "GET /api/knowledge/status",
                    "Выполнить явный ingest",
                ),
                (
                    "Индекс incompatible",
                    "Модель и размерность embeddings",
                    "Согласовать настройки, переиндексировать",
                ),
                (
                    "HTTP 422",
                    "JSON, лишние поля, ID, поля решения",
                    "Сверить схему Swagger; reject требует comment",
                ),
                (
                    "HTTP 401 / 403 / 429",
                    "Bearer-токен, срок и отзыв, роль, лимит в минуту",
                    "Новый токен (app.tokens issue), роль или пауза",
                ),
                (
                    "HTTP 409",
                    "Предложение, статус обращения, запуски",
                    "Принять решение, отклонить или сменить статус",
                ),
                (
                    "HTTP 502 / 504",
                    "run_id, safe error, лимиты",
                    "Изучить failed run, исправить причину",
                ),
                (
                    "Windows: кракозябры, cp1252",
                    "Кодировка консоли PowerShell",
                    "[Console]::OutputEncoding = UTF8; PowerShell 7",
                ),
                (
                    "Windows: $'\\r' в bash",
                    "CRLF в старой копии репозитория",
                    "git rm --cached -r . и git reset --hard",
                ),
                (
                    "Тесты skipped локально",
                    "TEST_DATABASE_URL, TEST_QDRANT_URL",
                    "Поднять test-сервисы, повторить полностью",
                ),
                (
                    "GitHub: container Skipped",
                    "Результат предыдущего задания",
                    "Исправить первый сбой checks/security",
                ),
                (
                    "GHCR denied",
                    "Права пакета, владелец, GITHUB_TOKEN",
                    "Проверить Packages/Actions permissions",
                ),
            ],
            [116, 185, W - 301],
        ),
        Spacer(1, 10),
        p("Команды для сбора понятной диагностики", H2),
        code_block(
            "docker compose ps --all\n"
            "docker compose logs --no-color --tail 100 \\\n"
            "  api ui migrate db qdrant\n"
            'docker compose logs --no-color api | grep \'"level": "ERROR"\'\n'
            "docker compose exec -T api python -m alembic current"
        ),
        Spacer(1, 9),
        callout(
            "В GitHub открой CI and GHCR → нужный коммит → первый красный шаг. "
            "JUnit лежит в pytest-report; контейнерные логи показывают зависимости. "
            "После исправления повтори весь workflow, если архив проверенного образа "
            "уже удалён. Новый Run workflow выполняет проверки без publish.",
            AMBER,
            AMBER_L,
        ),
    ]

    section(
        story,
        "22",
        "Покрытие и восстановление",
        "Как повторить порог качества и что произойдёт после остановки worker.",
        TEAL,
    )
    story += [
        p("1. Выполнить полный прогон с покрытием", H2),
        p(
            "Сначала подними db-test/qdrant-test и задай TEST URLs из раздела 18. "
            "Один полный запуск проверяет приложение, UI, оценки и CLI:",
            BODY_S,
        ),
        code_block(
            "CI=true uv run --locked pytest --cov \\\n"
            "  --cov-report=term-missing:skip-covered \\\n"
            "  --cov-report=xml --cov-report=json --cov-report=html"
        ),
        Spacer(1, 7),
        p(
            "Порог <b>85%</b> задаётся в pyproject.toml: это общий показатель строк "
            "и ветвей, а не отдельный порог каждого файла. patch=subprocess "
            "объединяет дочерние Python-процессы smoke с Pytest. Макет PDF исключён "
            "из числового покрытия: он проверяется рендером. Недобор даёт код 1 "
            "и блокирует container/publish даже при зелёных assertions.",
            BODY_S,
        ),
        Spacer(1, 7),
        evidence_table(
            ["Отчёт", "Где смотреть"],
            [
                ("coverage.xml / coverage.json", "reports/: машинные результаты"),
                ("coverage-html/index.html", "reports/: строки и ветви по файлам"),
                ("Артефакт coverage-report", "GitHub Actions: XML, JSON, HTML; 7 дней"),
            ],
            [180, W - 180],
        ),
        Spacer(1, 10),
        p("2. Проверить восстановление брошенного запуска", H2),
        p(
            "Сервер сохраняет running до вызова агента. После сбоя процесса "
            "монитор каждые <b>30 секунд</b> переводит только running возрастом "
            "<b>не менее 180 секунд</b> в failed. Время берётся из PostgreSQL: "
            "120 секунд максимального лимита плюс запас 60. Перед новым анализом "
            "того же обращения действует та же проверка.",
            BODY_S,
        ),
        Spacer(1, 6),
        code_block(
            "docker compose logs --since 10m api\n"
            'read -r -p "ID проверяемого запуска: " RUN_ID\n'
            "curl --fail-with-body \\\n"
            '  -H "Authorization: Bearer $API_TOKEN" \\\n'
            '  "http://127.0.0.1:8001/api/agent-runs/$RUN_ID"'
        ),
        Spacer(1, 7),
        p(
            "Ищи событие agent_run_recovered и статус failed с finished_at. "
            "Recovery не создаёт предложение и не отправляет ответ; сохранённый "
            "прогресс остаётся. Поздний worker получает 409 и не оживляет failed. "
            "Успевший completed и его pending сохраняются. Ошибка БД откатывает "
            "проход; таймер повторит попытку. Проверки гонок выполняются только "
            "в тестовой БД, рабочий процесс для демонстрации не останавливай.",
            BODY_S,
        ),
        Spacer(1, 7),
        callout(
            "RUN_RECOVERY_ENABLED=false отключает таймер, но сохраняет проверку "
            "перед анализом. SQL ограничен statement_timeout=3s и lock_timeout=1s; "
            "занятая БД может задержать проход. Инструкция: docs/RUN_RECOVERY.md.",
            TEAL,
            TEAL_L,
        ),
    ]

    section(
        story,
        "23",
        "Обновления и версии релизов",
        "Как получить проверяемую PR зависимостей и опубликовать выбранную версию.",
        VIOLET,
    )
    story += [
        p("1. Еженедельная PR зависимостей", H2),
        p(
            "Update Python dependencies запускается по понедельникам в "
            "<b>06:17 UTC / 11:17 Ташкент</b> или вручную только из main. "
            "prepare без write-токена сдвигает exclude-newer до полуночи UTC "
            "за 14 суток и выполняет uv lock --upgrade --no-build. Age check "
            "повторно проверяет upload-time всех пакетов.",
            BODY_S,
        ),
        Spacer(1, 6),
        evidence_table(
            ["Шаг", "Ожидаемый результат"],
            [
                ("prepare → publish", "Только pyproject.toml и uv.lock; код из main"),
                (
                    "Bot-ветка",
                    "automation/python-dependencies: одна PR, без автослияния",
                ),
                (
                    "Отдельный dispatch CI",
                    "Обычные проверки этой ветки; GHCR не публикуется",
                ),
                (
                    "После review и зелёного CI",
                    "Человек сливает PR; main запускает полный CI/CD",
                ),
            ],
            [150, W - 150],
        ),
        Spacer(1, 7),
        p(
            "uv 0.12.23 опубликован 03.10.2026; совместимость актуального lock "
            "проверена. Оба builder используют его закреплённый digest. Точные "
            "ограничения вроде streamlit==1.65.0 сохраняются. Docker digest, "
            "Actions SHA и диапазоны пакетов пересматриваются отдельно. "
            "HTTP 403 при создании PR требует разрешения в настройках Actions; "
            "PAT для обхода скрипт не подставляет. Политика и разбор ошибок: "
            "docs/DEPENDENCY_UPDATES.md.",
            BODY_S,
        ),
        Spacer(1, 10),
        p("2. Выбрать стабильную версию vX.Y.Z", H2),
        p(
            "Версию выбирает человек после просмотра успешного CI нужного SHA. "
            "Тег должен указывать на commit из истории origin/main; development- "
            "и PR-ветки не выпускаются. Разрешён строгий vMAJOR.MINOR.PATCH, "
            "без суффиксов и ведущих нулей. Команды ниже выполняют явный релиз:",
            BODY_S,
        ),
        code_block(
            "set -e\n"
            "git fetch origin main --tags\n"
            'read -r -p "Проверенный полный SHA из main: " RELEASE_SHA\n'
            'read -r -p "Выбранный тег vX.Y.Z: " RELEASE_VERSION\n'
            'git merge-base --is-ancestor "$RELEASE_SHA" origin/main\n'
            'git tag -a "$RELEASE_VERSION" "$RELEASE_SHA" -m "$RELEASE_VERSION"\n'
            'git push origin "refs/tags/$RELEASE_VERSION"'
        ),
        Spacer(1, 7),
        p(
            "Push тега запускает шесть jobs: delivery → checks/security/"
            "portability → container → publish. API и UI для amd64 и arm64 "
            "получают vX.Y.Z-amd64, vX.Y.Z-arm64 и общий vX.Y.Z; повторной "
            "сборки в publish нет. Существующий "
            "version-тег с другим image ID останавливает доставку. Повтор "
            "идентичного образа разрешён. Релиз не двигает latest; main публикует "
            "sha-&lt;полный SHA&gt; и latest только для своей текущей вершины. "
            "Публичная подпись выключена по выбору пользователя. "
            "GHCR_PUBLIC_SIGNING=true включает cosign keyless и SBOM attestations; "
            "metadata появятся в Rekor. Проверка: docs/RELEASES.md, раздел 5.",
            BODY_S,
        ),
        Spacer(1, 7),
        p("3. Очистка старых SHA-образов", H2),
        p(
            "Clean up old GHCR SHA images по воскресеньям строит план: SHA-версии "
            "main старше 30 дней вместе с их подписями. latest, vX.Y.Z и всё, на "
            "что они ссылаются, остаются. По умолчанию dry-run; удаление — "
            "отметка delete при ручном запуске или GHCR_CLEANUP_DELETE=true.",
            BODY_S,
        ),
        Spacer(1, 7),
        callout(
            "CI настраивает доставку, но локальная проверка не доказывает результат "
            "GitHub. Подтверди шесть jobs итогового коммита, оба пакета GHCR и "
            "статус выключенной подписи в Actions Summary. Тег и GitHub Release "
            "автоматически не создаются.",
            VIOLET,
            VIOLET_L,
        ),
    ]

    section(
        story,
        "24",
        "Что делать дальше",
        "Десять этапов готовы; дальше - реальная модель, доступ и эксплуатация.",
        BLUE,
    )
    story += [
        p("Что изменено при этом аудите", H2),
        *[p("• " + escape(item), BODY_S) for item in verification["changes"]],
        Spacer(1, 10),
        evidence_table(
            ["Следующая работа", "Критерий готовности"],
            [
                (
                    "Реальный OpenAI",
                    "Пользователь задаёт ключ и доступную модель "
                    "локально; тот же набор оценок с отчётом о стоимости",
                ),
                (
                    "Вход через SSO",
                    "OIDC поверх готовых ролей и токенов; общий лимит "
                    "изменений в шлюзе или Redis; TLS на внешнем адресе",
                ),
                (
                    "Настоящая доставка",
                    "Email или helpdesk; учёт повторов, журнал попыток, "
                    "тест отказа внешнего сервиса",
                ),
                (
                    "Подтвердить удалённый CI",
                    "Шесть успешных jobs итогового SHA, оба пакета GHCR, SBOM "
                    "и первый dry-run очистки",
                ),
                (
                    "Внешний сервер",
                    "Выбрана площадка и секреты; расписание backup.py и "
                    "шифрованное хранилище копий; проверка после deploy",
                ),
            ],
            [150, W - 150],
        ),
        Spacer(1, 10),
        p("Остающиеся ограничения", H2),
        *[p("• " + escape(item), BODY_S) for item in verification["remaining"]],
        Spacer(1, 10),
        callout(
            "OpenAI-адаптеры уже есть, но реальный запрос и "
            "качество ответов не проверены. "
            "По текущему выбору пользователя сохраняется mock: ключ не нужен и платных "
            "вызовов нет. Подключение настоящей модели меняет "
            "стоимость и передачу данных; "
            "оно выполняется отдельным явным действием.",
            BLUE,
            BLUE_L,
        ),
        Spacer(1, 10),
        p("Документация и проверяемая история", H2),
        p(
            "<link "
            'href="https://github.com/merkertg/ai-support-agent">'
            "Репозиторий проекта</link>"
            " · <link "
            'href="https://github.com/merkertg/ai-support-agent/actions">'
            "GitHub Actions</link>"
            " · <link "
            'href="https://github.com/merkertg/ai-support-agent/tree/main/docs">'
            "Документация</link>",
            BODY_S,
        ),
        p(
            "Исходник этого PDF: scripts/build_guide.py. "
            "Проверенные числа и результаты: "
            "docs/guide/verification.json. Инструкция пересборки: "
            "docs/guide/README.md. "
            "Не меняй числа вручную в макете: обновляй "
            "свидетельства после полного прогона.",
            SMALL,
        ),
    ]


def load_verification(path):
    """Прочитать свидетельства аудита и отклонить отсутствующие или неверные числа."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "checked_at",
        "base_commit",
        "tests",
        "checks",
        "remote_ci",
        "changes",
        "remaining",
        "evaluations",
    ):
        if key not in data:
            raise ValueError(f"В verification.json отсутствует {key}")
    tests = data["tests"]
    for key in ("total", "unit", "integration", "smoke_cli", "skipped"):
        if type(tests.get(key)) is not int or tests[key] < 0:
            raise ValueError(f"tests.{key}: требуется неотрицательное целое число")
    if tests["total"] != tests["unit"] + tests["integration"]:
        raise ValueError("Число всех тестов не равно сумме unit и integration")
    if tests["smoke_cli"] > tests["unit"]:
        raise ValueError("Smoke CLI является подмножеством unit-тестов")
    if not isinstance(data["checks"], list) or not data["checks"]:
        raise ValueError("checks должен содержать фактические результаты проверок")
    for item in data["checks"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key] for key in ("name", "result")
        ):
            raise ValueError("Для каждой проверки нужны строки name и result")
    for key in ("changes", "remaining"):
        if not isinstance(data[key], list) or not all(
            isinstance(item, str) for item in data[key]
        ):
            raise ValueError(f"{key}: требуется список строк")
    evaluations = data["evaluations"]
    for key in ("recall_at_5", "mrr_at_5", "no_hit_accuracy"):
        value = evaluations.get(key)
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise ValueError(f"evaluations.{key}: требуется число от 0 до 1")
    for key in ("questions", "agent_passed", "agent_total"):
        if type(evaluations.get(key)) is not int or evaluations[key] < 0:
            raise ValueError(f"evaluations.{key}: требуется неотрицательное целое")
    if evaluations["agent_passed"] > evaluations["agent_total"]:
        raise ValueError("Пройденных сценариев больше, чем всего сценариев")
    remote = data["remote_ci"]
    if remote.get("status") not in {
        "not_verified",
        "passed",
        "failed",
        "running",
    } or not isinstance(remote.get("detail"), str):
        raise ValueError("remote_ci: нужны допустимый status и пояснение detail")
    if not all(
        isinstance(data[key], str) and data[key]
        for key in ("checked_at", "base_commit")
    ):
        raise ValueError("checked_at и base_commit должны быть непустыми строками")
    return data


def build_guide(output, verification):
    """Сохранить backup и атомарно заменить PDF только после успешной сборки."""
    register_fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = Path(tempfile.gettempdir()) / (
            f"ai-support-agent-guide-before-{stamp}.pdf"
        )
        shutil.copy2(output, backup)
        print(f"Исходный PDF сохранён: {backup}")
    with tempfile.NamedTemporaryFile(
        prefix=".ai-support-guide-",
        suffix=".pdf",
        dir=output.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        document = BaseDocTemplate(
            str(temporary_path),
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=16 * mm,
            bottomMargin=17 * mm,
            title="AI Support Agent - наглядный гайд и полная проверка",
            author="AI Support Agent",
            subject="Архитектура, практика, CI/CD и результаты аудита",
        )
        document.verification = verification
        frame = Frame(
            MARGIN,
            17 * mm,
            W,
            PAGE_H - 33 * mm,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        cover_frame = Frame(0, 0, PAGE_W, PAGE_H, id="cover")
        document.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[cover_frame], onPage=cover_page),
                PageTemplate(id="content", frames=[frame], onPage=content_page),
            ]
        )
        document.build(build_story(verification))
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Руководство создано: {output}")


def main(argv=None):
    """Разобрать CLI и собрать PDF только при явном запуске скрипта."""
    parser = argparse.ArgumentParser(
        description="Собрать русский PDF-гайд AI Support Agent."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "docs/guide/ai-support-agent-guide.pdf",
    )
    parser.add_argument(
        "--verification",
        type=Path,
        default=PROJECT_ROOT / "docs/guide/verification.json",
    )
    args = parser.parse_args(argv)
    build_guide(args.output.resolve(), load_verification(args.verification.resolve()))


if __name__ == "__main__":
    main()
