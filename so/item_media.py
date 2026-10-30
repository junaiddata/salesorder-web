"""Turning an uploaded photo / datasheet into the small JPEG the item catalog stores.

Photos are shrunk on upload (a raw phone photo is several MB; the catalog needs a few tens of KB), datasheets
(PDF / JPG / PNG) become one JPEG through the same converter the Junaid Galleria module uses.
"""
from io import BytesIO

from PIL import Image, ImageOps

from quotation.datasheet import DatasheetError, to_jpeg

MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_DATASHEET_BYTES = 10 * 1024 * 1024
PHOTO_MAX_SIDE = 800
PHOTO_QUALITY = 80


class MediaError(ValueError):
    pass


def photo_to_jpeg(uploaded):
    """JPEG bytes (longest side <= PHOTO_MAX_SIDE) for an uploaded photo; MediaError when it is too big or not an image."""
    if uploaded.size > MAX_PHOTO_BYTES:
        raise MediaError('the photo is too large (max 5 MB).')
    try:
        data = uploaded.read()
        Image.open(BytesIO(data)).verify()
        img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
        img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
        if img.mode in ('RGBA', 'LA', 'P'):   # JPEG has no transparency: put it on white
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.getchannel('A'))
            img = bg
        else:
            img = img.convert('RGB')
        out = BytesIO()
        img.save(out, 'JPEG', quality=PHOTO_QUALITY, optimize=True)
        return out.getvalue()
    except MediaError:
        raise
    except Exception:
        raise MediaError('please upload a valid image (PNG or JPG).')


def datasheet_to_jpeg(uploaded):
    """JPEG bytes for an uploaded datasheet (PDF / JPG / PNG); MediaError when it is too big or unreadable."""
    if uploaded.size > MAX_DATASHEET_BYTES:
        raise MediaError('the datasheet is too large (max 10 MB).')
    try:
        return to_jpeg(uploaded)[1]
    except DatasheetError as exc:
        raise MediaError(str(exc))
    except Exception:
        raise MediaError('the datasheet could not be read.')
