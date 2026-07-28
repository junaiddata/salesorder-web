"""
Warranty Letter PDF Builder.

Builds a single-page (typically) formal warranty letter with ReportLab:
Ref No/Date, a centered title, a Project/Client/Consultant/Main Contractor
label block, an intro sentence, an items table ending in "END OF LIST",
warranty terms as a bullet list, and a sign-off block with the signature
and stamp images placed inline once uploaded. Letterhead (header logo,
footer banner, DRAFT watermark while not yet Approved) is drawn on every
page via a PageTemplate onPage callback, mirroring the pattern used for
the Submittal app's internal warranty letter.
"""
import os
from io import BytesIO

import numpy as np
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem, Image,
)

from .models import WarrantyLetterSettings, default_item_columns

PAGE_W, PAGE_H = A4

HEADER_LOGO_BOX = (160, 55)   # (max width, max height)
FOOTER_IMAGE_TARGET_W = PAGE_W - 60   # span (almost) the full page width
FOOTER_IMAGE_MAX_H = 110              # cap so a very tall banner can't eat into body content


def _trim_side_borders(im, max_trim=10, threshold=195):
    """Trim a thin border/edge-artifact line from an image's left and
    right sides (e.g. a faint box outline baked in by whatever tool
    produced the graphic), so it doesn't render as a stray vertical line
    once the image is stretched across the page. Only trims a column when
    every pixel in it is light -- real dark content (badge ink, text)
    stops the trim immediately, so this can't eat into actual artwork."""
    arr = np.array(im.convert('RGB'))
    w = arr.shape[1]

    def _is_light(col):
        return col.size == 0 or col.min() >= threshold

    left, right = 0, w
    for _ in range(max_trim):
        if left < right - 1 and _is_light(arr[:, left]):
            left += 1
        else:
            break
    for _ in range(max_trim):
        if right > left + 1 and _is_light(arr[:, right - 1]):
            right -= 1
        else:
            break
    if left == 0 and right == w:
        return im
    return im.crop((left, 0, right, im.height))


def _prepare_footer_image(path, target_w, max_h):
    """Load the footer banner, trim any stray border lines from its left
    and right edges, and compute the (buffer, width, height) to draw it
    at so it spans target_w edge to edge while preserving its own aspect
    ratio (capped at max_h so it can't grow into the letter body).
    reportlab's drawImage(preserveAspectRatio=True) fits *within* a given
    w AND h box, picking whichever dimension is tighter -- for a
    banner-shaped image inside a much wider/shorter box that means it
    gets fit to the height and ends up far narrower than the page instead
    of spanning it edge to edge, so the height here is computed from the
    image's real aspect ratio rather than left to reportlab."""
    with PILImage.open(path) as im:
        trimmed = _trim_side_borders(im)
        iw, ih = trimmed.size
        if not iw or not ih:
            draw_w, draw_h = target_w, max_h or target_w
        else:
            draw_h = target_w * (ih / iw)
            if max_h and draw_h > max_h:
                draw_w, draw_h = max_h * (iw / ih), max_h
            else:
                draw_w = target_w
        buf = BytesIO()
        trimmed.save(buf, format='PNG')
        buf.seek(0)
        return buf, draw_w, draw_h


def _strip_light_background(im, threshold=225, feather=40):
    """Turn a near-white/paper-colored background transparent, so a
    scanned or photographed signature overlays cleanly onto the page
    instead of showing a visible rectangle. Dark ink strokes stay opaque;
    pixels between (threshold-feather) and threshold fade smoothly so
    anti-aliased edges don't look jagged."""
    arr = np.array(im.convert('RGBA')).astype(np.float32)
    luminance = arr[..., :3].mean(axis=2)
    factor = np.clip((threshold - luminance) / feather, 0.0, 1.0)
    arr[..., 3] *= factor
    return PILImage.fromarray(arr.astype('uint8'), 'RGBA')


def _crop_to_content(im):
    """Crop an RGBA image to the bounding box of its non-transparent
    pixels. A scanned signature often has a lot of blank paper margin
    around the actual ink; without trimming that first, scaling the whole
    canvas to fit its box leaves the visible signature tiny and shoved
    into a corner instead of filling (and overlapping the stamp within)
    its allotted space."""
    bbox = im.getchannel('A').getbbox()
    return im.crop(bbox) if bbox else im


def _fit_image_flowable(path, max_w, max_h, strip_background=False):
    """Return a reportlab Image flowable scaled to fit within max_w/max_h,
    preserving aspect ratio (platypus Image doesn't do this on its own).
    strip_background=True clears a near-white paper background to
    transparent and crops to the remaining ink, for scanned signatures
    that weren't pre-cleaned."""
    im = PILImage.open(path)

    if strip_background:
        cleaned = _crop_to_content(_strip_light_background(im))
        im.close()
        iw, ih = cleaned.size
        scale = min(max_w / iw, max_h / ih) if iw and ih else 1.0
        buf = BytesIO()
        cleaned.save(buf, format='PNG')
        buf.seek(0)
        return Image(buf, width=iw * scale, height=ih * scale)

    iw, ih = im.size
    scale = min(max_w / iw, max_h / ih) if iw and ih else 1.0
    im.close()
    return Image(path, width=iw * scale, height=ih * scale)


def _compose_signature_and_stamp(sig_path, stamp_path, box_w=170, box_h=88, render_scale=4):
    """Layer the signature on top of the stamp into a single image, the way
    they'd overlap when actually signed and stamped on a printed letter --
    stamp sitting right-of-center, signature crossing over it from the
    upper-left, both anchored as one block directly above the signatory's
    name. Returns a reportlab Image flowable sized box_w x box_h (pt)."""
    px_w, px_h = int(box_w * render_scale), int(box_h * render_scale)
    canvas_im = PILImage.new('RGBA', (px_w, px_h), (0, 0, 0, 0))

    def _load_scaled(path, max_w, max_h, strip_bg):
        im = PILImage.open(path)
        im = _crop_to_content(_strip_light_background(im)) if strip_bg else im.convert('RGBA')
        iw, ih = im.size
        scale = min(max_w / iw, max_h / ih) if iw and ih else 1.0
        return im.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), PILImage.LANCZOS)

    stamp_im = _load_scaled(stamp_path, px_w * 0.62, px_h * 0.95, strip_bg=False)
    stamp_x0 = px_w - stamp_im.width - int(px_w * 0.03)
    canvas_im.alpha_composite(stamp_im, (stamp_x0, (px_h - stamp_im.height) // 2))

    # Scale the signature generously, then anchor it by its RIGHT edge so
    # it crosses a fixed distance into the stamp regardless of the
    # signature's own aspect ratio -- anchoring at a fixed x=0 left a gap
    # whenever a (typically landscape) signature wasn't wide enough on its
    # own to reach the right-anchored stamp.
    sig_im = _load_scaled(sig_path, px_w * 0.85, px_h * 0.8, strip_bg=True)
    overlap = int(stamp_im.width * 0.3)
    sig_x0 = max(0, stamp_x0 + overlap - sig_im.width)
    sig_y0 = max(0, (px_h - sig_im.height) // 2 - int(px_h * 0.08))
    canvas_im.alpha_composite(sig_im, (sig_x0, sig_y0))

    buf = BytesIO()
    canvas_im.save(buf, format='PNG')
    buf.seek(0)
    return Image(buf, width=box_w, height=box_h)


class _WarrantyLetterDocTemplate(BaseDocTemplate):
    """Doc template that draws the header logo and footer banner on every
    page once the letter is Approved (kept off the Draft/Pending/Rejected
    preview so it doesn't look like a finished, letterhead-branded letter
    before it actually is one), and a DRAFT watermark until then."""

    def __init__(self, buf, company='junaid', status='Draft', **kwargs):
        self._settings = WarrantyLetterSettings.get_instance(company)
        self._status = status
        super().__init__(buf, **kwargs)
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id='normal')
        template = PageTemplate(id='warranty', frames=[frame], onPage=self._draw_letterhead)
        self.addPageTemplates([template])

    def _draw_letterhead(self, c, doc):
        c.saveState()

        if self._status == 'Approved':
            if self._settings.header_logo and os.path.exists(self._settings.header_logo.path):
                logo_w, logo_h = HEADER_LOGO_BOX
                c.drawImage(
                    self._settings.header_logo.path,
                    PAGE_W - 40 - logo_w, PAGE_H - 40 - logo_h,
                    width=logo_w, height=logo_h,
                    preserveAspectRatio=True, anchor='n', mask='auto',
                )

            if self._settings.footer_image and os.path.exists(self._settings.footer_image.path):
                footer_buf, footer_w, footer_h = _prepare_footer_image(
                    self._settings.footer_image.path, FOOTER_IMAGE_TARGET_W, FOOTER_IMAGE_MAX_H,
                )
                c.drawImage(
                    ImageReader(footer_buf),
                    (PAGE_W - footer_w) / 2, 15,
                    width=footer_w, height=footer_h,
                    preserveAspectRatio=True, anchor='s', mask='auto',
                )
        else:
            c.setFont('Helvetica-Bold', 72)
            c.setFillColor(colors.Color(0.75, 0.75, 0.75, alpha=0.35))
            c.translate(PAGE_W / 2, PAGE_H / 2)
            c.rotate(45)
            c.drawCentredString(0, 0, 'DRAFT')

        c.restoreState()


def build_warranty_letter_pdf(letter) -> BytesIO:
    buf = BytesIO()
    settings_row = WarrantyLetterSettings.get_instance(letter.company)
    doc = _WarrantyLetterDocTemplate(
        buf,
        company=letter.company,
        status=letter.status,
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=90,     # room for header logo
        bottomMargin=15 + FOOTER_IMAGE_MAX_H + 10,  # room for footer banner at its tallest, plus a gap
    )

    style_body = ParagraphStyle('WLBody', fontSize=10, fontName='Helvetica', leading=14, spaceAfter=4)
    style_body_bold = ParagraphStyle('WLBodyBold', parent=style_body, fontName='Helvetica-Bold')
    style_title = ParagraphStyle(
        'WLTitle', fontSize=13, fontName='Helvetica-Bold',
        alignment=TA_CENTER, spaceBefore=6, spaceAfter=14, textColor=colors.black,
    )
    style_label = ParagraphStyle('WLLabel', fontSize=10, fontName='Helvetica-Bold', leading=14)
    style_label_val = ParagraphStyle('WLLabelVal', fontSize=10, fontName='Helvetica', leading=14)
    style_cell = ParagraphStyle('WLCell', fontSize=9, fontName='Helvetica', leading=11)
    style_header = ParagraphStyle('WLHeader', fontSize=9, fontName='Helvetica-Bold', leading=11, textColor=colors.black)

    def _lbl_row(label, value):
        return [Paragraph(label, style_label), Paragraph(':', style_label), Paragraph(value, style_label_val)]

    lbl_w, col_w = 120, 10
    val_w = doc.width - lbl_w - col_w
    label_rows = [
        _lbl_row(label, value) for label, value in letter.ordered_project_fields() if value
    ]
    label_tbl = Table(label_rows, colWidths=[lbl_w, col_w, val_w]) if label_rows else None
    if label_tbl:
        label_tbl.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))

    # ── Items table -- columns are freely customizable per letter ───────
    columns = letter.item_columns or default_item_columns()
    ncols = len(columns)
    headers = [c.get('label', '') for c in columns]
    # Distribute the full table width evenly across however many columns
    # the user configured, same "even split with a floor" approach used for
    # the submittal app's dynamic materials table.
    available_w = doc.width
    col_widths = [max(40, available_w // ncols)] * ncols
    col_widths[-1] += available_w - sum(col_widths)  # absorb integer-division remainder

    table_data = [[Paragraph(h, style_header) for h in headers]]
    for item in letter.items.all():
        table_data.append([Paragraph(item.get(c['key'], ''), style_cell) for c in columns])
    end_row_idx = len(table_data)
    table_data.append([Paragraph('END OF LIST', style_header)] + [''] * (ncols - 1))

    items_tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ('SPAN', (0, end_row_idx), (-1, end_row_idx)),
        ('ALIGN', (0, end_row_idx), (-1, end_row_idx), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    # ── Warranty terms bullets ───────────────────────────────────────────
    terms_lines = [ln.strip() for ln in (letter.terms_text or '').splitlines() if ln.strip()]
    terms_list = ListFlowable(
        [ListItem(Paragraph(line, style_body), leftIndent=6) for line in terms_lines],
        bulletType='bullet', start='circle', leftIndent=14, bulletFontSize=6,
    ) if terms_lines else None

    # ── Sign-off block ────────────────────────────────────────────────
    # Signature and stamp overlap into one block (signature crossing over
    # the stamp, like ink signed across an already-stamped page), sitting
    # directly above the name -- rather than sitting in separate columns.
    # Each falls back to the company-wide default so an Approved letter
    # auto-carries a signature/stamp without a manual per-letter upload;
    # neither is drawn before Approved, even if a default is configured.
    sig_field = letter.effective_signature_image() if letter.status == 'Approved' else None
    stamp_field = letter.effective_stamp_image() if letter.status == 'Approved' else None
    has_sig = bool(sig_field and os.path.exists(sig_field.path))
    has_stamp = bool(stamp_field and os.path.exists(stamp_field.path))

    sign_stamp_flowable = None
    if has_sig and has_stamp:
        sign_stamp_flowable = _compose_signature_and_stamp(
            sig_field.path, stamp_field.path,
        )
    elif has_sig:
        sign_stamp_flowable = _fit_image_flowable(sig_field.path, 130, 50, strip_background=True)
    elif has_stamp:
        sign_stamp_flowable = _fit_image_flowable(stamp_field.path, 90, 90)
    if sign_stamp_flowable:
        # Image flowables default to hAlign='CENTER'; this block sits
        # directly in the page flow (not inside a table cell), so without
        # this it drifts to the middle of the page instead of the left
        # margin where the name/title below it starts.
        sign_stamp_flowable.hAlign = 'LEFT'

    name_title_lines = []
    if letter.signatory_name:
        name_title_lines.append(f'<b>{letter.signatory_name}</b>')
    if letter.signatory_title:
        name_title_lines.append(letter.signatory_title)
    name_title_flowable = Paragraph('<br/>'.join(name_title_lines), style_body) if name_title_lines else None

    # ── Assemble ─────────────────────────────────────────────────────
    elements = [
        Paragraph(f'Ref: {letter.ref_no}', style_body) if letter.ref_no else Spacer(1, 0),
        Spacer(1, 4),
        Paragraph(f"Date: {letter.letter_date.strftime('%B %d, %Y')}", style_body),
        Spacer(1, 6),
        Paragraph('<u>WARRANTY LETTER</u>', style_title),
    ]
    if label_tbl:
        elements += [label_tbl, Spacer(1, 12)]

    # Intro text: one line per paragraph (mirrors how Warranty Terms splits
    # by line), but rendered as plain paragraphs -- no bullet markers.
    intro_lines = [ln.strip() for ln in (letter.intro_text or '').splitlines() if ln.strip()]
    if intro_lines:
        elements += [Paragraph(line, style_body) for line in intro_lines]
        elements.append(Spacer(1, 10))

    elements += [items_tbl, Spacer(1, 12)]

    if terms_list:
        elements += [terms_list, Spacer(1, 16)]

    elements += [
        Paragraph('For,', style_body),
        Paragraph(f'M/s. {settings_row.legal_company_name}', style_body_bold),
        Spacer(1, 8),
    ]
    if sign_stamp_flowable:
        elements += [sign_stamp_flowable, Spacer(1, 2)]
    if name_title_flowable:
        elements.append(name_title_flowable)

    doc.build(elements)
    buf.seek(0)
    return buf
