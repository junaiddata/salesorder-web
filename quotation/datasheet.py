"""Datasheet upload -> JPEG.

Whatever the user uploads (PDF, JPG, PNG...) is stored as a single JPEG, so the
public QR/link always shows and downloads a JPEG. A multi-page PDF becomes one
tall JPEG with the pages stacked top to bottom.
"""
import os
from io import BytesIO

from PIL import Image, ImageOps

MAX_PDF_PAGES = 10
PAGE_PX_WIDTH = 1240        # ~150 dpi on A4: sharp on screen and in print, still a small file
MAX_IMAGE_SIDE = 3000
JPEG_QUALITY = 85


class DatasheetError(ValueError):
    pass


def _flatten(img):
    """RGB image on a white background (JPEG has no transparency)."""
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel('A'))
        return bg
    return img.convert('RGB')


def _pdf_to_image(data):
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=data, filetype='pdf')
    except Exception:
        raise DatasheetError('this PDF could not be read.')
    if doc.needs_pass:
        raise DatasheetError('this PDF is password protected.')
    if doc.page_count == 0:
        raise DatasheetError('this PDF has no pages.')
    if doc.page_count > MAX_PDF_PAGES:
        raise DatasheetError(f'this PDF has {doc.page_count} pages (max {MAX_PDF_PAGES}).')
    pages = []
    for page in doc:
        zoom = PAGE_PX_WIDTH / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pages.append(Image.frombytes('RGB', (pix.width, pix.height), pix.samples))
    if len(pages) == 1:
        return pages[0]
    width = max(p.width for p in pages)
    sheet = Image.new('RGB', (width, sum(p.height for p in pages)), (255, 255, 255))
    y = 0
    for p in pages:
        sheet.paste(p, ((width - p.width) // 2, y))
        y += p.height
    return sheet


def to_jpeg(uploaded):
    """Convert an uploaded PDF/image to JPEG. Returns (filename.jpg, bytes)."""
    data = uploaded.read()
    uploaded.seek(0)
    stem = os.path.splitext(os.path.basename(uploaded.name))[0] or 'datasheet'
    if data[:5] == b'%PDF-':
        img = _pdf_to_image(data)
    else:
        try:
            Image.open(BytesIO(data)).verify()
            img = Image.open(BytesIO(data))
            img = ImageOps.exif_transpose(img)
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            img = _flatten(img)
        except Exception:
            raise DatasheetError('the datasheet must be a PDF or an image (JPG/PNG).')
    out = BytesIO()
    img.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True)
    return f'{stem}.jpg', out.getvalue()
