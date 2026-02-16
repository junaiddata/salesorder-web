"""
Finance Statement PDF Export - Customer Finance Summary
Refactored for enterprise-grade visual quality using ReportLab only.
Logo source: https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp
"""
import html as html_module
import os
from io import BytesIO
from decimal import Decimal
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Value, FloatField, Max
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
    PageBreak, KeepTogether,
)

from so.models import Customer, Salesman, FinanceCreditEditLog


# ─────────────────────────────────────────────────────────────────────────────
# DESIGN CONSTANTS — single source of truth for all visual parameters
# ─────────────────────────────────────────────────────────────────────────────

# Brand palette
CLR_PRIMARY = HexColor('#1B2A4A')       # Deep navy — headers, titles
CLR_PRIMARY_LIGHT = HexColor('#2C4A7C') # Lighter navy — subtle accents
CLR_ACCENT = HexColor('#D4912A')        # Warm gold — section markers
CLR_BG_HEADER = HexColor('#1B2A4A')     # Table header background
CLR_BG_TOTAL = HexColor('#EBF0F7')      # Totals row background
CLR_BG_ZEBRA = HexColor('#F8F9FB')      # Alternating row tint
CLR_BG_SECTION = HexColor('#F3F4F6')    # Section header background
CLR_BORDER = HexColor('#D1D5DB')        # Table grid — subtle gray
CLR_BORDER_HEAVY = HexColor('#9CA3AF')  # Outer box — slightly darker
CLR_TEXT = HexColor('#1F2937')          # Body text — near-black
CLR_TEXT_MUTED = HexColor('#6B7280')    # Labels, secondary text
CLR_DANGER = HexColor('#DC2626')        # Over-limit warning
CLR_REMARKS_HIGHLIGHT = HexColor('#FFF3CD')  # Light yellow highlight for remarks
CLR_WHITE = colors.white

# Typography sizes
FONT_TITLE = 13
FONT_SUBTITLE = 8
FONT_SECTION = 8.5
FONT_BODY = 7.5
FONT_BODY_SM = 7
FONT_FOOTER = 6.5
FONT_KPI = 9

# Spacing constants (in points)
SP_SECTION = 10        # Space before a new section
SP_AFTER_HEADER = 4    # Space after section header bar
SP_ROW_PAD_V = 3.5     # Vertical cell padding — data rows
SP_ROW_PAD_H = 5       # Horizontal cell padding
SP_HEADER_PAD_V = 5    # Vertical cell padding — header rows


"""
Finance Statement PDF Export - Customer Finance Summary
Optimized for landscape A4 with full monthly detail columns.
Uses logo from media/footer-logo.png or footer-logo1.png.
"""
import os
from io import BytesIO
from decimal import Decimal
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, Value, FloatField
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
    PageBreak, KeepTogether,
)

from so.models import Customer, Salesman


# ─────────────────────────────────────────────────────────────────────────────
# DESIGN CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Brand palette
CLR_PRIMARY      = HexColor('#1B2A4A')
CLR_PRIMARY_LT   = HexColor('#2C4A7C')
CLR_ACCENT       = HexColor('#D4912A')
CLR_ACCENT_LT    = HexColor('#F5E6CC')
CLR_BG_HEADER    = HexColor('#1B2A4A')
CLR_BG_TOTAL     = HexColor('#EBF0F7')
CLR_BG_ZEBRA     = HexColor('#F8F9FB')
CLR_BG_SECTION   = HexColor('#F3F4F6')
CLR_BORDER       = HexColor('#D1D5DB')
CLR_BORDER_HEAVY = HexColor('#9CA3AF')
CLR_TEXT         = HexColor('#1F2937')
CLR_TEXT_MUTED   = HexColor('#6B7280')
CLR_TEXT_FAINT   = HexColor('#9CA3AF')
CLR_DANGER       = HexColor('#DC2626')
CLR_REMARKS_HIGHLIGHT = HexColor('#FFF3CD')  # Light yellow highlight for remarks
CLR_SUCCESS      = HexColor('#059669')
CLR_WHITE        = colors.white

# ── Typography — TWO size tiers: normal (simple) and compact (detail) ──

# Simple mode (9 columns — plenty of room)
FONT_TITLE       = 13
FONT_SUBTITLE    = 8
FONT_SECTION     = 8.5
FONT_BODY        = 7.5
FONT_BODY_SM     = 7
FONT_KPI         = 9
FONT_FOOTER      = 6.5

# Compact mode (17 columns — every point matters)
FONT_C_HEADER    = 5.5      # Table header text
FONT_C_BODY      = 5.5      # Table body text
FONT_C_BODY_BOLD = 5.5      # Bold variant (same size, weight differs)

# Spacing
SP_SECTION       = 10
SP_AFTER_HEADER  = 4
SP_ROW_PAD_V     = 3.5      # Normal mode vertical padding
SP_ROW_PAD_H     = 5        # Normal mode horizontal padding
SP_HEADER_PAD_V  = 5

# Compact mode padding (detail view)
SP_C_PAD_V       = 2        # Tight vertical cell padding
SP_C_PAD_H       = 2.5      # Tight horizontal cell padding
SP_C_HDR_PAD_V   = 3.5      # Header row vertical padding


# ─────────────────────────────────────────────────────────────────────────────
# STYLE BUILDERS — two tiers
# ─────────────────────────────────────────────────────────────────────────────

def _build_styles():
    """Return a dict of ParagraphStyles for page-level elements and simple-mode table."""
    base = getSampleStyleSheet()['Normal']
    return {
        'title': ParagraphStyle(
            'PDFTitle', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_TITLE,
            textColor=CLR_PRIMARY, leading=FONT_TITLE + 3,
        ),
        'subtitle': ParagraphStyle(
            'PDFSubtitle', parent=base,
            fontName='Helvetica', fontSize=FONT_SUBTITLE,
            textColor=CLR_TEXT_MUTED, leading=FONT_SUBTITLE + 3,
        ),
        'section': ParagraphStyle(
            'PDFSection', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_SECTION,
            textColor=CLR_PRIMARY, leading=FONT_SECTION + 3,
        ),
        'label': ParagraphStyle(
            'PDFLabel', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY_SM,
            textColor=CLR_TEXT_MUTED, leading=FONT_BODY_SM + 3,
        ),
        # ── Simple-mode table styles ──
        'cell': ParagraphStyle(
            'PDFCell', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
        ),
        'cell_r': ParagraphStyle(
            'PDFCellR', parent=base,
            fontName='Helvetica', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'cell_bold': ParagraphStyle(
            'PDFCellBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
        ),
        'cell_bold_r': ParagraphStyle(
            'PDFCellBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_TEXT, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'header_cell': ParagraphStyle(
            'PDFHdrCell', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_WHITE, leading=FONT_BODY + 3,
        ),
        'header_cell_r': ParagraphStyle(
            'PDFHdrCellR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_WHITE, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        # ── KPI / footer ──
        'kpi_value': ParagraphStyle(
            'PDFKpiValue', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_KPI,
            textColor=CLR_PRIMARY, leading=FONT_KPI + 3,
        ),
        'kpi_label': ParagraphStyle(
            'PDFKpiLabel', parent=base,
            fontName='Helvetica', fontSize=FONT_FOOTER,
            textColor=CLR_TEXT_MUTED, leading=FONT_FOOTER + 2,
        ),
        'footer': ParagraphStyle(
            'PDFFooter', parent=base,
            fontName='Helvetica', fontSize=FONT_FOOTER,
            textColor=CLR_TEXT_MUTED, leading=FONT_FOOTER + 2,
            alignment=TA_CENTER,
        ),
        'danger_bold_r': ParagraphStyle(
            'PDFDangerBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_DANGER, leading=FONT_BODY + 3,
            alignment=TA_RIGHT,
        ),
        'remarks_highlight': ParagraphStyle(
            'PDFRemarksHighlight', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_BODY,
            textColor=CLR_DANGER, leading=FONT_BODY + 4,
            backColor=CLR_REMARKS_HIGHLIGHT,
            leftIndent=8, rightIndent=8,
            topPadding=6, bottomPadding=6,
        ),
    }


def _build_compact_styles():
    """
    Return a dict of COMPACT ParagraphStyles for the 17-column detail table.
    Smaller fonts, tighter leading — numbers never wrap.
    """
    base = getSampleStyleSheet()['Normal']
    return {
        # Header row
        'hdr': ParagraphStyle(
            'CHdr', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_HEADER,
            textColor=CLR_WHITE, leading=FONT_C_HEADER + 2,
        ),
        'hdr_r': ParagraphStyle(
            'CHdrR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_HEADER,
            textColor=CLR_WHITE, leading=FONT_C_HEADER + 2,
            alignment=TA_RIGHT,
        ),
        'hdr_c': ParagraphStyle(
            'CHdrC', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_HEADER,
            textColor=CLR_WHITE, leading=FONT_C_HEADER + 2,
            alignment=TA_CENTER,
        ),
        # Data cells
        'td': ParagraphStyle(
            'CTd', parent=base,
            fontName='Helvetica', fontSize=FONT_C_BODY,
            textColor=CLR_TEXT, leading=FONT_C_BODY + 2,
        ),
        'td_r': ParagraphStyle(
            'CTdR', parent=base,
            fontName='Helvetica', fontSize=FONT_C_BODY,
            textColor=CLR_TEXT, leading=FONT_C_BODY + 2,
            alignment=TA_RIGHT,
        ),
        'td_c': ParagraphStyle(
            'CTdC', parent=base,
            fontName='Helvetica', fontSize=FONT_C_BODY,
            textColor=CLR_TEXT, leading=FONT_C_BODY + 2,
            alignment=TA_CENTER,
        ),
        'td_bold': ParagraphStyle(
            'CTdBold', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_BODY_BOLD,
            textColor=CLR_TEXT, leading=FONT_C_BODY_BOLD + 2,
        ),
        'td_bold_r': ParagraphStyle(
            'CTdBoldR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_BODY_BOLD,
            textColor=CLR_TEXT, leading=FONT_C_BODY_BOLD + 2,
            alignment=TA_RIGHT,
        ),
        # Muted dash for zero
        'td_muted': ParagraphStyle(
            'CTdMuted', parent=base,
            fontName='Helvetica', fontSize=FONT_C_BODY,
            textColor=CLR_TEXT_FAINT, leading=FONT_C_BODY + 2,
            alignment=TA_RIGHT,
        ),
        # Danger (over-limit)
        'td_danger_r': ParagraphStyle(
            'CTdDangerR', parent=base,
            fontName='Helvetica-Bold', fontSize=FONT_C_BODY_BOLD,
            textColor=CLR_DANGER, leading=FONT_C_BODY_BOLD + 2,
            alignment=TA_RIGHT,
        ),
        # Label (for totals row count)
        'td_label': ParagraphStyle(
            'CTdLabel', parent=base,
            fontName='Helvetica-Oblique', fontSize=FONT_C_BODY,
            textColor=CLR_TEXT_MUTED, leading=FONT_C_BODY + 2,
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HELPER UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _get_logo():
    """Load company logo from media directory. Returns Image or None."""
    for name in ['footer-logo.png', 'footer-logo1.png']:
        path = os.path.join(settings.BASE_DIR, 'media', name)
        if os.path.exists(path):
            try:
                return Image(path, width=1.6 * inch, height=0.6 * inch)
            except Exception:
                pass
    return None


def _fmt(num):
    """Format number: integers without decimals, floats with 2 decimals."""
    if num is None:
        return "0"
    try:
        v = float(num)
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    except (TypeError, ValueError):
        return "0"


def _fmt_compact(num):
    """
    Compact number format for tight columns.
    - None/0 → "–" (en-dash, visually cleaner than "0")
    - Integers → no decimals, with comma grouping
    - Floats → 1 decimal max
    Keeps strings short to prevent line wrapping.
    """
    if num is None:
        return "–"
    try:
        v = float(num)
        if v == 0:
            return "–"
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.1f}"
    except (TypeError, ValueError):
        return "–"


def _build_document_header(styles, title_text, subtitle_text, page_width):
    """Build the top-of-page header: logo left, title right, accent line."""
    logo_img = _get_logo()

    title_block = Paragraph(f"<b>{title_text}</b>", styles['title'])
    subtitle_block = Paragraph(subtitle_text, styles['subtitle'])
    date_block = Paragraph(
        f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')}",
        styles['label'],
    )

    right_content = Table(
        [[title_block], [subtitle_block], [date_block]],
        colWidths=[page_width - 2.2 * inch],
    )
    right_content.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))

    if logo_img:
        row = [[logo_img, right_content]]
        widths = [2.0 * inch, page_width - 2.0 * inch]
    else:
        brand = Paragraph("<b>JUNAID</b>", ParagraphStyle(
            'Brand', fontName='Helvetica-Bold', fontSize=16, textColor=CLR_PRIMARY,
        ))
        row = [[brand, right_content]]
        widths = [2.0 * inch, page_width - 2.0 * inch]

    header_table = Table(row, colWidths=widths)
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))

    line_table = Table([['']], colWidths=[page_width])
    line_table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, CLR_ACCENT),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))

    return [header_table, Spacer(1, 4), line_table, Spacer(1, SP_SECTION)]


def _build_kpi_bar(kpi_items, styles, page_width):
    """Build a horizontal KPI summary bar."""
    num_items = len(kpi_items)
    cell_width = page_width / num_items

    kpi_cells = []
    for label, value in kpi_items:
        mini = Table(
            [
                [Paragraph(value, styles['kpi_value'])],
                [Paragraph(label, styles['kpi_label'])],
            ],
            colWidths=[cell_width - 8],
        )
        mini.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        kpi_cells.append(mini)

    bar = Table([kpi_cells], colWidths=[cell_width] * num_items)
    bar.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CLR_BG_SECTION),
        ('BOX', (0, 0), (-1, -1), 0.5, CLR_BORDER),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, CLR_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    return bar


def _build_section_header(text, styles, width):
    """Build a section header bar: accent strip + bold label."""
    t = Table(
        [['', Paragraph(f"<b>{text}</b>", styles['section'])]],
        colWidths=[3, width - 3],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), CLR_ACCENT),
        ('BACKGROUND', (1, 0), (1, 0), CLR_BG_SECTION),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (1, 0), (1, 0), 8),
    ]))
    return t


def _build_table_style_simple(num_rows):
    """Table style for simple mode (9 columns, comfortable spacing)."""
    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), CLR_BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), CLR_WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), FONT_BODY),
        ('BOX', (0, 0), (-1, -1), 0.75, CLR_BORDER_HEAVY),
        ('LINEBELOW', (0, 0), (-1, 0), 0.75, CLR_BORDER_HEAVY),
        ('TOPPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('LEFTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('RIGHTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('TOPPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]

    for i in range(1, num_rows - 1):
        if i % 2 == 0:
            cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_BG_ZEBRA))
        if i < num_rows - 1:
            cmds.append(('LINEBELOW', (0, i), (-1, i), 0.25, CLR_BORDER))

    # Total row
    cmds.extend([
        ('BACKGROUND', (0, -1), (-1, -1), CLR_BG_TOTAL),
        ('LINEABOVE', (0, -1), (-1, -1), 1.2, CLR_PRIMARY),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ])
    return TableStyle(cmds)


def _standard_data_table_style(num_rows, has_total_row=True):
    """
    General-purpose table style: header, zebra striping, optional total row.
    Used by finance statement detail PDF, credit edit list, and sap_purchaseorder_pdf_export.
    """
    cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), CLR_BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), CLR_WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), FONT_BODY),
        ('BOX', (0, 0), (-1, -1), 0.75, CLR_BORDER_HEAVY),
        ('LINEBELOW', (0, 0), (-1, 0), 0.75, CLR_BORDER_HEAVY),
        ('TOPPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, -1), SP_ROW_PAD_V),
        ('LEFTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('RIGHTPADDING', (0, 0), (-1, -1), SP_ROW_PAD_H),
        ('TOPPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, 0), SP_HEADER_PAD_V),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    last_data_idx = num_rows - 2 if has_total_row else num_rows - 1
    for i in range(1, last_data_idx + 1):
        if i % 2 == 0:
            cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_BG_ZEBRA))
        if i < num_rows - 1:
            cmds.append(('LINEBELOW', (0, i), (-1, i), 0.25, CLR_BORDER))
    if has_total_row and num_rows > 1:
        cmds.extend([
            ('BACKGROUND', (0, -1), (-1, -1), CLR_BG_TOTAL),
            ('LINEABOVE', (0, -1), (-1, -1), 1.2, CLR_PRIMARY),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ])
    return TableStyle(cmds)


def _build_table_style_compact(num_rows, date_col_start, date_col_end, summary_col_start):
    """
    Table style for compact detail mode (17 columns).
    Adds visual grouping: vertical separators between column groups.
    """
    cmds = [
        # ── Header row ──
        ('BACKGROUND', (0, 0), (-1, 0), CLR_BG_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, 0), CLR_WHITE),

        # ── Outer border ──
        ('BOX', (0, 0), (-1, -1), 0.75, CLR_BORDER_HEAVY),
        ('LINEBELOW', (0, 0), (-1, 0), 1, CLR_BORDER_HEAVY),

        # ── Compact cell padding — the key to fitting everything ──
        ('TOPPADDING', (0, 0), (-1, -1), SP_C_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, -1), SP_C_PAD_V),
        ('LEFTPADDING', (0, 0), (-1, -1), SP_C_PAD_H),
        ('RIGHTPADDING', (0, 0), (-1, -1), SP_C_PAD_H),

        # Header row gets slightly more vertical room
        ('TOPPADDING', (0, 0), (-1, 0), SP_C_HDR_PAD_V),
        ('BOTTOMPADDING', (0, 0), (-1, 0), SP_C_HDR_PAD_V),

        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),

        # ── Column group separators — visual clarity without full grid ──
        # Between info columns and date columns
        ('LINEAFTER', (date_col_start - 1, 0), (date_col_start - 1, -1), 0.75, CLR_BORDER_HEAVY),
        # Between date columns and summary columns
        ('LINEAFTER', (date_col_end, 0), (date_col_end, -1), 0.75, CLR_BORDER_HEAVY),
        # Between aging columns (6+ / 6++) and summary
        ('LINEAFTER', (summary_col_start - 1, 0), (summary_col_start - 1, -1), 0.75, CLR_BORDER_HEAVY),

        # ── Date column header tint — subtle differentiation ──
        ('BACKGROUND', (date_col_start, 0), (date_col_end, 0), HexColor('#2C4A7C')),
    ]

    # Zebra striping + subtle row lines
    for i in range(1, num_rows - 1):
        if i % 2 == 0:
            cmds.append(('BACKGROUND', (0, i), (-1, i), CLR_BG_ZEBRA))
        if i < num_rows - 1:
            cmds.append(('LINEBELOW', (0, i), (-1, i), 0.2, CLR_BORDER))

    # Total row emphasis
    cmds.extend([
        ('BACKGROUND', (0, -1), (-1, -1), CLR_BG_TOTAL),
        ('LINEABOVE', (0, -1), (-1, -1), 1.2, CLR_PRIMARY),
    ])

    return TableStyle(cmds)


def _build_page_footer(canvas, doc, styles_dict=None):
    """Draw a consistent footer on every page."""
    canvas.saveState()
    page_w, page_h = doc.pagesize

    y = 16
    canvas.setStrokeColor(CLR_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, y, page_w - doc.rightMargin, y)

    canvas.setFont('Helvetica', 6)
    canvas.setFillColor(CLR_TEXT_MUTED)
    canvas.drawCentredString(
        page_w / 2, 7,
        f"Page {doc.page}  •  Finance Statement  •  "
        f"Generated {datetime.now().strftime('%d %b %Y %H:%M')}  •  Confidential"
    )
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# VIEW: FINANCE STATEMENT LIST
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_finance_statement_list_pdf(request):
    """
    Export Finance Statement List to PDF - Landscape, compact layout.
    Supports ?detail=1 for full monthly breakdown (17 columns, no wrapping).
    Respects same filters as list view (q, salesman, store).
    """
    # ── Get filter parameters ──
    search_query = request.GET.get('q', '').strip()
    salesman_filter = request.GET.get('salesman', '').strip()
    store_filter = request.GET.get('store', '').strip()
    include_detail = request.GET.get('detail', '').strip().lower() in ('1', 'true', 'yes', 'on')

    customers = Customer.objects.filter(
        Q(total_outstanding__gt=0) | Q(pdc_received__gt=0)
    ).select_related('salesman')

    if search_query:
        customers = customers.filter(
            Q(customer_code__icontains=search_query) |
            Q(customer_name__icontains=search_query)
        )
    if salesman_filter:
        customers = customers.filter(salesman__id=salesman_filter)
    if store_filter == 'HO':
        customers = customers.filter(customer_code__startswith='HO')
    elif store_filter == 'Others':
        customers = customers.exclude(customer_code__startswith='HO')

    customers = customers.order_by('-total_outstanding_with_pdc', 'customer_name')

    # Monthly labels
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    monthly_labels = []
    for i in range(6):
        months_ago = 5 - i
        month_date = datetime.now().date() - timedelta(days=30 * months_ago)
        monthly_labels.append(month_names[month_date.month - 1])

    totals = customers.aggregate(
        total_outstanding=Coalesce(Sum('total_outstanding'), Value(0.0, output_field=FloatField())),
        total_pdc=Coalesce(Sum('pdc_received'), Value(0.0, output_field=FloatField())),
        total_with_pdc=Coalesce(Sum('total_outstanding_with_pdc'), Value(0.0, output_field=FloatField())),
        total_month_1=Coalesce(Sum('month_pending_1'), Value(0.0, output_field=FloatField())),
        total_month_2=Coalesce(Sum('month_pending_2'), Value(0.0, output_field=FloatField())),
        total_month_3=Coalesce(Sum('month_pending_3'), Value(0.0, output_field=FloatField())),
        total_month_4=Coalesce(Sum('month_pending_4'), Value(0.0, output_field=FloatField())),
        total_month_5=Coalesce(Sum('month_pending_5'), Value(0.0, output_field=FloatField())),
        total_month_6=Coalesce(Sum('month_pending_6'), Value(0.0, output_field=FloatField())),
        total_old_months=Coalesce(Sum('old_months_pending'), Value(0.0, output_field=FloatField())),
        total_very_old_months=Coalesce(Sum('very_old_months_pending'), Value(0.0, output_field=FloatField())),
    )

    # ── Build PDF ──
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_statement_list_'
        f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = landscape(A4)

    # ── Margins: tighter for detail mode to maximize usable width ──
    if include_detail:
        margin_h = 14          # ~0.19 inch — tight but print-safe
        margin_v = 20
    else:
        margin_h = 24          # Standard comfortable margins
        margin_v = 24

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 4,
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    # ── 1. Document Header ──
    elements.extend(_build_document_header(
        styles,
        title_text='FINANCE STATEMENT',
        subtitle_text='Customer Finance Summary & Outstanding Balances'
                      + (' — Detailed Monthly View' if include_detail else ''),
        page_width=usable_width,
    ))

    # ── 2. KPI Summary Bar ──
    kpi_items = [
        ('Total Outstanding', _fmt(totals['total_outstanding']) + ' AED'),
        ('PDC Received', _fmt(totals['total_pdc']) + ' AED'),
        ('Net Balance', _fmt(totals['total_with_pdc']) + ' AED'),
        ('Customers', str(customers.count())),
    ]
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    # ── 3. Active Filters note ──
    active_filters = []
    if search_query:
        active_filters.append(f'Search: "{search_query}"')
    if salesman_filter:
        active_filters.append(f'Salesman ID: {salesman_filter}')
    if store_filter:
        active_filters.append(f'Store: {store_filter}')
    if active_filters:
        filter_text = '  •  '.join(active_filters)
        elements.append(Paragraph(
            f'<font color="#6B7280">Filters: {filter_text}</font>',
            styles['label'],
        ))
        elements.append(Spacer(1, 4))

    # ════════════════════════════════════════════════════════════════
    # 4. DATA TABLE — DETAIL MODE (17 columns)
    # ════════════════════════════════════════════════════════════════

    if include_detail:
        cs = _build_compact_styles()

        # ── Column layout for 17 columns on landscape A4 ──
        # Usable width at 14pt margins: 842 - 28 = 814pt
        #
        # Group A: Info (3 cols - Code removed)
        #   #=18  Name=flex (includes space from Code)  Salesman=52
        # Group B: Monthly (6 cols) + Aging (2 cols)
        #   M1..M6 = 6×46  6+=40  6++=40
        # Group C: Summary (5 cols)
        #   Balance=54  PDC=48  Total=56  Limit=48  Terms=28
        #
        # Total fixed: 18+42+52 + 276+40+40 + 54+48+56+48+28 = 702
        # Name column: 814 - 702 = 112pt ≈ 1.56 inches (enough for ~25 chars at 5.5pt)

        W_NUM    = 18     # Row number
        W_CODE   = 42     # Customer code
        W_SALES  = 52     # Salesman (truncated)
        W_MONTH  = 46     # Each month column
        W_AGE    = 40     # 6+ and 6++ columns
        W_BAL    = 54     # Balance
        W_PDC    = 48     # PDC
        W_TOTAL  = 56     # Total outstanding
        W_LIMIT  = 48     # Credit limit
        W_TERMS  = 28     # Payment terms

        fixed_w = (W_NUM + W_SALES
                   + 6 * W_MONTH + 2 * W_AGE
                   + W_BAL + W_PDC + W_TOTAL + W_LIMIT + W_TERMS)
        W_NAME = max(100, usable_width - fixed_w)   # Absorb remainder (includes space from removed Code column)

        col_widths = [
            W_NUM, W_NAME, W_SALES,                                  # Info group (Code removed)
            W_MONTH, W_MONTH, W_MONTH, W_MONTH, W_MONTH, W_MONTH,   # M1–M6
            W_AGE, W_AGE,                                            # 6+, 6++
            W_BAL, W_PDC, W_TOTAL, W_LIMIT, W_TERMS,                # Summary
        ]

        # Column group indices (for vertical separators)
        DATE_COL_START = 3    # Changed from 4 (Code column removed)
        DATE_COL_END   = 8    # Last month column (changed from 9)
        AGING_COL_END  = 10   # Last aging column (changed from 11)
        SUMMARY_START  = 11   # Changed from 12

        # ── Header row ──
        hdr = [
            Paragraph('#', cs['hdr']),
            Paragraph('Customer Name', cs['hdr']),
            Paragraph('S/Man', cs['hdr']),
        ]
        for lbl in monthly_labels:
            hdr.append(Paragraph(lbl, cs['hdr_r']))
        hdr.extend([
            Paragraph('6+', cs['hdr_r']),
            Paragraph('6++', cs['hdr_r']),
            Paragraph('Balance', cs['hdr_r']),
            Paragraph('PDC', cs['hdr_r']),
            Paragraph('Total', cs['hdr_r']),
            Paragraph('Limit', cs['hdr_r']),
            Paragraph('Trm', cs['hdr_c']),
        ])
        table_data = [hdr]

        # ── Data rows ──
        month_fields = [
            'month_pending_1', 'month_pending_2', 'month_pending_3',
            'month_pending_4', 'month_pending_5', 'month_pending_6',
        ]

        for idx, c in enumerate(customers, start=1):
            s_name = c.salesman.salesman_name if c.salesman else '—'
            over_limit = (
                (c.total_outstanding_with_pdc or 0) > (c.credit_limit or 0)
                and (c.credit_limit or 0) > 0
            )

            row = [
                Paragraph(str(idx), cs['td_c']),
                Paragraph((c.customer_name or '—')[:40], cs['td']),  # Increased from 28 to 40 chars
                Paragraph(str(s_name)[:12], cs['td']),
            ]

            # Month columns — use compact format, muted style for zeros
            for f in month_fields:
                val = getattr(c, f, 0) or 0
                if val and float(val) > 0:
                    row.append(Paragraph(_fmt_compact(val), cs['td_r']))
                else:
                    row.append(Paragraph('–', cs['td_muted']))

            # Aging columns
            old_val = c.old_months_pending or 0
            very_old_val = getattr(c, 'very_old_months_pending', 0) or 0
            row.append(
                Paragraph(_fmt_compact(old_val), cs['td_r'])
                if old_val and float(old_val) > 0
                else Paragraph('–', cs['td_muted'])
            )
            row.append(
                Paragraph(_fmt_compact(very_old_val), cs['td_r'])
                if very_old_val and float(very_old_val) > 0
                else Paragraph('–', cs['td_muted'])
            )

            # Summary columns
            row.append(Paragraph(_fmt_compact(c.total_outstanding), cs['td_bold_r']))
            row.append(Paragraph(_fmt_compact(c.pdc_received), cs['td_r']))
            row.append(Paragraph(
                _fmt_compact(c.total_outstanding_with_pdc),
                cs['td_danger_r'] if over_limit else cs['td_bold_r'],
            ))
            row.append(Paragraph(_fmt_compact(c.credit_limit), cs['td_r']))
            row.append(Paragraph(str(c.credit_days or '—'), cs['td_c']))

            table_data.append(row)

        # ── Totals row ──
        totals_row = [
            Paragraph('', cs['td']),
            Paragraph('TOTAL', cs['td_bold']),
            Paragraph(f'{customers.count()} customers', cs['td_label']),
        ]
        month_total_keys = [
            'total_month_1', 'total_month_2', 'total_month_3',
            'total_month_4', 'total_month_5', 'total_month_6',
        ]
        for key in month_total_keys:
            totals_row.append(Paragraph(_fmt_compact(totals[key]), cs['td_bold_r']))
        totals_row.extend([
            Paragraph(_fmt_compact(totals['total_old_months']), cs['td_bold_r']),
            Paragraph(_fmt_compact(totals['total_very_old_months']), cs['td_bold_r']),
            Paragraph(_fmt_compact(totals['total_outstanding']), cs['td_bold_r']),
            Paragraph(_fmt_compact(totals['total_pdc']), cs['td_bold_r']),
            Paragraph(_fmt_compact(totals['total_with_pdc']), cs['td_bold_r']),
            Paragraph('', cs['td']),
            Paragraph('', cs['td']),
        ])
        table_data.append(totals_row)

        # ── Build table ──
        data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
        ts = _build_table_style_compact(
            num_rows=len(table_data),
            date_col_start=DATE_COL_START,
            date_col_end=AGING_COL_END,
            summary_col_start=SUMMARY_START,
        )
        # Right-align all numeric columns
        ts.add('ALIGN', (DATE_COL_START, 0), (-2, -1), 'RIGHT')
        data_table.setStyle(ts)
        elements.append(data_table)

    # ════════════════════════════════════════════════════════════════
    # 4b. DATA TABLE — SIMPLE MODE (9 columns)
    # ════════════════════════════════════════════════════════════════

    else:
        col_widths = [
            0.38 * inch,    # #
            0,              # Name (calculated below - includes space from removed Code)
            0.95 * inch,    # Salesman
            1.00 * inch,    # Balance
            0.85 * inch,    # PDC
            1.00 * inch,    # Total
            0.85 * inch,    # Limit
            0.50 * inch,    # Terms
        ]
        allocated = sum(col_widths)
        col_widths[1] = max(3.0 * inch, usable_width - allocated)  # Increased from 2.0 to 3.0 inches

        hdr = [
            Paragraph('#', styles['header_cell']),
            Paragraph('Customer Name', styles['header_cell']),
            Paragraph('Salesman', styles['header_cell']),
            Paragraph('Balance (AED)', styles['header_cell_r']),
            Paragraph('PDC (AED)', styles['header_cell_r']),
            Paragraph('Total (AED)', styles['header_cell_r']),
            Paragraph('Limit (AED)', styles['header_cell_r']),
            Paragraph('Terms', styles['header_cell_r']),
        ]
        table_data = [hdr]

        for idx, c in enumerate(customers, start=1):
            salesman_name = c.salesman.salesman_name if c.salesman else '—'
            over_limit = (
                (c.total_outstanding_with_pdc or 0) > (c.credit_limit or 0)
                and (c.credit_limit or 0) > 0
            )
            total_style = styles['danger_bold_r'] if over_limit else styles['cell_bold_r']

            table_data.append([
                Paragraph(str(idx), styles['cell']),
                Paragraph((c.customer_name or '—')[:60], styles['cell']),  # Increased from 48 to 60 chars
                Paragraph(str(salesman_name)[:22], styles['cell']),
                Paragraph(_fmt(c.total_outstanding), styles['cell_bold_r']),
                Paragraph(_fmt(c.pdc_received), styles['cell_r']),
                Paragraph(_fmt(c.total_outstanding_with_pdc), total_style),
                Paragraph(_fmt(c.credit_limit), styles['cell_r']),
                Paragraph(str(c.credit_days or '—'), styles['cell_r']),
            ])

        # Totals row
        table_data.append([
            Paragraph('', styles['cell']),
            Paragraph('<b>TOTAL</b>', styles['cell_bold']),
            Paragraph(f'<i>{customers.count()} customers</i>', styles['label']),
            Paragraph(_fmt(totals['total_outstanding']), styles['cell_bold_r']),
            Paragraph(_fmt(totals['total_pdc']), styles['cell_bold_r']),
            Paragraph(_fmt(totals['total_with_pdc']), styles['cell_bold_r']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
        ])

        data_table = Table(table_data, colWidths=col_widths, repeatRows=1)
        ts = _build_table_style_simple(len(table_data))
        ts.add('ALIGN', (3, 0), (7, -1), 'RIGHT')  # Changed from (4, 0), (8, -1) since Code column removed
        data_table.setStyle(ts)
        elements.append(data_table)

    # ── Build and return ──
    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response


# ─────────────────────────────────────────────────────────────────────────────
# VIEW 2: FINANCE STATEMENT DETAIL (portrait, single customer)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def export_finance_statement_detail_pdf(request, customer_id):
    """
    Export Finance Statement Detail (single customer) to PDF.
    Structure: logo, title, customer info, tables, summary.
    """
    customer = get_object_or_404(
        Customer.objects.select_related('salesman'), id=customer_id
    )

    # ── Compute data (business logic unchanged) ──
    from calendar import monthrange
    today = datetime.now().date()
    month_names = [
        'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
    ]
    monthly_data = []
    month_amounts = [
        customer.month_pending_1, customer.month_pending_2,
        customer.month_pending_3, customer.month_pending_4,
        customer.month_pending_5, customer.month_pending_6,
    ]
    
    # Generate month labels with date ranges
    for i in range(6):
        months_ago = 5 - i
        # Calculate the actual month date (first day of that month)
        if months_ago == 0:
            month_start = today.replace(day=1)
        else:
            year = today.year
            month = today.month - months_ago
            while month <= 0:
                month += 12
                year -= 1
            month_start = datetime(year, month, 1).date()
        
        # Get last day of that month
        last_day = monthrange(month_start.year, month_start.month)[1]
        month_end = datetime(month_start.year, month_start.month, last_day).date()
        
        # Format dates as DD-MM-YY
        start_str = month_start.strftime('%d-%m-%y')
        end_str = month_end.strftime('%d-%m-%y')
        month_name = month_names[month_start.month - 1]
        
        monthly_data.append({
            'month': f"{month_name} {month_start.year}",
            'month_name': month_name,
            'date_range': f"({start_str} to {end_str})",
            'start_date': month_start,
            'end_date': month_end,
            'amount': month_amounts[i] or 0,
        })
    
    # Calculate 6+ months end date (end of month before Month 1)
    if monthly_data:
        # Month 1 is 5 months ago
        months_ago = 5
        year = today.year
        month = today.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        month_1_start = datetime(year, month, 1).date()
        
        # Go back one month from Month 1 start
        year = month_1_start.year
        month = month_1_start.month - 1
        if month <= 0:
            month = 12
            year -= 1
        last_day_6plus = monthrange(year, month)[1]
        six_plus_end = datetime(year, month, last_day_6plus).date()
        six_plus_end_str = six_plus_end.strftime('%d-%m-%y')
    else:
        six_plus_end_str = ""

    total_monthly = sum(m['amount'] for m in monthly_data)
    total_outstanding = customer.total_outstanding or 0
    pdc_received = customer.pdc_received or 0
    total_with_pdc = customer.total_outstanding_with_pdc or 0
    old_months = customer.old_months_pending or 0
    very_old_months = getattr(customer, 'very_old_months_pending', 0) or 0
    
    # Calculate 90+ and 120+ aging buckets
    # 90+ = months 1, 2 (before last 4 months, so if current is Feb, till Oct 31)
    # 120+ = month 1 only (before last 5 months, so if current is Feb, till Sep 30)
    pending_90_plus = sum(month_amounts[i] for i in [0, 1])  # months 1, 2
    pending_120_plus = month_amounts[0]  # month 1 only
    
    # Calculate end dates for 90+ and 120+
    if monthly_data:
        # 90+ ends at end of month 2
        month_2_end = monthly_data[1]['end_date']
        end_90_str = month_2_end.strftime('%d-%m-%y')
        
        # 120+ ends at end of month 1
        month_1_end = monthly_data[0]['end_date']
        end_120_str = month_1_end.strftime('%d-%m-%y')
    else:
        end_90_str = ""
        end_120_str = ""
    
    credit_limit = customer.credit_limit or 0
    has_over_limit = total_with_pdc > credit_limit and credit_limit > 0

    # ── Build PDF ──
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_statement_'
        f'{customer.customer_code}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = A4
    margin_h, margin_v = 32, 30

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 6,
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    # ── 1. Document Header ──
    elements.extend(_build_document_header(
        styles,
        title_text='FINANCE STATEMENT',
        subtitle_text='Customer Finance Details',
        page_width=usable_width,
    ))

    # ── 1b. Internal Remarks (top of PDF when requested) ──
    include_internal_remarks = request.GET.get('include_internal_remarks', '').strip().lower() in ('1', 'true', 'yes', 'on')
    internal_remarks_text = getattr(customer, 'internal_remarks', None) if hasattr(customer, 'internal_remarks') else None
    if include_internal_remarks and internal_remarks_text:
        elements.append(_build_section_header('Remarks From MD:', styles, usable_width))
        elements.append(Spacer(1, SP_AFTER_HEADER))
        safe_text = html_module.escape(internal_remarks_text).replace('\n', '<br/>')
        # Create highlighted remarks box with red bold text
        remarks_para = Paragraph(
            f'<font color="#DC2626"><b>{safe_text}</b></font>',
            styles['remarks_highlight']
        )
        # Wrap in table for background highlight
        remarks_table = Table(
            [[remarks_para]],
            colWidths=[usable_width],
        )
        remarks_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), CLR_REMARKS_HIGHLIGHT),
            ('BOX', (0, 0), (-1, -1), 1.5, CLR_DANGER),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(remarks_table)
        elements.append(Spacer(1, SP_SECTION))

    # ── 2. Customer Information Section ──
    elements.append(_build_section_header('Customer Information', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    # Two-column info grid
    info_col_widths = [1.3 * inch, 2.0 * inch, 1.3 * inch, 2.0 * inch]
    # Adjust to fill usable width
    info_allocated = sum(info_col_widths)
    if usable_width > info_allocated:
        extra = (usable_width - info_allocated) / 2
        info_col_widths[1] += extra
        info_col_widths[3] += extra

    info_data = [
        [
            Paragraph('Customer Code', styles['label']),
            Paragraph(customer.customer_code or '—', styles['cell_bold']),
            Paragraph('Customer Name', styles['label']),
            Paragraph((customer.customer_name or '—')[:40], styles['cell_bold']),
        ],
        [
            Paragraph('Salesman', styles['label']),
            Paragraph(
                (customer.salesman.salesman_name if customer.salesman else '—')[:28],
                styles['cell'],
            ),
            Paragraph('Credit Limit', styles['label']),
            Paragraph(_fmt(credit_limit) + ' AED', styles['cell_bold']),
        ],
        [
            Paragraph('Payment Terms', styles['label']),
            Paragraph(str(customer.credit_days or '—') + ' days', styles['cell']),
            Paragraph('Status', styles['label']),
            Paragraph(
                '<font color="#D97706"><b>Limit not set</b></font>' if credit_limit <= 0
                else (
                    '<font color="#DC2626"><b>OVER LIMIT</b></font>' if has_over_limit
                    else '<font color="#059669"><b>Within Limit</b></font>'
                ),
                styles['cell'],
            ),
        ],
    ]
    info_table = Table(info_data, colWidths=info_col_widths)
    info_table.setStyle(TableStyle([
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, CLR_BORDER),  # Subtle row separators
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, SP_SECTION))

    # ── 3. Outstanding Summary KPI Bar ──
    elements.append(_build_section_header('Outstanding Summary', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    outstanding_kpis = [
        ('Balance Due', _fmt(total_outstanding) + ' AED'),
        ('PDC Received in Hand', _fmt(pdc_received) + ' AED'),
        ('Net Outstanding', _fmt(total_with_pdc) + ' AED'),
    ]
    elements.append(_build_kpi_bar(outstanding_kpis, styles, usable_width * 0.75))
    elements.append(Spacer(1, SP_SECTION))

    # ── 4. Monthly Pending Table ──
    elements.append(_build_section_header('Monthly Pending (Last 6 Months)', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    month_col_widths = [2.2 * inch, 1.6 * inch]
    month_rows = [
        [
            Paragraph('Month', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ]
    ]
    for m in monthly_data:
        amt = m['amount']
        # Highlight non-zero amounts
        amt_style = styles['cell_bold_r'] if amt > 0 else styles['cell_r']
        month_rows.append([
            Paragraph(f"{m['month_name']} {m['date_range']}", styles['cell']),
            Paragraph(_fmt(amt), amt_style),
        ])
    month_rows.append([
        Paragraph('<b>Subtotal (6 months)</b>', styles['cell_bold']),
        Paragraph(_fmt(total_monthly), styles['cell_bold_r']),
    ])

    month_table = Table(month_rows, colWidths=month_col_widths, repeatRows=1)
    ts_month = _standard_data_table_style(len(month_rows), has_total_row=True)
    ts_month.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    month_table.setStyle(ts_month)
    elements.append(month_table)
    elements.append(Spacer(1, SP_SECTION))

    # ── 5. Aged Pending Section ──
    elements.append(_build_section_header('Aged Pending', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    aged_col_widths = [2.8 * inch, 1.6 * inch]
    aged_rows = [
        [
            Paragraph('Aging Bucket', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ],
        [
            Paragraph(f'90+ Days Pending (till {end_90_str})', styles['cell']),
            Paragraph(
                _fmt(pending_90_plus),
                styles['cell_bold_r'] if pending_90_plus > 0 else styles['cell_r'],
            ),
        ],
        [
            Paragraph(f'120+ Days Pending (till {end_120_str})', styles['cell']),
            Paragraph(
                _fmt(pending_120_plus),
                styles['cell_bold_r'] if pending_120_plus > 0 else styles['cell_r'],
            ),
        ],
        [
            Paragraph(f'180+ Days Pending (6+ months, till {six_plus_end_str})', styles['cell']),
            Paragraph(
                _fmt(old_months),
                styles['cell_bold_r'] if old_months > 0 else styles['cell_r'],
            ),
        ],
        [
            Paragraph('360+ Days Pending (6++ months)', styles['cell']),
            Paragraph(
                _fmt(very_old_months),
                styles['cell_bold_r'] if very_old_months > 0 else styles['cell_r'],
            ),
        ],
    ]
    aged_table = Table(aged_rows, colWidths=aged_col_widths)
    ts_aged = _standard_data_table_style(len(aged_rows), has_total_row=False)
    ts_aged.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    aged_table.setStyle(ts_aged)
    elements.append(aged_table)
    elements.append(Spacer(1, SP_SECTION + 4))

    # ── 6. Grand Total Summary ──
    elements.append(_build_section_header('Total Summary', styles, usable_width))
    elements.append(Spacer(1, SP_AFTER_HEADER))

    # Determine the style for the final total
    if has_over_limit:
        final_total_style = ParagraphStyle(
            'FinalTotalDanger', parent=styles['cell_bold_r'],
            fontSize=FONT_KPI, textColor=CLR_DANGER,
        )
    else:
        final_total_style = ParagraphStyle(
            'FinalTotal', parent=styles['cell_bold_r'],
            fontSize=FONT_KPI, textColor=CLR_PRIMARY,
        )

    summary_col_widths = [2.8 * inch, 2.0 * inch]
    summary_rows = [
        [
            Paragraph('Description', styles['header_cell']),
            Paragraph('Amount (AED)', styles['header_cell_r']),
        ],
        [
            Paragraph('Last 6 Months Total', styles['cell']),
            Paragraph(_fmt(total_monthly), styles['cell_bold_r']),
        ],
        [
            Paragraph(f'90+ Days Pending (till {end_90_str})', styles['cell']),
            Paragraph(_fmt(pending_90_plus), styles['cell_r']),
        ],
        [
            Paragraph(f'120+ Days Pending (till {end_120_str})', styles['cell']),
            Paragraph(_fmt(pending_120_plus), styles['cell_r']),
        ],
        [
            Paragraph(f'180+ Days Pending (till {six_plus_end_str})', styles['cell']),
            Paragraph(_fmt(old_months), styles['cell_r']),
        ],
        [
            Paragraph('360+ Days Pending', styles['cell']),
            Paragraph(_fmt(very_old_months), styles['cell_r']),
        ],
        [
            Paragraph('<b>Total Outstanding</b>', styles['cell_bold']),
            Paragraph(_fmt(total_outstanding), styles['cell_bold_r']),
        ],
        [
            Paragraph('<b>PDC Received in Hand</b>', styles['cell_bold']),
            Paragraph(
                _fmt(pdc_received),
                ParagraphStyle(
                    'PDCDeduct', parent=styles['cell_bold_r'],
                    textColor=HexColor('#059669'),
                ),
            ),
        ],
        [
            Paragraph('<b>Net Outstanding (with PDC)</b>', styles['cell_bold']),
            Paragraph(_fmt(total_with_pdc), final_total_style),
        ],
    ]

    summary_table = Table(summary_rows, colWidths=summary_col_widths, repeatRows=1)

    # Custom styling for the summary table
    ts_summary = _standard_data_table_style(len(summary_rows), has_total_row=True)
    ts_summary.add('ALIGN', (1, 0), (1, -1), 'RIGHT')
    # Extra emphasis on the penultimate separator (before Total Outstanding)
    ts_summary.add('LINEABOVE', (0, -3), (-1, -3), 0.75, CLR_PRIMARY_LIGHT)
    # Double line above final row
    ts_summary.add('LINEABOVE', (0, -1), (-1, -1), 1.5, CLR_PRIMARY)
    # Larger padding on final row
    ts_summary.add('TOPPADDING', (0, -1), (-1, -1), 8)
    ts_summary.add('BOTTOMPADDING', (0, -1), (-1, -1), 8)

    summary_table.setStyle(ts_summary)
    elements.append(summary_table)

    # ── Build and return ──
    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response


@login_required
def export_finance_credit_edit_list_pdf(request):
    """
    Export consolidated manager credit edits to PDF using date range filter.
    """
    if request.user.username != 'manager':
        return HttpResponseForbidden("Only manager can export credit edit list.")

    today = datetime.now().date()
    from_date_str = request.GET.get('from_date', today.strftime('%Y-%m-%d'))
    to_date_str = request.GET.get('to_date', today.strftime('%Y-%m-%d'))

    try:
        from_date = datetime.strptime(from_date_str, '%Y-%m-%d').date()
    except ValueError:
        from_date = today
        from_date_str = today.strftime('%Y-%m-%d')

    try:
        to_date = datetime.strptime(to_date_str, '%Y-%m-%d').date()
    except ValueError:
        to_date = today
        to_date_str = today.strftime('%Y-%m-%d')

    if from_date > to_date:
        from_date, to_date = to_date, from_date
        from_date_str, to_date_str = to_date_str, from_date_str

    filtered_edits = FinanceCreditEditLog.objects.filter(
        created_at__date__gte=from_date,
        created_at__date__lte=to_date
    )
    latest_edit_ids = (
        filtered_edits
        .values('customer_id')
        .annotate(latest_id=Max('id'))
        .values_list('latest_id', flat=True)
    )
    edits = (
        FinanceCreditEditLog.objects
        .filter(id__in=latest_edit_ids)
        .select_related('customer__salesman', 'edited_by')
        .order_by('-created_at')
    )

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = (
        f'attachment; filename="finance_credit_edits_'
        f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf"'
    )

    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    margin_h, margin_v = 24, 24
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v + 6,
    )

    usable_width = page_w - 2 * margin_h
    styles = _build_styles()
    elements = []

    subtitle = f"Manager credit edits from {from_date_str} to {to_date_str}"
    elements.extend(_build_document_header(
        styles,
        title_text='CREDIT EDIT CONSOLIDATED REPORT',
        subtitle_text=subtitle,
        page_width=usable_width,
    ))

    kpi_items = [
        ('Total Edits', str(edits.count())),
        ('From Date', from_date_str),
        ('To Date', to_date_str),
        ('Prepared By', request.user.username),
    ]
    elements.append(_build_kpi_bar(kpi_items, styles, usable_width))
    elements.append(Spacer(1, SP_SECTION))

    col_widths = [
        0.35 * inch,
        1.00 * inch,
        2.30 * inch,
        1.00 * inch,
        1.20 * inch,
        1.00 * inch,
        0.90 * inch,
        2.20 * inch,
    ]
    allocated = sum(col_widths)
    remainder = max(0, usable_width - allocated)
    col_widths[7] += remainder

    table_data = [[
        Paragraph('#', styles['header_cell']),
        Paragraph('Code', styles['header_cell']),
        Paragraph('Customer', styles['header_cell']),
        Paragraph('Salesman', styles['header_cell']),
        Paragraph('Edited Limit', styles['header_cell_r']),
        Paragraph('Terms', styles['header_cell_r']),
        Paragraph('Edited By', styles['header_cell']),
        Paragraph('Edited At / Remarks', styles['header_cell']),
    ]]

    for idx, edit in enumerate(edits, start=1):
        salesman_name = (
            edit.customer.salesman.salesman_name
            if edit.customer and edit.customer.salesman else '—'
        )
        edit_by = edit.edited_by.username if edit.edited_by else '—'
        date_str = edit.created_at.strftime('%d %b %Y %H:%M')
        if edit.remarks:
            # Highlight remarks in red and bold
            safe_remarks = html_module.escape(edit.remarks)
            notes_para = Paragraph(
                f'{date_str} | <font color="#DC2626"><b>{safe_remarks}</b></font>',
                styles['cell']
            )
        else:
            notes_para = Paragraph(date_str, styles['cell'])

        table_data.append([
            Paragraph(str(idx), styles['cell']),
            Paragraph(edit.customer.customer_code or '—', styles['cell']),
            Paragraph(edit.customer.customer_name or '—', styles['cell']),
            Paragraph(str(salesman_name)[:20], styles['cell']),
            Paragraph(_fmt(edit.edited_credit_limit), styles['cell_r']),
            Paragraph(str(edit.edited_credit_days or '—'), styles['cell_r']),
            Paragraph(edit_by, styles['cell']),
            notes_para,
        ])

    if not edits.exists():
        table_data.append([
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('No edits found for selected date range.', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
            Paragraph('', styles['cell']),
        ])

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table_style = _standard_data_table_style(len(table_data), has_total_row=False)
    table_style.add('ALIGN', (4, 0), (5, -1), 'RIGHT')
    table.setStyle(table_style)
    elements.append(table)

    doc.build(elements, onFirstPage=_build_page_footer, onLaterPages=_build_page_footer)
    response.write(buffer.getvalue())
    return response