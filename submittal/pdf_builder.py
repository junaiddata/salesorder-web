"""
Submittal PDF Builder
Generates title page, index page, section divider pages, materials table
with ReportLab, then merges all sections using PyPDF2.
Divider pages are auto-generated (not uploaded).
"""
import os
from io import BytesIO

from django.conf import settings
from PyPDF2 import PdfMerger
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, BaseDocTemplate, Frame, PageTemplate,
    NextPageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.pdfgen import canvas

from .models import Submittal, SubmittalSectionUpload
from . import services


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BLUE_DARK = HexColor('#003399')
RED_ACCENT = HexColor('#CC0000')
WHITE = colors.white

PAGE_W, PAGE_H = A4

# Standard sections that have content auto-provided (no upload needed)
AUTO_CONTENT_LABELS = {
    "title page", "index",
    "company profile", "trade license",
    "list of proposed material",
    "product catalogue", "technical details",
    "test certificates", "country of origin certificate",
    "previous approvals",
}

# Sections that need per-submittal upload (unless user removed them from index)
UPLOAD_LABELS = {
    "highlighted vendor list", "comply statement with project specification",
    "comply statement", "area of application", "warranty draft letter",
}


DEFAULT_INDEX_ITEMS = [
    "Title Page",
    "Index",
    "Company Profile",
    "Trade License",
    "Highlighted Vendor List",
    "Comply Statement with Project Specification",
    "List of Proposed Material",
    "Area of Application",
    "Product Catalogue",
    "Technical Details",
    "Test Certificates",
    "Country of Origin Certificate",
    "Warranty Draft Letter",
    "Previous Approvals",
]

INDEX_LABEL_TO_SECTION = {
    "title page": 1,
    "index": 2,
    "company profile": 3,
    "trade license": 4,
    "highlighted vendor list": 5,
    "comply statement with project specification": 6,
    "comply statement": 6,
    "list of proposed material": 7,
    "area of application": 8,
    "product catalogue": 9,
    "technical details": 10,
    "test certificates": 11,
    "country of origin certificate": 12,
    "warranty draft letter": 13,
    "previous approvals": 14,
}


def _norm(label: str) -> str:
    return (label or "").strip().lower()


def _label_to_section(label: str):
    return INDEX_LABEL_TO_SECTION.get(_norm(label))


def needs_upload(label: str) -> bool:
    """Return True if this index label requires a per-submittal file upload."""
    key = _norm(label)
    if key in AUTO_CONTENT_LABELS:
        return False
    if key in UPLOAD_LABELS:
        return True
    # Custom (unknown) labels always need an upload
    if key not in INDEX_LABEL_TO_SECTION:
        return True
    return False


# ---------------------------------------------------------------------------
# Shared company header drawing (used by title, index, divider pages)
# ---------------------------------------------------------------------------

def _draw_left_strips(c):
    c.setFillColor(RED_ACCENT)
    c.rect(0, 0, 12, PAGE_H, fill=1, stroke=0)
    c.setFillColor(BLUE_DARK)
    c.rect(12, 0, 8, PAGE_H, fill=1, stroke=0)


def _draw_company_header(c, box_y=None, box_h=120, use_black=False):
    """Draw the company header box. Returns bottom y of the box.
    use_black: if True, use black instead of blue (for compliance statement)."""
    cx = PAGE_W / 2
    if box_y is None:
        box_y = PAGE_H - 170
    box_x = 60
    box_w = PAGE_W - 120

    header_color = colors.black if use_black else BLUE_DARK
    c.setStrokeColor(header_color)
    c.setLineWidth(2)
    c.rect(box_x, box_y, box_w, box_h, fill=0, stroke=1)

    c.setFont('Helvetica-Bold', 15)
    c.setFillColor(header_color)
    c.drawCentredString(cx, box_y + box_h - 26, 'JUNAID SANT. & ELECT. MAT. TR. LLC')

    c.setFont('Helvetica', 8.5)
    c.setFillColor(colors.black)
    info = [
        'P.O. Box 34862, Dubai, U.A.E.',
        'Tel: +971 4 236 7723, Fax: +971 4 236 7750',
        'E-mail: project@junaid.ae',
    ]
    for i, line in enumerate(info):
        c.drawCentredString(cx, box_y + box_h - 46 - i * 13, line)

    return box_y


# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------
BLUE_DARK = colors.HexColor('#000080')      # Navy blue for company name / title
BLUE_STRIP_DARK = colors.HexColor('#1a5276')  # Dark blue strip
BLUE_STRIP_MED = colors.HexColor('#2e86c1')   # Medium blue strip
BLUE_STRIP_LIGHT = colors.HexColor('#85c1e9') # Light blue strip
GREY_STRIP = colors.HexColor('#b0b0b0')       # Grey strip


# ---------------------------------------------------------------------------
# Decorative corner strips (diagonal bars – left-top & right-bottom)
# ---------------------------------------------------------------------------

def _draw_corner_strips(c):
    """Draw diagonal decorative strips at top-left and bottom-right corners."""
    
    # ── Top-left corner strips ────────────────────────────────────────
    strip_colors_tl = [
        BLUE_STRIP_DARK,
        BLUE_STRIP_MED,
        BLUE_STRIP_LIGHT,
        GREY_STRIP,
    ]
    strip_w = 38
    for i, col in enumerate(strip_colors_tl):
        c.saveState()
        c.setFillColor(col)
        c.setStrokeColor(col)
        x_offset = i * strip_w
        p = c.beginPath()
        p.moveTo(x_offset, PAGE_H)
        p.lineTo(x_offset + strip_w, PAGE_H)
        p.lineTo(x_offset + strip_w, PAGE_H - 180)
        p.lineTo(x_offset, PAGE_H - 130)
        p.close()
        c.drawPath(p, fill=1, stroke=0)
        c.restoreState()

    # ── Bottom-right corner strips ────────────────────────────────────
    strip_colors_br = [
        GREY_STRIP,
        BLUE_STRIP_LIGHT,
        BLUE_STRIP_MED,
        BLUE_STRIP_DARK,
    ]
    for i, col in enumerate(strip_colors_br):
        c.saveState()
        c.setFillColor(col)
        x_start = PAGE_W - (len(strip_colors_br) - i) * strip_w
        p = c.beginPath()
        p.moveTo(x_start, 0)
        p.lineTo(x_start + strip_w, 0)
        p.lineTo(x_start + strip_w, 130)
        p.lineTo(x_start, 180)
        p.close()
        c.drawPath(p, fill=1, stroke=0)
        c.restoreState()


# ---------------------------------------------------------------------------
# Outer border
# ---------------------------------------------------------------------------

def _draw_outer_border(c):
    """Draw thin black outer border with margin."""
    margin = 20
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.rect(margin, margin, PAGE_W - 2 * margin, PAGE_H - 2 * margin, fill=0, stroke=1)


# ---------------------------------------------------------------------------
# Text wrapping helper
# ---------------------------------------------------------------------------

def _draw_wrapped(c, text, x, y, max_width, font_size, leading):
    """Draw text with word wrapping. Returns final y position."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = str(text).split()
    line = ''
    current_y = y
    for word in words:
        test = (line + ' ' + word).strip()
        if stringWidth(test, c._fontname, font_size) < max_width:
            line = test
        else:
            if line:
                c.drawString(x, current_y, line)
                current_y -= leading
            line = word
    if line:
        c.drawString(x, current_y, line)
        current_y -= leading
    return current_y


# ---------------------------------------------------------------------------
# Font Registration (add this at the top of your file)
# ---------------------------------------------------------------------------
import os
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Download these fonts and save in media/fonts/ ─────────────────────────
# Option 1: Playfair Display (elegant, similar to MATERIAL SUBMITTAL style)
# Option 2: Montserrat (modern, clean)
# Download from: https://fonts.google.com/

FONT_DIR = os.path.join('media', 'fonts')

def _register_custom_fonts():
    """Register custom fonts. Call once at startup."""
    font_map = {
        # Title font (like MATERIAL SUBMITTAL in image)
        'PlayfairDisplay':       'PlayfairDisplay-Regular.ttf',
        'PlayfairDisplay-Bold':  'PlayfairDisplay-Bold.ttf',

        # Modern body font
        'Montserrat':            'Montserrat-Regular.ttf',
        'Montserrat-Bold':       'Montserrat-Bold.ttf',
        'Montserrat-SemiBold':   'Montserrat-SemiBold.ttf',

        # Alternative: Poppins (very modern, clean)
        'Poppins':               'Poppins-Regular.ttf',
        'Poppins-Bold':          'Poppins-Bold.ttf',
        'Poppins-SemiBold':      'Poppins-SemiBold.ttf',
    }

    for font_name, filename in font_map.items():
        font_path = os.path.join(FONT_DIR, filename)
        if os.path.exists(font_path):
            try:
                pdfmetrics.registerFont(TTFont(font_name, font_path))
            except Exception:
                pass  # Fall back to Helvetica if font not found


# Call once at module load
_register_custom_fonts()


# ---------------------------------------------------------------------------
# Font availability helper
# ---------------------------------------------------------------------------

def _get_font(preferred, fallback='Helvetica'):
    """Return preferred font if registered, else fallback."""
    try:
        pdfmetrics.getFont(preferred)
        return preferred
    except KeyError:
        return fallback


def _get_font_bold(preferred, fallback='Helvetica-Bold'):
    try:
        pdfmetrics.getFont(preferred)
        return preferred
    except KeyError:
        return fallback


# ── Resolved font names ──────────────────────────────────────────────────
FONT_TITLE       = _get_font('PlayfairDisplay-Bold', 'Helvetica-Bold')
FONT_LABEL_BOLD  = _get_font_bold('Montserrat-Bold', 'Helvetica-Bold')
FONT_LABEL       = _get_font('Montserrat-SemiBold', 'Helvetica-Bold')
FONT_BODY        = _get_font('Montserrat', 'Helvetica')
FONT_BODY_BOLD   = _get_font_bold('Montserrat-Bold', 'Helvetica-Bold')


# ---------------------------------------------------------------------------
# Text wrapping helper (updated with font param)
# ---------------------------------------------------------------------------

def _draw_wrapped(c, text, x, y, max_width, font_name, font_size, leading):
    """Draw text with word wrapping using specified font. Returns final y."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = str(text).split()
    line = ''
    current_y = y
    c.setFont(font_name, font_size)
    for word in words:
        test = (line + ' ' + word).strip()
        if stringWidth(test, font_name, font_size) < max_width:
            line = test
        else:
            if line:
                c.drawString(x, current_y, line)
                current_y -= leading
            line = word
    if line:
        c.drawString(x, current_y, line)
        current_y -= leading
    return current_y


# ---------------------------------------------------------------------------
# Title Page (Section 1) – Background image + modern font overlay
# ---------------------------------------------------------------------------

BLUE_DARK = colors.HexColor('#000080')

def _build_title_page(submittal: Submittal) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    # ── Draw background image (full page) ─────────────────────────────
    bg_path = os.path.join('media', 'title_page_bg1.png')
    if os.path.exists(bg_path):
        c.drawImage(
            bg_path,
            0, 0,
            width=PAGE_W,
            height=PAGE_H,
            preserveAspectRatio=False,
            mask='auto',
        )

    from reportlab.pdfbase.pdfmetrics import stringWidth

    # ── Dynamic fields ────────────────────────────────────────────────
    fields = [
        ('Project',        submittal.project),
        ('Employer',       submittal.client),
        ('Consultant',     submittal.consultant),
        ('Contractor',     submittal.main_contractor),
        ('MEP Contractor', submittal.mep_contractor),
        ('Product',        submittal.product),
        ('Manufacturer',   getattr(submittal, 'manufacturer', '')),
    ]

    # Starting Y position below "MATERIAL SUBMITTAL" in background
    field_y = PAGE_H - 370
    arrow_x = 58
    label_x = 72
    colon_x = 178
    value_x = 188
    max_w = PAGE_W - value_x - 60

    for label, value in fields:
        if not value:
            continue

        # ── Arrow "❯" ────────────────────────────────────────────────
        c.setFillColor(BLUE_DARK)
        c.setFont(FONT_BODY_BOLD, 12)
        c.drawString(arrow_x, field_y, '\u276F')

        # ── Underlined label ─────────────────────────────────────────
        c.setFillColor(colors.black)
        c.setFont(FONT_LABEL, 10)
        c.drawString(label_x, field_y, label)

        # Underline
        lw = stringWidth(label, FONT_LABEL, 10)
        c.setStrokeColor(colors.black)
        c.setLineWidth(0.5)
        c.line(label_x, field_y - 2, label_x + lw, field_y - 2)

        # ── Colon ───────────────────────────────────────────────────
        c.setFont(FONT_LABEL_BOLD, 10)
        c.drawString(colon_x, field_y, ':')

        # ── Value (wrapped, modern body font) ────────────────────────
        c.setFillColor(colors.black)
        end_y = _draw_wrapped(
            c, str(value),
            value_x, field_y,
            max_w,
            FONT_BODY_BOLD, 10, 15,
        )

        lines_used = max(1, int((field_y - end_y) / 15) + 1)
        field_y -= max(34, lines_used * 15 + 12)

    c.save()
    buf.seek(0)
    return buf


def _draw_wrapped(c, text, x, y, max_w, font_name, font_size, leading):
    words = str(text).split()
    line = ''
    cy = y
    c.setFont(font_name, font_size)
    for w in words:
        test = f'{line} {w}'.strip()
        if c.stringWidth(test, font_name, font_size) > max_w:
            c.drawString(x, cy, line)
            cy -= leading
            line = w
        else:
            line = test
    if line:
        c.drawString(x, cy, line)
    return cy


# ---------------------------------------------------------------------------
# Index Page (Section 2)
# ---------------------------------------------------------------------------

def _build_index_page(items: list) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    _draw_left_strips(c)
    _draw_company_header(c)

    title_y = PAGE_H - 170 - 50
    c.setFont('Helvetica-Bold', 20)
    c.setFillColor(BLUE_DARK)
    c.drawCentredString(cx, title_y, 'INDEX')
    tw = c.stringWidth('INDEX', 'Helvetica-Bold', 20)
    c.setStrokeColor(BLUE_DARK)
    c.setLineWidth(1.5)
    c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

    row_xl, row_xr = 80, PAGE_W - 80
    row_y = title_y - 36
    row_h = 26

    for idx, label in enumerate(items, 1):
        bg = HexColor('#EEF2F8') if idx % 2 == 0 else WHITE
        c.setFillColor(bg)
        c.rect(row_xl - 6, row_y - 6, row_xr - row_xl + 12, row_h, fill=1, stroke=0)

        c.setFillColor(BLUE_DARK)
        c.roundRect(row_xl - 6, row_y - 4, 26, 20, 4, fill=1, stroke=0)
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(WHITE)
        c.drawCentredString(row_xl + 6, row_y + 3, str(idx))

        c.setFont('Helvetica', 10)
        c.setFillColor(colors.black)
        c.drawString(row_xl + 28, row_y + 3, label)

        c.setStrokeColor(HexColor('#CBD5E1'))
        c.setLineWidth(0.5)
        c.setDash([2, 3])
        lw = c.stringWidth(label, 'Helvetica', 10)
        x1, x2 = row_xl + 30 + lw + 6, row_xr - 28
        if x2 > x1:
            c.line(x1, row_y + 7, x2, row_y + 7)
        c.setDash()
        row_y -= row_h

    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Watermark (logo from media)
# ---------------------------------------------------------------------------

WATERMARK_FILENAMES = ('footer-logo1.png', 'footer-logo.png')


def _draw_watermark(c, cx):
    """Draw watermark: logo image from media if available, else text."""
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'media')
    media_root = os.path.abspath(str(media_root))

    for filename in WATERMARK_FILENAMES:
        path = os.path.join(media_root, filename)
        if os.path.isfile(path):
            try:
                from PIL import Image
                from reportlab.lib.utils import ImageReader

                im = Image.open(path).convert('RGBA')
                iw, ih = im.size
                # Reduce opacity to ~15% (very light, like old text watermark #E8ECF0)
                data = im.getdata()
                faded = []
                for item in data:
                    r, g, b, a = item
                    faded.append((r, g, b, int(a * 0.15)))
                im.putdata(faded)
                buf = BytesIO()
                im.save(buf, format='PNG')
                buf.seek(0)
                im.close()

                img = ImageReader(buf)
                max_w = 250
                scale = min(1.0, max_w / iw) if iw else 1.0
                w, h = iw * scale, ih * scale
                x = cx - w / 2
                y = PAGE_H / 2 - h / 2
                c.saveState()
                c.drawImage(img, x, y, width=w, height=h, mask='auto')
                c.restoreState()
                return
            except Exception:
                continue
    # Fallback: text watermark
    c.saveState()
    c.setFillColor(HexColor('#E8ECF0'))
    c.setFont('Helvetica-Bold', 42)
    c.drawCentredString(cx, PAGE_H / 2 + 30, 'JUNAID')
    c.setFont('Helvetica', 16)
    c.drawCentredString(cx, PAGE_H / 2 - 5, 'GROUP OF COMPANIES')
    c.restoreState()


# ---------------------------------------------------------------------------
# Section Divider Page – Background image + centered section text overlay
# ---------------------------------------------------------------------------

def _wrap_to_lines(text, max_width, font_name, font_size, max_lines=2):
    """Wrap text to fit max_width, return list of lines (at most max_lines)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = str(text).split()
    if not words:
        return ['']
    lines = []
    line = ''
    for w in words:
        test = (line + ' ' + w).strip() if line else w
        if stringWidth(test, font_name, font_size) <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
                if len(lines) >= max_lines:
                    return lines
            line = w
    if line:
        lines.append(line)
    return lines


def _draw_centered_wrapped(c, text, cx, y, max_width, font_name, font_size, leading=30, max_lines=2):
    """Draw text wrapped to max_lines, each line centered. Returns final y."""
    lines = _wrap_to_lines(text, max_width, font_name, font_size, max_lines)
    c.setFont(font_name, font_size)
    n = len(lines)
    start_y = y + (n - 1) * leading / 2 if n > 1 else y
    for i, line in enumerate(lines):
        c.drawCentredString(cx, start_y - i * leading, line)
    return start_y - (n - 1) * leading


def _build_divider_page(section_number: int, section_name: str) -> BytesIO:
    """
    Build divider page using a pre-designed background image.
    Only the section name text is drawn dynamically on top.
    
    Background image: media/divider_page_bg.png
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    cx = PAGE_W / 2

    # ── Draw background image (full page) ─────────────────────────────
    bg_path = os.path.join('media', 'divider_page_bg.png')
    if os.path.exists(bg_path):
        c.drawImage(
            bg_path,
            0, 0,
            width=PAGE_W,
            height=PAGE_H,
            preserveAspectRatio=False,
            mask='auto',
        )

    # ── Section text overlay (centered on page) ──────────────────────
    # Use modern font (Montserrat Bold or PlayfairDisplay Bold)
    title_font = _get_font_bold('Montserrat-Bold', 'Helvetica-Bold')
    
    section_text = section_name
    max_w = PAGE_W - 120  # padding from edges

    # Determine font size based on text length
    if len(section_text) <= 25:
        font_size = 26
    elif len(section_text) <= 45:
        font_size = 22
    else:
        font_size = 18

    leading = font_size + 8

    # Center vertically – slightly above page center 
    # (logo watermark is roughly at center, text sits over it)
    center_y = PAGE_H / 2 + 10

    c.setFillColor(colors.HexColor('#1a1a1a'))  # near-black for readability
    _draw_centered_wrapped(
        c,
        section_text,
        cx,
        center_y,
        max_w,
        title_font,
        font_size,
        leading=leading,
        max_lines=2,
    )

    c.save()
    buf.seek(0)
    return buf

# ---------------------------------------------------------------------------
# Proposed Materials Table (Section 7) – Background image + table overlay
# ---------------------------------------------------------------------------

def _get_effective_columns(submittal, column_override=None):
    """Return list of (key, label) for materials table."""
    materials = submittal.materials.select_related('brand').all()
    if not materials:
        return [('model_no', 'Model No.'), ('item_description', 'Item Description')]

    seen_keys = {}
    for mat in materials:
        if not mat.brand or not mat.brand.column_definitions:
            continue
        for col in mat.brand.column_definitions:
            key = col.get('key') or col.get('label', '')
            if key and key not in seen_keys:
                seen_keys[key] = col.get('label', key)

    if not seen_keys:
        return [('model_no', 'Model No.'), ('item_description', 'Item Description')]

    first_brand = next((m.brand for m in materials if m.brand and m.brand.column_definitions), None)
    if first_brand:
        ordered = []
        for col in first_brand.column_definitions:
            key = col.get('key')
            if key and key in seen_keys:
                ordered.append((key, seen_keys[key]))
        for k in seen_keys:
            if k not in [x[0] for x in ordered]:
                ordered.append((k, seen_keys[k]))
        cols = ordered
    else:
        cols = list(seen_keys.items())

    sel = column_override if column_override is not None else (submittal.materials_columns or [])
    if sel:
        cols = [(k, lbl) for k, lbl in cols if k in sel]
    return cols if cols else [('model_no', 'Model No.')]


def _get_warranty_columns(submittal):
    """Return columns for warranty materials table."""
    sel = submittal.warranty_materials_columns if submittal.warranty_materials_columns else None
    return _get_effective_columns(submittal, column_override=sel)


class _MaterialPageTemplate(BaseDocTemplate):
    """
    Custom doc template that draws material_page_bg.png on every page.
    The table flows naturally within the frame area below the header/title
    already baked into the background image.
    """

    def __init__(self, buf, bg_path, **kwargs):
        self._bg_path = bg_path
        super().__init__(buf, **kwargs)

        # Frame starts below the header + "LIST OF PROPOSED MATERIALS" title
        # in the background image. Adjust top_start based on your bg image.
        top_start = kwargs.get('topMargin', 230)
        bottom_end = kwargs.get('bottomMargin', 50)
        left = kwargs.get('leftMargin', 50)
        right = kwargs.get('rightMargin', 50)
        frame_w = PAGE_W - left - right
        frame_h = PAGE_H - top_start - bottom_end

        frame = Frame(
            left,
            bottom_end,
            frame_w,
            frame_h,
            id='material_frame',
        )
        template = PageTemplate(
            id='material_bg',
            frames=[frame],
            onPage=self._draw_bg,
        )
        self.addPageTemplates([template])

    def _draw_bg(self, canvas_obj, doc):
        """Draw background image on every page."""
        if os.path.exists(self._bg_path):
            canvas_obj.saveState()
            canvas_obj.drawImage(
                self._bg_path,
                0, 0,
                width=PAGE_W,
                height=PAGE_H,
                preserveAspectRatio=False,
                mask='auto',
            )
            canvas_obj.restoreState()


def _build_materials_table(submittal: Submittal) -> BytesIO:
    """
    Build materials table PDF with background image on every page.
    Background: media/material_page_bg.png
    Table is rendered inside frame area below the pre-designed header & title.
    """
    buf = BytesIO()
    bg_path = os.path.join('media', 'materials_page_bg.png')

    # ── Use custom template with background ───────────────────────────
    doc = _MaterialPageTemplate(
        buf,
        bg_path=bg_path,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=230,     # below header + "LIST OF PROPOSED MATERIALS" in bg
        bottomMargin=50,
    )

    # ── Font setup ────────────────────────────────────────────────────
    header_font = _get_font_bold('Montserrat-Bold', 'Helvetica-Bold')
    body_font = _get_font('Montserrat', 'Helvetica')

    style_cell = ParagraphStyle(
        'MatCell',
        fontSize=8,
        fontName=body_font,
        leading=10,
        textColor=colors.black,
    )
    style_header = ParagraphStyle(
        'MatHeader',
        fontSize=8,
        fontName=header_font,
        textColor=colors.white,
        leading=10,
    )

    # ── Fetch materials & columns ─────────────────────────────────────
    materials = (
        submittal.materials
        .select_related('brand')
        .all()
        .order_by('display_order', 'model_no')
    )
    cols = _get_effective_columns(submittal)

    # ── Build table header ────────────────────────────────────────────
    header_row = [Paragraph('S.No', style_header)] + [
        Paragraph(lbl, style_header) for _, lbl in cols
    ]

    # ── Build table rows ──────────────────────────────────────────────
    data = [header_row]
    for idx, mat in enumerate(materials, 1):
        row = [Paragraph(str(idx), style_cell)]
        for key, _ in cols:
            if key == 'model_no':
                val = mat.model_no
            else:
                val = mat.get(key, '')
            row.append(Paragraph(str(val or ''), style_cell))
        data.append(row)

    # ── Column widths ─────────────────────────────────────────────────
    ncols = len(cols) + 1
    available_w = PAGE_W - 100  # left + right margin
    col_widths = [30] + [max(45, (available_w - 30) // (ncols - 1))] * (ncols - 1)

    # ── Create table ──────────────────────────────────────────────────
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        # Header row styling
        ('BACKGROUND',     (0, 0), (-1, 0), BLUE_DARK),
        ('TEXTCOLOR',      (0, 0), (-1, 0), colors.white),
        ('FONTNAME',       (0, 0), (-1, 0), header_font),
        ('FONTSIZE',       (0, 0), (-1, 0), 8),

        # Body styling
        ('FONTNAME',       (0, 1), (-1, -1), body_font),
        ('FONTSIZE',       (0, 1), (-1, -1), 8),
        ('TEXTCOLOR',      (0, 1), (-1, -1), colors.black),

        # Grid & padding
        ('GRID',           (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, HexColor('#F5F7FA')]),
        ('VALIGN',         (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING',     (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING',  (0, 0), (-1, -1), 4),
        ('LEFTPADDING',    (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',   (0, 0), (-1, -1), 4),
    ]))

    # ── Build PDF ─────────────────────────────────────────────────────
    elements = [table]
    doc.build(elements)
    buf.seek(0)
    return buf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_path(file_field):
    if file_field and file_field.name:
        try:
            return file_field.path
        except (ValueError, FileNotFoundError):
            return None
    return None


def _get_upload_path(submittal, label):
    """Get the per-submittal uploaded file path for a given index label."""
    try:
        upload = SubmittalSectionUpload.objects.get(
            submittal=submittal, index_label=label
        )
        if upload.file and upload.file.name:
            return upload.file.path
    except SubmittalSectionUpload.DoesNotExist:
        pass
    return None


def _get_ordered_index_items(submittal: Submittal) -> list:
    """
    Return list of {display, canonical} for included index items.
    - display: shown in index PDF and divider pages (display_label or label)
    - canonical: used for section lookup and upload lookup (always label)
    """
    raw_items = submittal.index_items or []
    items = []
    for entry in raw_items:
        if isinstance(entry, dict):
            if entry.get('included', True):
                canonical = entry.get('label', '')
                display = entry.get('display_label') or canonical
                items.append({'display': display, 'canonical': canonical})
        elif isinstance(entry, str):
            items.append({'display': entry, 'canonical': entry})
    if not items:
        for lbl in DEFAULT_INDEX_ITEMS:
            items.append({'display': lbl, 'canonical': lbl})
    return items


def _append(merger, pdf, add_divider=False, div_num=0, div_name=''):
    """Append optional divider then content to merger."""
    if add_divider and div_name:
        divider = _build_divider_page(div_num, div_name)
        merger.append(divider)
    if pdf is None:
        return
    if isinstance(pdf, BytesIO):
        merger.append(pdf)
    elif isinstance(pdf, str) and os.path.exists(pdf):
        merger.append(pdf)


# ---------------------------------------------------------------------------
# Compliance Statement PDF (Section 6 - generated from form rows)
# ---------------------------------------------------------------------------

def _build_compliance_statement_pdf(submittal: Submittal) -> BytesIO:
    """
    Build a multi-page compliance statement PDF from submittal.compliance_rows.
    First page: company header + horizontal line + title + project details + table.
    Continuation pages: title only + table (no header / project details).
    """
    rows = submittal.compliance_rows or []

    buf = BytesIO()

    # Page frame leaves room for the first-page header content
    FIRST_TOP_MARGIN = 320   # space for header, title, and project details
    LATER_TOP_MARGIN = 170   # space for company header + title on continuation pages
    BOTTOM_MARGIN = 50       # space for "Page X of Y"

    style_cell = ParagraphStyle('CSCell', fontSize=8, fontName='Helvetica', leading=11)
    style_header_cell = ParagraphStyle('CSHdr', fontSize=8, fontName='Helvetica-Bold', textColor=WHITE, leading=10)
    style_bold = ParagraphStyle('CSBold', fontSize=8, fontName='Helvetica-Bold', leading=11)

    def _draw_first_page(canvas_obj, doc):
        """Draw company header, title, and project details on the first page."""
        c = canvas_obj
        cx = PAGE_W / 2

        _draw_left_strips(c)
        _draw_company_header(c, box_y=PAGE_H - 150, box_h=100, use_black=True)

        # Horizontal rule
        rule_y = PAGE_H - 160
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(36, rule_y, PAGE_W - 36, rule_y)

        # Title
        title_y = rule_y - 28
        c.setFont('Helvetica-Bold', 16)
        c.setFillColor(colors.black)
        c.drawCentredString(cx, title_y, 'COMPLIANCE STATEMENT')
        tw = c.stringWidth('COMPLIANCE STATEMENT', 'Helvetica-Bold', 16)
        c.setLineWidth(1.2)
        c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

        # Project details
        fields = [
            ('Project', submittal.project),
            ('Client', submittal.client),
            ('Consultant', submittal.consultant),
            ('Main Contractor', submittal.main_contractor),
            ('MEP Contractor', submittal.mep_contractor),
            ('Product', submittal.product),
        ]
        field_y = title_y - 30
        label_x, colon_x, value_x = 55, 170, 180
        max_w = PAGE_W - value_x - 36

        c.setFont('Helvetica', 9)
        c.setFillColor(colors.black)
        for lbl, val in fields:
            if not val:
                continue
            c.setFont('Helvetica-Bold', 9)
            c.drawString(label_x, field_y, lbl)
            c.drawString(colon_x, field_y, ':')
            c.setFont('Helvetica', 9)
            _draw_wrapped(c, val, value_x, field_y, max_w, 'Helvetica', 9, 12)
            field_y -= 16

        # Page X of Y at bottom (small)
        page_num = getattr(doc, 'page', 1)
        total_pages = getattr(doc, '_compliance_total_pages', page_num)
        c.setFont('Helvetica', 8)
        c.setFillColor(colors.grey)
        c.drawCentredString(cx, 22, f'Page {page_num} of {total_pages}')

    def _draw_later_page(canvas_obj, doc):
        """Draw company header + title on continuation pages. Page X of Y at bottom."""
        c = canvas_obj
        cx = PAGE_W / 2

        _draw_left_strips(c)
        _draw_company_header(c, box_y=PAGE_H - 150, box_h=100, use_black=True)

        # Horizontal rule
        rule_y = PAGE_H - 160
        c.setStrokeColor(colors.black)
        c.setLineWidth(1)
        c.line(36, rule_y, PAGE_W - 36, rule_y)

        # Title (no "continued")
        title_y = rule_y - 28
        c.setFont('Helvetica-Bold', 16)
        c.setFillColor(colors.black)
        c.drawCentredString(cx, title_y, 'COMPLIANCE STATEMENT')
        tw = c.stringWidth('COMPLIANCE STATEMENT', 'Helvetica-Bold', 16)
        c.setLineWidth(1.2)
        c.line(cx - tw / 2, title_y - 4, cx + tw / 2, title_y - 4)

        # Page X of Y at bottom (small)
        page_num = getattr(doc, 'page', 1)
        total_pages = getattr(doc, '_compliance_total_pages', page_num)
        c.setFont('Helvetica', 8)
        c.setFillColor(colors.grey)
        c.drawCentredString(cx, 22, f'Page {page_num} of {total_pages}')

    # Build table data
    col_widths = [28, 235, 100, 155]
    header_row = [
        Paragraph('SR.<br/>NO.', style_header_cell),
        Paragraph('SPECIFICATION', style_header_cell),
        Paragraph('COMPLIANCE', style_header_cell),
        Paragraph('REMARKS', style_header_cell),
    ]
    table_data = [header_row]

    for i, row in enumerate(rows, 1):
        spec = row.get('specification', '') or ''
        compliance = row.get('compliance', '') or ''
        remarks = row.get('remarks', '') or ''
        table_data.append([
            Paragraph(str(i), style_cell),
            Paragraph(str(spec).replace('\n', '<br/>'), style_cell),
            Paragraph(str(compliance), style_bold if compliance else style_cell),
            Paragraph(str(remarks).replace('\n', '<br/>'), style_cell),
        ])

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.black),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor('#F5F7FA')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))

    first_frame = Frame(
        36, BOTTOM_MARGIN,
        PAGE_W - 72, PAGE_H - FIRST_TOP_MARGIN - BOTTOM_MARGIN,
        id='first',
    )
    later_frame = Frame(
        36, BOTTOM_MARGIN,
        PAGE_W - 72, PAGE_H - LATER_TOP_MARGIN - BOTTOM_MARGIN,
        id='later',
    )

    # Two-pass: first build to get page count, then build with "Page X of Y"
    total_pages = [1]

    def _first_page_cb(c, doc):
        doc._compliance_total_pages = total_pages[0]
        _draw_first_page(c, doc)

    def _later_page_cb(c, doc):
        doc._compliance_total_pages = total_pages[0]
        _draw_later_page(c, doc)

    # First pass: build to temp buffer to get page count
    temp_buf = BytesIO()
    doc_temp = BaseDocTemplate(
        temp_buf,
        pagesize=A4,
        leftMargin=36, rightMargin=36,
        topMargin=FIRST_TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )
    doc_temp.addPageTemplates([
        PageTemplate(id='First', frames=[first_frame], onPage=lambda c, d: None),
        PageTemplate(id='Later', frames=[later_frame], onPage=lambda c, d: None),
    ])
    doc_temp.build([NextPageTemplate('Later'), tbl])
    temp_buf.seek(0)
    from PyPDF2 import PdfReader
    total_pages[0] = len(PdfReader(temp_buf).pages)

    # Second pass: build with correct page callbacks
    doc3 = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=36, rightMargin=36,
        topMargin=FIRST_TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )
    doc3.addPageTemplates([
        PageTemplate(id='First', frames=[first_frame], onPage=_first_page_cb),
        PageTemplate(id='Later', frames=[later_frame], onPage=_later_page_cb),
    ])
    doc3.build([NextPageTemplate('Later'), tbl])
    buf.seek(0)
    return buf


import os
from io import BytesIO
from datetime import date
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm, inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus.doctemplate import PageTemplate, BaseDocTemplate, Frame


PAGE_W, PAGE_H = A4


def _draw_draft_watermark(c, doc):
    """Draw DRAFT watermark and logo on every page."""
    c.saveState()

    # ── DRAFT watermark ──────────────────────────────────────────────
    c.setFont('Helvetica-Bold', 72)
    c.setFillColor(colors.Color(0.75, 0.75, 0.75, alpha=0.35))
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, 'DRAFT')
    c.restoreState()

    c.saveState()

    # ── Logo top-right ───────────────────────────────────────────────
    logo_path = os.path.join('media', 'footer-logo1.png')
    if os.path.exists(logo_path):
        logo_w = 160
        logo_h = 55
        c.drawImage(
            logo_path,
            PAGE_W - 36 - logo_w,
            PAGE_H - 36 - logo_h,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            mask='auto',
        )

    c.restoreState()


class _WarrantyDocTemplate(BaseDocTemplate):
    """Custom doc template that injects watermark + logo on every page."""

    def __init__(self, buf, **kwargs):
        super().__init__(buf, **kwargs)
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id='normal',
        )
        template = PageTemplate(
            id='warranty',
            frames=[frame],
            onPage=_draw_draft_watermark,
        )
        self.addPageTemplates([template])


def _build_warranty_letter_pdf(submittal) -> BytesIO:
    """
    Build a black-and-white warranty certificate letter styled after the
    Pegler reference image (logo top-right, DRAFT watermark, label table,
    materials table in body, TOC bold note, Mr. Junaid Nasheer sign-off).
    """
    buf = BytesIO()
    doc = _WarrantyDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=80,   # leave room for logo
        bottomMargin=50,
    )

    # ── Shared styles ────────────────────────────────────────────────
    style_body = ParagraphStyle(
        'WBody', fontSize=10, fontName='Helvetica', leading=14, spaceAfter=6,
    )
    style_title = ParagraphStyle(
        'WTitle', fontSize=11, fontName='Helvetica-Bold',
        alignment=TA_CENTER, spaceAfter=14,
        textColor=colors.black,
    )
    style_label = ParagraphStyle(
        'WLabel', fontSize=10, fontName='Helvetica', leading=14,
    )
    style_label_val = ParagraphStyle(
        'WLabelVal', fontSize=10, fontName='Helvetica', leading=14,
    )
    style_cell = ParagraphStyle(
        'WarrCell', fontSize=8, fontName='Helvetica', leading=10,
    )
    style_header = ParagraphStyle(
        'WarrHeader', fontSize=8, fontName='Helvetica-Bold',
        textColor=colors.black, leading=10,
    )

    # ── Fetch materials ───────────────────────────────────────────────
    materials = (
        submittal.materials
        .select_related('brand')
        .all()
        .order_by('display_order', 'model_no')
    )
    cols = _get_warranty_columns(submittal)

    # ── Build materials table ─────────────────────────────────────────
    header_row = [Paragraph('S.No', style_header)] + [
        Paragraph(lbl, style_header) for _, lbl in cols
    ]
    table_data = [header_row]
    for idx, mat in enumerate(materials, 1):
        row = [Paragraph(str(idx), style_cell)]
        for key, _ in cols:
            val = mat.model_no if key == 'model_no' else mat.get(key, '')
            row.append(Paragraph(str(val or ''), style_cell))
        table_data.append(row)

    ncols = len(cols) + 1
    col_widths = [25] + [max(40, 500 // ncols)] * (ncols - 1)
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('TEXTCOLOR',     (0, 0), (-1, -1), colors.black),
        ('GRID',          (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    # ── Date ─────────────────────────────────────────────────────────
    today = date.today().strftime('%B %d, %Y')

    # ── date_type / date_word ─────────────────────────────────────────
    date_type = getattr(submittal, 'warranty_date_type', 'toc') or 'toc'
    date_word = 'INVOICE' if date_type == 'invoice' else 'TOC'

    # ── Project / Employer label table ───────────────────────────────
    lbl_w = 110   # label column
    col_w = 10    # colon column
    val_w = doc.width - lbl_w - col_w

    def _lbl_row(label, value):
        return [
            Paragraph(label, style_label),
            Paragraph(':', style_label),
            Paragraph(value, style_label_val),
        ]

    info_data = [
        _lbl_row('Project',  submittal.project or ''),
        _lbl_row('Employer', submittal.client  or ''),
        _lbl_row('Subject',  'Warranty Certificate for Plumbing Valves'),
    ]
    info_tbl = Table(info_data, colWidths=[lbl_w, col_w, val_w])
    info_tbl.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE',      (0, 0), (-1, -1), 10),
        ('TEXTCOLOR',     (0, 0), (-1, -1), colors.black),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING',    (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING',   (0, 0), (-1, -1), 0),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
    ]))

    # ── Assemble flowables ────────────────────────────────────────────
    elements = [
        # Date – top left
        Paragraph(today, style_body),
        Spacer(1, 10),

        # Title
        Paragraph('<u>TO WHOM IT MAY CONCERN</u>', style_title),

        # Project / Employer / Subject
        info_tbl,
        Spacer(1, 12),

        # Intro sentence
        Paragraph(
            'This is to confirm that the below following items are manufactured in '
            'accordance with ISO 9001:2015 Quality Management Systems.',
            style_body,
        ),
        Spacer(1, 10),

        # ── Materials table sits here (replaces brand line) ───────────
        tbl,
        Spacer(1, 12),

        # Warranty duration
        Paragraph(
            f'It carries warranty for a period of 5 years from date of {date_word}. '
            'This warranty covers defects rising due to faulty manufacture.',
            style_body,
        ),

        # TOC bold note
        Paragraph(
            '<b>TOC DATE cannot be exceeded 6 months from the date of invoice</b>.',
            style_body,
        ),
        Spacer(1, 6),

        # Exclusions
        Paragraph(
            'It does not extend to defects arising due to incorrect installation/application, '
            'misuse or normal wear and tear.',
            style_body,
        ),
        Spacer(1, 6),

        Paragraph(
            'As per the manufacturer guidelines all warranty given as per the supply/invoice date.',
            style_body,
        ),
        Spacer(1, 6),

        Paragraph(
            "And Manufacturer\u2019s instructions manual must be strictly complied for warranty claim.",
            style_body,
        ),
        Spacer(1, 30),

        # Sign-off
        Paragraph('For M/s. Junaid Sanitary Electrical Material Trading LLC', style_body),
        Spacer(1, 36),   # signature gap
        Paragraph('Mr. Junaid Nasheer', style_body),
        Paragraph('Sales Manager', style_body),
    ]

    doc.build(elements)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Main Build Pipeline
# ---------------------------------------------------------------------------

def build_submittal_pdf(submittal_id: int) -> BytesIO:
    """
    Build the complete merged submittal PDF.
    Order follows submittal.index_items. Each included section gets an
    auto-generated divider page (except Title Page) then its content.
    Custom sections use SubmittalSectionUpload for content.
    """
    submittal = Submittal.objects.select_related('warranty_brand').prefetch_related('materials').get(pk=submittal_id)
    company_docs = services.get_company_documents()
    merger = PdfMerger()

    items = _get_ordered_index_items(submittal)
    display_labels = [it['display'] for it in items]
    materials = submittal.materials.all().order_by('display_order')
    seen = set()
    visible_num = 0

    for item in items:
        display = item['display']
        canonical = item['canonical']
        section = _label_to_section(canonical)
        visible_num += 1

        # ── Title Page: no divider ──
        if section == 1:
            merger.append(_build_title_page(submittal))
            seen.add(1)
            continue

        # ── Index: no divider ──
        if section == 2:
            merger.append(_build_index_page(display_labels))
            seen.add(2)
            continue

        # Skip duplicate sections
        if section is not None and section in seen:
            continue
        if section is not None:
            seen.add(section)

        # ── Standard single-content sections ──
        if section == 3:
            _append(merger, _safe_path(company_docs.company_profile_pdf),
                    True, visible_num, display)
            continue

        if section == 4:
            _append(merger, _safe_path(company_docs.trade_license_pdf),
                    True, visible_num, display)
            continue

        if section == 7:
            _append(merger, _build_materials_table(submittal),
                    True, visible_num, display)
            continue

        # ── Upload-based sections (standard or custom) ──
        if section in (5, 6, 8, 13) or section is None:
            # Always add divider; content optional (show renamed display)
            merger.append(_build_divider_page(visible_num, display))

            # Section 13: generated warranty letter (when brand has format) OR uploaded PDF
            if section == 13:
                warranty_brand = getattr(submittal, 'warranty_brand', None)
                use_generated = warranty_brand and getattr(warranty_brand, 'use_generated_warranty', False)
                if use_generated:
                    merger.append(_build_warranty_letter_pdf(submittal))
                else:
                    upload_path = _get_upload_path(submittal, canonical)
                    if not upload_path:
                        upload_path = _safe_path(submittal.warranty_draft_pdf)
                    if upload_path:
                        merger.append(upload_path)
                continue

            upload_path = _get_upload_path(submittal, canonical)
            if not upload_path:
                legacy_map = {
                    5: submittal.vendor_list_pdf,
                    6: submittal.comply_statement_file,
                    8: submittal.area_of_application_pdf,
                }
                ff = legacy_map.get(section)
                upload_path = _safe_path(ff) if ff else None
            if upload_path:
                merger.append(upload_path)

            # Section 6: also append generated compliance table if rows exist
            if section == 6 and (submittal.compliance_rows or []):
                compliance_pdf = _build_compliance_statement_pdf(submittal)
                merger.append(compliance_pdf)
            continue

        # ── Multi-material sections (9-12, 14) ──
        if section in (9, 10, 11, 12, 14):
            # Always add divider; content optional
            merger.append(_build_divider_page(visible_num, display))
            for mat in materials:
                paths = []
                if section == 9:
                    p = services.get_catalogue_pdf(mat)
                    if p:
                        paths = [p]
                elif section == 10:
                    p = services.get_technical_pdf(mat)
                    if p:
                        paths = [p]
                elif section == 11:
                    paths = services.get_certifications(mat, 'test_certificate')
                elif section == 12:
                    paths = services.get_certifications(mat, 'country_of_origin')
                elif section == 14:
                    paths = services.get_certifications(mat, 'previous_approval')
                for path in paths:
                    if path and os.path.exists(path):
                        merger.append(path)

    output = BytesIO()
    merger.write(output)
    merger.close()
    output.seek(0)
    return output
