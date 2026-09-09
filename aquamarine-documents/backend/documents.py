"""Сборка файлов документов: PDF (колонтитулы, нумерация, QR) и DOCX.

На вход — готовый текст от backend.render, на выход — байты файла.
Строки целиком заглавными буквами считаются заголовками и центрируются.
"""

import io
import os

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas

from .security import AppError

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_LEFT = 20 * mm
MARGIN_RIGHT = 15 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 20 * mm
BODY_SIZE = 10.5
TITLE_SIZE = 12
LEADING = 14
QR_SIZE = 20 * mm
BRAND = (0.18, 0.769, 0.714)  # #2EC4B6
INK = (0.09, 0.22, 0.22)
GRAY = (0.42, 0.47, 0.47)

FONT_REGULAR = "AquaSerif"
FONT_BOLD = "AquaSerif-Bold"
FONT_CANDIDATES = (
    ("/usr/share/fonts/liberation-serif/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/liberation-serif/LiberationSerif-Bold.ttf"),
    ("/usr/share/fonts/msttcore/times.ttf", "/usr/share/fonts/msttcore/timesbd.ttf"),
    ("/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
     "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),
)

_fonts_ready = False


def font_paths():
    """Пара путей (обычный, жирный) к шрифту с кириллицей."""
    manual = (os.environ.get("PDF_FONT_REGULAR"), os.environ.get("PDF_FONT_BOLD"))
    candidates = ((manual,) if all(manual) else ()) + FONT_CANDIDATES
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            return regular, bold
    return None


def register_fonts():
    """Регистрирует шрифты один раз на процесс."""
    global _fonts_ready
    if _fonts_ready:
        return
    found = font_paths()
    if not found:
        raise AppError(
            "На сервере нет шрифта с поддержкой кириллицы — PDF не сформировать",
            status=500,
            details={"hint": "укажите PDF_FONT_REGULAR и PDF_FONT_BOLD или установите liberation-serif"},
        )
    regular, bold = found
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, regular))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
    pdfmetrics.registerFontFamily(FONT_REGULAR, normal=FONT_REGULAR, bold=FONT_BOLD)
    _fonts_ready = True


def _is_title(line):
    letters = [char for char in line if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters) and len(line) <= 120


def _wrap(text, font, size, width):
    """Перенос по словам; слишком длинное слово режется по символам."""
    lines = []
    current = ""
    for word in text.split(" "):
        candidate = (current + " " + word).strip()
        if not current or pdfmetrics.stringWidth(candidate, font, size) <= width:
            current = candidate
            continue
        lines.append(current)
        current = word
        while pdfmetrics.stringWidth(current, font, size) > width and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and pdfmetrics.stringWidth(current[:cut], font, size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    lines.append(current)
    return lines or [""]


def _layout(text, usable_width, usable_height, reserve_last=0.0):
    """Разбивает текст на страницы со стилями строк."""
    items = []
    for raw in str(text).replace("\r\n", "\n").split("\n"):
        stripped = raw.strip()
        if not stripped:
            items.append({"text": "", "font": FONT_REGULAR, "size": BODY_SIZE, "align": "left", "indent": 0.0})
            continue
        title = _is_title(stripped)
        font = FONT_BOLD if title else FONT_REGULAR
        size = TITLE_SIZE if title else BODY_SIZE
        indent = min((len(raw) - len(raw.lstrip())) * 2.0, 40.0)
        for piece in _wrap(stripped, font, size, usable_width - indent):
            items.append({"text": piece, "font": font, "size": size,
                          "align": "center" if title else "left", "indent": indent})
    pages = []
    page = []
    used = 0.0
    for item in items:
        step = LEADING if item["size"] <= BODY_SIZE else LEADING + 4
        if page and used + step > usable_height:
            pages.append((page, used))
            page = []
            used = 0.0
        page.append(item)
        used += step
    pages.append((page, used))
    if reserve_last and pages[-1][1] + reserve_last > usable_height:
        pages.append(([], 0.0))
    return pages


def _draw_frame(sheet, meta, page_number, total):
    top = PAGE_HEIGHT - MARGIN_TOP + 8 * mm
    bottom = MARGIN_BOTTOM - 9 * mm
    sheet.setFont(FONT_REGULAR, 8)
    sheet.setFillColorRGB(*GRAY)
    sheet.drawString(MARGIN_LEFT, top, str(meta.get("agency") or "")[:70])
    sheet.drawRightString(PAGE_WIDTH - MARGIN_RIGHT, top, str(meta.get("deal_number") or "")[:40])
    sheet.setStrokeColorRGB(*BRAND)
    sheet.setLineWidth(0.8)
    sheet.line(MARGIN_LEFT, top - 2.5 * mm, PAGE_WIDTH - MARGIN_RIGHT, top - 2.5 * mm)
    sheet.line(MARGIN_LEFT, bottom + 4 * mm, PAGE_WIDTH - MARGIN_RIGHT, bottom + 4 * mm)
    footer = " · ".join(part for part in (meta.get("document_code"), meta.get("generated_at")) if part)
    sheet.drawString(MARGIN_LEFT, bottom, footer[:110])
    sheet.drawRightString(PAGE_WIDTH - MARGIN_RIGHT, bottom, f"стр. {page_number} из {total}")


def _draw_qr(sheet, url, caption):
    widget = qr.QrCodeWidget(str(url))
    left, low, right, high = widget.getBounds()
    width = (right - left) or 1
    height = (high - low) or 1
    drawing = Drawing(QR_SIZE, QR_SIZE, transform=[QR_SIZE / width, 0, 0, QR_SIZE / height, 0, 0])
    drawing.add(widget)
    x = PAGE_WIDTH - MARGIN_RIGHT - QR_SIZE
    y = MARGIN_BOTTOM
    renderPDF.draw(drawing, sheet, x, y)
    sheet.setFont(FONT_REGULAR, 7)
    sheet.setFillColorRGB(*GRAY)
    for offset, line in enumerate(_wrap(caption, FONT_REGULAR, 7, 60 * mm)[:2]):
        sheet.drawRightString(x - 3 * mm, y + QR_SIZE - 6 - offset * 9, line)


def to_pdf(text, meta=None):
    """Текст -> PDF с колонтитулами, нумерацией и QR-кодом проверки."""
    register_fonts()
    meta = dict(meta or {})
    usable_width = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    usable_height = PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    verify_url = meta.get("verify_url")
    pages = _layout(text, usable_width, usable_height, QR_SIZE + 6 * mm if verify_url else 0.0)
    buffer = io.BytesIO()
    sheet = pdf_canvas.Canvas(buffer, pagesize=A4)
    sheet.setTitle(str(meta.get("title") or "Документ"))
    sheet.setAuthor(str(meta.get("agency") or ""))
    total = len(pages)
    for number, (lines, _used) in enumerate(pages, start=1):
        _draw_frame(sheet, meta, number, total)
        position = PAGE_HEIGHT - MARGIN_TOP
        for item in lines:
            step = LEADING if item["size"] <= BODY_SIZE else LEADING + 4
            position -= step
            sheet.setFont(item["font"], item["size"])
            sheet.setFillColorRGB(*INK)
            if item["align"] == "center":
                sheet.drawCentredString(PAGE_WIDTH / 2, position, item["text"])
            else:
                sheet.drawString(MARGIN_LEFT + item["indent"], position, item["text"])
        if verify_url and number == total:
            _draw_qr(sheet, verify_url, str(meta.get("qr_caption") or "Проверка документа в Кабинете туриста"))
        sheet.showPage()
    sheet.save()
    return buffer.getvalue()


# --- DOCX ---

DOCX_FONT = "Times New Roman"


def _docx_field(paragraph, code):
    """Вставляет поле Word (PAGE / NUMPAGES) для автонумерации."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = code
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instruction)
    run._r.append(end)
    return run


def to_docx(text, meta=None):
    """Текст -> DOCX с колонтитулами и автонумерацией страниц."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    meta = dict(meta or {})
    document = Document()

    normal = document.styles["Normal"]
    normal.font.name = DOCX_FONT
    normal.font.size = Pt(BODY_SIZE)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), DOCX_FONT)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.15

    section = document.sections[0]
    section.left_margin = Mm(20)
    section.right_margin = Mm(15)
    section.top_margin = Mm(20)
    section.bottom_margin = Mm(20)

    head = section.header.paragraphs[0]
    head.text = str(meta.get("agency") or "")
    head.add_run("\t" + str(meta.get("deal_number") or ""))
    head.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in head.runs:
        run.font.size = Pt(8)

    foot = section.footer.paragraphs[0]
    footer_text = " · ".join(part for part in (meta.get("document_code"), meta.get("generated_at")) if part)
    foot.text = footer_text
    foot.add_run("\tстр. ")
    _docx_field(foot, "PAGE")
    foot.add_run(" из ")
    _docx_field(foot, "NUMPAGES")
    for run in foot.runs:
        run.font.size = Pt(8)

    for raw in str(text).replace("\r\n", "\n").split("\n"):
        stripped = raw.strip()
        paragraph = document.add_paragraph()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent:
            paragraph.paragraph_format.left_indent = Pt(min(indent * 2.0, 40.0))
        run = paragraph.add_run(stripped)
        if _is_title(stripped):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run.bold = True
            run.font.size = Pt(TITLE_SIZE)
        else:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    if meta.get("verify_url"):
        note = document.add_paragraph()
        note_run = note.add_run("Проверка документа: " + str(meta["verify_url"]))
        note_run.font.size = Pt(8)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def merge_pdfs(items):
    """Собирает единый PDF пакета из нескольких файлов."""
    from pypdf import PdfReader, PdfWriter

    parts = [item for item in items if item]
    if not parts:
        raise AppError("Нет файлов для сборки пакета")
    writer = PdfWriter()
    for index, data in enumerate(parts, start=1):
        try:
            reader = PdfReader(io.BytesIO(data))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as error:  # нечитаемый файл не должен ронять всю выгрузку
            raise AppError(
                f"Файл №{index} в пакете не похож на PDF — загрузите его заново",
                details={"reason": str(error)},
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def page_count(data):
    """Число страниц в готовом PDF."""
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


def digest(data):
    """SHA-256 файла: к нему привязывается простая электронная подпись."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


def build_files(text, meta=None):
    """Готовый текст -> {pdf, docx, sha256, pages}."""
    pdf = to_pdf(text, meta)
    docx = to_docx(text, meta)
    return {
        "pdf": pdf,
        "docx": docx,
        "sha256": digest(pdf),
        "pages": page_count(pdf),
    }
