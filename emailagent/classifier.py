"""Email classification: does this email + its attachments look like a
customer RFQ/enquiry? Runs as a tool-calling agent -- Claude can look up
similar past enquiries and the item catalog before finalizing its answer
(vision for images, extracted text for PDF/Excel) via `submit_classification`.
"""
import base64
import hashlib
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal

import anthropic
import fitz  # PyMuPDF
import openpyxl
from anthropic import beta_tool
from django.conf import settings
from PyPDF2 import PdfReader
from pydantic import BaseModel

from emailagent.tools import lookup_item_master, search_similar_enquiries

logger = logging.getLogger(__name__)

CATEGORY_RFQ = 'rfq'
CATEGORY_NOT_RELEVANT = 'not_relevant'
CATEGORY_UNCERTAIN = 'uncertain'


class ClassificationItem(BaseModel):
    description: str
    category: str
    brand: str
    quantity: str
    unit: str
    notes: str
    source_attachment: str = ""


class ClassificationAttachment(BaseModel):
    filename: str
    source: Literal["client_requirement", "supplier_reference", "lpo_document", "other"]


class SubmittalClassificationItem(BaseModel):
    description: str
    brand: str = ""
    category: str = ""


class LPOClassificationItem(BaseModel):
    description: str
    extra_description: str = ""
    quantity: str = ""
    unit: str = ""
    price: str = ""
    discount_percent: str = ""
    vat_amount: str = ""
    amount: str = ""


@beta_tool
def submit_classification(
    category: Literal["rfq", "not_relevant", "uncertain"],
    confidence: float,
    reasoning: str,
    items: list[ClassificationItem],
    attachments: list[ClassificationAttachment],
    is_submittal_request: bool = False,
    submittal_brand: str = "",
    submittal_project: str = "",
    submittal_client: str = "",
    submittal_consultant: str = "",
    submittal_main_contractor: str = "",
    submittal_mep_contractor: str = "",
    submittal_items: list[SubmittalClassificationItem] = [],
    is_lpo: bool = False,
    lpo_number: str = "",
    lpo_date: str = "",
    lpo_customer_name: str = "",
    lpo_referenced_quotation_number: str = "",
    lpo_delivery_terms: str = "",
    lpo_payment_terms: str = "",
    lpo_total_amount: str = "",
    lpo_total_discount: str = "",
    lpo_total_excl_vat: str = "",
    lpo_total_vat: str = "",
    lpo_amount_in_words: str = "",
    lpo_items: list[LPOClassificationItem] = [],
    duplicate_of_tracked_email_id: int = 0,
) -> str:
    """Record your final RFQ classification for this email. Call this exactly
    once, when you are done reasoning (and have used search_similar_enquiries
    / lookup_item_master if useful) -- do not call any other tool afterward.

    Args:
        category: "rfq" if a customer enquiry/RFQ appears anywhere in the
            thread, "not_relevant" if there is no customer pricing/requirement
            request anywhere in it, or "uncertain" if you cannot tell
            confidently.
        confidence: Your confidence in `category`, from 0.0 to 1.0.
        reasoning: One or two sentences explaining the decision, including
            any thread-revision or client-vs-supplier-attachment reasoning.
        items: Requirement/BOQ line items requested by the customer, if any
            -- from the email body, or a table in a PDF/Excel/image
            attachment. Only from client_requirement-sourced content -- never
            from a supplier_reference attachment. Empty list if none. Each
            item's source_attachment should be set to the exact filename it
            came from (from the "attachments" list below), or "" if it came
            from the email body -- see the multi-attachment guidance below.
        attachments: One entry per PDF/Excel attachment provided, classifying
            who it came from -- "client_requirement" (the customer's own
            requirement/BOQ/spec document), "supplier_reference" (our own
            previously-sent quotation/pricing document, kept only for
            reference), "lpo_document" (the customer's own Purchase Order /
            LPO PDF confirming an order -- see is_lpo below), or "other"
            (invoice, delivery note, unrelated).
        is_submittal_request: True if the customer/consultant is asking for
            MATERIAL SUBMITTAL / technical approval documents (catalogues,
            datasheets, test certificates, compliance statement, etc. for
            specific brands/products, to get them approved for a project) --
            independent of whether pricing (category="rfq") was also asked
            for; a submittal request can arrive before, after, or instead of
            a pricing enquiry. False otherwise.
        submittal_brand: The single overall brand for the submittal, if the
            email states one brand for the whole request (e.g. "Pegler")
            rather than a different brand per item. "" if not stated, if
            brands vary per item (state each on its own submittal_items
            entry instead), or is_submittal_request is False.
        submittal_project: Project name, if stated. "" otherwise.
        submittal_client: Client/employer name, if stated. "" otherwise.
        submittal_consultant: Consultant company name, if stated. "" otherwise.
        submittal_main_contractor: Main contractor name, if stated. "" otherwise.
        submittal_mep_contractor: MEP contractor name, if stated. "" otherwise.
        submittal_items: Each specific item/model the submittal is needed
            for (e.g. "PRV 2 inch", "V8850 control valve"), if listed. Empty
            list if not stated or is_submittal_request is False -- do not
            invent items. IMPORTANT: a single submittal request commonly
            covers SEVERAL DIFFERENT BRANDS and/or product categories at
            once (e.g. Pegler valves, Ariston water heaters, Cosmoplast
            drainage pipes, all in the same email) -- these must become
            SEPARATE submittal packages downstream, one per distinct
            brand+category combination, so set each item's own `brand`
            (falling back to submittal_brand only when every item shares
            one brand) and `category` (a short, consistent label grouping
            similar items, same convention as the `items` field's category
            above, e.g. "Valves", "Water Heaters", "Drainage Pipes",
            "Manhole Covers & Gratings") accurately per item -- never leave
            them blank/guessed when the email states or clearly implies a
            different brand/category per item.
        is_lpo: True if the customer is sending their own Purchase Order /
            LPO (Local Purchase Order) -- a document CONFIRMING an order at
            already-agreed pricing (e.g. "as per your quotation QTN1234, "
            "please proceed with the below order"), as opposed to asking
            for NEW pricing (category="rfq") or asking for submittal/
            approval documents (is_submittal_request). Independent of both
            of those -- can co-occur with either, e.g. a thread where the
            client also asks a fresh question alongside confirming an
            order. Look for the customer's own letterhead/branding (not
            ours), a heading like "Purchase Order" / "LPO" / "PO No.", and
            language confirming/placing an order rather than requesting a
            quote. False otherwise -- never mistake OUR OWN quotation or
            invoice sent to the client for their LPO.
        lpo_number: The PO/LPO number stated on the document. "" if not
            stated or is_lpo is False.
        lpo_date: The PO/LPO date, exactly as written on the document (do
            not reformat or guess a format). "" if not stated.
        lpo_customer_name: The customer/company name stated on the LPO
            (their own letterhead/company name, not ours). "" if not
            stated.
        lpo_referenced_quotation_number: The quotation/reference number
            the LPO cites as what it's confirming, if stated (e.g. "QTN1234"
            from a line like "As per your quotation QTN1234 dated..."). ""
            if the LPO doesn't cite one.
        lpo_delivery_terms: Delivery terms/date/location as stated on the
            LPO. "" if not stated.
        lpo_payment_terms: Payment terms as stated on the LPO. "" if not
            stated.
        lpo_total_amount: The total/grand amount (i.e. total INCL. VAT)
            stated on the LPO, exactly as written (keep currency symbol/
            formatting -- do not convert to a number). "" if not stated.
        lpo_total_discount: The total discount amount stated in the LPO's
            totals block (e.g. a "Total Discount" line), exactly as
            written. "" if the document doesn't break this out separately.
        lpo_total_excl_vat: The total amount EXCL. VAT stated in the LPO's
            totals block, exactly as written. "" if not broken out
            separately from the grand total.
        lpo_total_vat: The total VAT amount stated in the LPO's totals
            block, exactly as written. "" if not broken out separately.
        lpo_amount_in_words: The grand total spelled out in words, exactly
            as written (e.g. "Seven thousand five hundred six and 35/100
            AED ONLY"), if the document states one. "" if not stated.
        lpo_items: Each line item on the LPO, in the order listed on the
            document. Empty list if not stated or is_lpo is False -- do not
            invent items. For each item capture whatever the document's
            table states, exactly as written (leave a sub-field "" if that
            column isn't present): description (the item name/code, e.g.
            'GI HANGING CLAMP 6"'), extra_description (a separate
            description column/line if the table has one distinct from the
            item name, e.g. 'WITH RUBBER' -- "" if there's only one text
            column), quantity, unit (UOM), price (unit price, excl. VAT),
            discount_percent (the line's discount %, e.g. "3"), vat_amount
            (the line's VAT amount, if broken out per line), and amount
            (the line's total/extended amount).
        duplicate_of_tracked_email_id: If you called search_similar_enquiries
            and one of the results it returned (cited there as
            "[tracked_email_id=NNN]") is clearly the SAME underlying
            requirement being resent -- e.g. this email is a "reminder" /
            "soft reminder" / "following up" re-send of an enquiry already
            tracked from the same sender, with the same or near-identical
            item list -- set this to that NNN so it can be linked back to
            the original instead of drafting a second, duplicate quotation
            for the same requirement. Only set this for a genuine repeat of
            the SAME requirement (same items/scope) from the SAME sender --
            never for a merely similar-looking but actually different
            enquiry, and never just because search_similar_enquiries
            returned a result (only when you're confident it's the same
            request being reminded about). 0 (default) if you didn't call
            search_similar_enquiries, found nothing, or this is a genuinely
            new/different requirement.
    """
    return "Recorded"


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    reasoning: str
    items: list
    attachment_sources: dict
    is_submittal_request: bool = False
    submittal_brand: str = ''
    submittal_project: str = ''
    submittal_client: str = ''
    submittal_consultant: str = ''
    submittal_main_contractor: str = ''
    submittal_mep_contractor: str = ''
    submittal_items: list = field(default_factory=list)
    is_lpo: bool = False
    lpo_number: str = ''
    lpo_date: str = ''
    lpo_customer_name: str = ''
    lpo_referenced_quotation_number: str = ''
    lpo_delivery_terms: str = ''
    lpo_payment_terms: str = ''
    lpo_total_amount: str = ''
    lpo_total_discount: str = ''
    lpo_total_excl_vat: str = ''
    lpo_total_vat: str = ''
    lpo_amount_in_words: str = ''
    lpo_items: list = field(default_factory=list)
    duplicate_of_tracked_email_id: int = 0
    model_used: str = ''


_PDF_SPARSE_TEXT_CHARS = 200  # a PDF whose real content is a BOQ/item table
# extracts far more than this via PyPDF2 -- below this, extraction likely
# only caught a caption/header/footer sitting alongside a table rendered as
# an image, so page images are rendered as a supplement (see
# build_classification_content) rather than trusting a short text layer.


def extract_pdf_text(file_bytes: bytes, max_chars: int = 20000) -> str:
    try:
        reader = PdfReader(BytesIO(file_bytes))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        return text[:max_chars]
    except Exception as e:
        logger.warning(f"PDF text extraction failed: {e}")
        return ''


def render_pdf_pages_as_images(file_bytes: bytes, max_pages: int = 5, dpi: int = 150) -> list:
    """Renders PDF pages to PNG bytes -- used as a fallback for scanned/
    image-only PDFs (no extractable text layer) so their content can still
    be read via vision instead of being silently missed."""
    images = []
    try:
        doc = fitz.open(stream=file_bytes, filetype='pdf')
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc[:max_pages]:
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes('png'))
        doc.close()
    except Exception as e:
        logger.warning(f"PDF page rendering failed: {e}")
    return images


def extract_excel_text(file_bytes: bytes, max_rows: int = 200) -> str:
    try:
        wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True, read_only=True)
        parts = []
        for sheet in wb.worksheets:
            parts.append(f"Sheet: {sheet.title}")
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= max_rows:
                    break
                parts.append('\t'.join('' if v is None else str(v) for v in row))
        return '\n'.join(parts)
    except Exception as e:
        logger.warning(f"Excel text extraction failed: {e}")
        return ''


def _image_media_type(content_type: str) -> str:
    return content_type if content_type in (
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    ) else 'image/jpeg'


def build_classification_content(email: dict, attachments: list) -> list:
    """attachments: list of dicts with keys filename, content_type,
    included_in_classification, extracted_text, data (raw bytes, may be
    absent/None if not needed for this attachment type)."""
    text_parts = [
        f"From: {email.get('sender_name', '')} <{email.get('sender', '')}>",
        f"Subject: {email.get('subject', '')}",
        f"Body:\n{email.get('body_text', '')}",
    ]

    image_blocks = []
    seen_image_hashes = set()  # skip byte-identical images (e.g. the same
    # signature graphic repeated at every quoted reply in a long forwarded
    # chain) so they don't burn through EMAILAGENT_MAX_ATTACHMENT_IMAGES
    # slots that a genuinely distinct requirement image/table needs.
    dropped_images = []

    def _add_image(data_bytes, media_type, label):
        if not data_bytes:
            return
        digest = hashlib.sha1(data_bytes).hexdigest()
        if digest in seen_image_hashes:
            return
        if len(image_blocks) >= settings.EMAILAGENT_MAX_ATTACHMENT_IMAGES:
            dropped_images.append(label)
            return
        seen_image_hashes.add(digest)
        image_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(data_bytes).decode('utf-8'),
            },
        })

    for att in attachments:
        if not att.get('included_in_classification', True):
            continue
        content_type = att.get('content_type', '')
        data_bytes = att.get('data')
        if content_type == 'application/pdf':
            text = att.get('extracted_text') or (extract_pdf_text(data_bytes) if data_bytes else '')
            if text.strip():
                text_parts.append(f"\n--- Attachment (PDF): {att.get('filename')} ---\n{text}")
            if len(text.strip()) < _PDF_SPARSE_TEXT_CHARS and data_bytes:
                # Either no extractable text layer at all (a fully scanned/
                # image-only PDF), or only a thin one -- e.g. a one-line
                # caption/note ("All PVC Pipe and Fittings in white color")
                # sitting above a BOQ/item table that's actually a raster
                # image PyPDF2 can't read at all. A few words of extracted
                # text is not proof the real content was captured, so render
                # pages as images too whenever text is this sparse rather
                # than trusting it just because it's non-empty -- otherwise
                # the table is silently missed entirely (observed: a 287KB
                # PDF where extraction returned only that one caption line).
                page_images = render_pdf_pages_as_images(data_bytes)
                if page_images:
                    text_parts.append(
                        f"\n--- Attachment (PDF, also rendered as {len(page_images)} page image(s) below "
                        f"since extracted text was sparse/empty): {att.get('filename')} ---"
                    )
                for i, page_png in enumerate(page_images):
                    _add_image(page_png, "image/png", f"{att.get('filename')} (page {i + 1})")
        elif content_type in ('application/vnd.ms-excel',
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
            text = att.get('extracted_text') or (extract_excel_text(data_bytes) if data_bytes else '')
            if text:
                text_parts.append(f"\n--- Attachment (Excel): {att.get('filename')} ---\n{text}")
        elif content_type.startswith('image/'):
            _add_image(data_bytes, _image_media_type(content_type), att.get('filename'))

    if dropped_images:
        logger.warning(
            f"build_classification_content: EMAILAGENT_MAX_ATTACHMENT_IMAGES "
            f"({settings.EMAILAGENT_MAX_ATTACHMENT_IMAGES}) reached -- dropped {len(dropped_images)} "
            f"image(s) that could contain real content: {dropped_images}"
        )

    content = [{"type": "text", "text": '\n'.join(text_parts)}]
    content.extend(image_blocks)
    return content


_CLASSIFICATION_PROMPT = (
    "You are triaging an inbox for a sales team that sells building materials, "
    "electrical/plumbing supplies, and similar trade goods. Decide whether the "
    "email below (including any attached images, PDFs, or Excel sheets) contains "
    "a genuine customer enquiry or request for quotation (RFQ) -- i.e. a customer "
    "asking for pricing, availability, or submitting requirements/specs/a BOQ "
    "to be quoted.\n\n"
    "Important: many real RFQs arrive as forwarded threads -- a colleague forwards "
    "the client's original request, sometimes with a reply/quotation already "
    "included further down the same thread. Look at the WHOLE email body for a "
    "customer's original ask, not just the most recent message on top. If a "
    "customer's request for pricing/specs/BOQ appears anywhere in the thread "
    "(quoted text, forwarded section, attached correspondence), classify this as "
    "\"rfq\" -- the fact that a quote was already sent in reply does not make it "
    "not_relevant; the original customer requirement still needs to be tracked. "
    "Only use \"not_relevant\" for content that has no customer pricing/requirement "
    "request anywhere in it at all (newsletters, internal-only mail with no "
    "customer ask, spam, invoices, delivery notifications, etc.).\n\n"
    "Important -- threads often contain a REVISION: the client asks for one "
    "quantity/spec/brand, then later in the same thread asks to change it (e.g. "
    "'please revise the quotation to 2 no's', 'change qty to X', 'update the "
    "spec to Y', 'please quote in Cosmoplast instead', 'change brand to X'). "
    "Read the WHOLE thread in chronological order (oldest message is usually "
    "quoted furthest down/at the bottom) and use the LATEST value the client "
    "actually asked for -- not the original ask, and not whatever value happens "
    "to appear first in the visible text. When you do this, set that item's "
    "\"notes\" to explain the revision (e.g. 'revised from 3 to 2 on 25 Jul', "
    "or 'customer requested brand change from Cosmoplast to Pilsa on 25 Jul') "
    "so it's clear why the value differs from the original message -- this "
    "matters because a reader skimming just the original request line would "
    "otherwise think the extracted quantity/brand is wrong. A short reply that "
    "ONLY asks to change a brand or quantity on an already-discussed enquiry, "
    "with no other RFQ language, still counts as \"rfq\" (not "
    "\"not_relevant\"/\"uncertain\") -- it is a live correction to a real "
    "enquiry, not a new or irrelevant message.\n\n"
    "Important -- distinguish the CLIENT's own document from OUR OWN reference "
    "document: forwarded threads often carry along the quotation WE already sent "
    "back to the client, attached purely for reference. Our own quotation PDFs "
    "typically look like a formal priced quote -- headers like 'Quotation No:', "
    "'We are pleased to quote', unit price / total amount columns, our company "
    "letterhead addressed to the customer, payment/delivery terms, filenames "
    "starting with an internal document number (e.g. '126004902 SPOTS.pdf'). The "
    "client's own requirement document looks different -- a plain list of what "
    "they need, phrasing like 'kindly quote', 'please provide pricing for the "
    "following', 'required qty', a BOQ/spec table with no pricing, or a customer "
    "letterhead/signature instead of ours. For every PDF/Excel attachment "
    "provided, classify it in \"attachments\" as \"client_requirement\" (the "
    "customer's own document), \"supplier_reference\" (our own previously-sent "
    "quotation/pricing document, kept only for reference), or \"other\".\n\n"
    "Also extract the requested items as structured data in \"items\", if any are "
    "present -- whether they're listed directly in the email body, or in a table "
    "inside a PDF, an Excel sheet, or a photo/screenshot of a table (read the "
    "table from the image). Important -- a single enquiry commonly spreads its "
    "requirement across MULTIPLE separate sources at once (e.g. some items typed "
    "in the email body, plus one or more DIFFERENT attachments each showing a table "
    "for a different scope/building/floor -- e.g. two Excel files named 'Building 1' "
    "and 'Building 2' with a similar or even IDENTICAL list of consumables/materials "
    "for each): finding items in one place is not a reason to stop -- go through the "
    "email body AND every single attached image/PDF/Excel individually and extract "
    "EVERY row from EACH one that has any. Set each item's \"source_attachment\" to "
    "the exact filename it came from (from the \"attachments\" list), or \"\" if it "
    "came from the email body. CRITICAL: when two or more attachments list similar "
    "or identical item names for DIFFERENT scopes, do NOT merge, deduplicate, or "
    "average them into one combined list -- extract ALL rows from EACH attachment as "
    "their OWN separate item entries (same description is fine and expected across "
    "attachments; that is not a duplicate, it is the same material needed for a "
    "different building/scope) so a human downstream can tell exactly which "
    "attachment each requirement came from and quote each scope separately. Never "
    "assume an image/attachment is 'just a duplicate' of another one without "
    "actually reading it -- forwarded threads often repeat the same signature "
    "graphic several times, but two attachments can also look superficially similar "
    "while being genuinely separate requirements, so check each one's actual content "
    "rather than its position/appearance/filename. ONLY extract items from "
    "client_requirement-sourced content (the email body itself counts as client "
    "content when it contains the customer's own ask) -- never extract items from a "
    "supplier_reference document, even though it also lists the same items with "
    "pricing; that would just be re-reading our own quote back to ourselves. Each "
    "item should "
    "capture its description, a short product category, brand (if stated), "
    "quantity, and unit of measure exactly as given (e.g. a row like '1 1/4\" HP "
    "UPVC Pipe / Brand: Cosmoplast | 24 | MTR' becomes one item with description "
    "'1 1/4\" HP UPVC Pipe', category 'PVC Pipes & Fittings', brand 'Cosmoplast', "
    "quantity '24', unit 'MTR'). For category, use a short, consistent label "
    "that groups similar items together (e.g. 'PVC Pipes & Fittings', 'PPR "
    "Pipes & Fittings', 'Water Heaters', 'Valves', 'Electrical Accessories', "
    "'Sanitary Ware', 'Tools', 'Safety Equipment') so items of the same kind "
    "always get the same category label across different emails -- don't invent "
    "a new, overly specific category for every single row. Preserve the "
    "original row order. If there is no itemized list anywhere in the client's "
    "own content, return an empty items array -- do not invent items.\n\n"
    "A brand requirement doesn't always appear as a per-row column -- customers "
    "often state it once for the whole enquiry instead, e.g. in the subject or "
    "an opening/closing line like \"Brand: Cosmo or cheapest\" or \"kindly quote "
    "in Cosmoplast, or cheapest available\". When that happens, apply it as the "
    "brand value for every item it reasonably covers (unless a specific row "
    "states a different brand) -- and keep any fallback wording (\"or "
    "cheapest\", \"or equivalent\", etc.) as part of the brand text itself, "
    "exactly as the customer phrased it, so the full instruction is visible "
    "later, not just the brand name.\n\n"
    "Separately, also decide is_submittal_request: many clients/consultants ask "
    "for MATERIAL SUBMITTAL (a.k.a. technical submittal) documents -- catalogues, "
    "datasheets, test certificates, country-of-origin certificates, a compliance "
    "statement -- so specific brands/products can be formally APPROVED for a "
    "project, as distinct from asking for pricing. This can arrive before a "
    "pricing enquiry (approve the brand first, quote later), after one (a "
    "quotation was already sent and now the consultant wants the paperwork), or "
    "on its own with no pricing request at all -- set is_submittal_request=True "
    "whenever you see language like \"submit for approval\", \"submittal\", "
    "\"technical submittal\", \"material approval\", \"MAS\" (material approval "
    "sheet), \"please provide datasheet/catalogue/test certificate for approval\", "
    "regardless of what category you chose above. When True, fill in whichever of "
    "submittal_brand/submittal_project/submittal_client/submittal_consultant/"
    "submittal_main_contractor/submittal_mep_contractor/submittal_items the email "
    "actually states (leave any not mentioned as \"\"/empty -- never invent "
    "project/company names or items that weren't written). A submittal request "
    "commonly spans several different brands and/or product categories in the "
    "SAME email (e.g. Pegler valves, Ariston water heaters, and Cosmoplast "
    "drainage pipes all being submitted for approval on the same project) -- "
    "these become SEPARATE submittal packages downstream, one per distinct "
    "brand+category combination, so give each submittal_items entry its own "
    "accurate brand and category rather than one blanket brand for everything "
    "(see the submittal_items argument description for details).\n\n"
    "Separately, also decide is_lpo: a client sometimes sends their OWN Purchase "
    "Order / LPO (Local Purchase Order) -- a document CONFIRMING an order at "
    "already-agreed pricing, distinct from an RFQ asking for NEW pricing or a "
    "submittal request asking for approval documents. Look for the CUSTOMER's own "
    "letterhead/branding (never ours), a heading like \"Purchase Order\", \"LPO\", "
    "or \"PO No.\", and confirming language (e.g. \"please proceed with the below "
    "order\", \"as per your quotation QTN1234, kindly supply...\") rather than a "
    "request for pricing. Set is_lpo=True whenever this appears anywhere in the "
    "thread, independent of category and of is_submittal_request -- an email can "
    "carry an LPO alongside a fresh question or a submittal ask. When True, extract "
    "exactly what the document states into lpo_number/lpo_date/lpo_customer_name/"
    "lpo_referenced_quotation_number/lpo_delivery_terms/lpo_payment_terms/"
    "lpo_total_amount/lpo_total_discount/lpo_total_excl_vat/lpo_total_vat/lpo_amount_in_words/"
    "lpo_items -- leave any field not stated as \"\"/empty, never "
    "invent a quotation number, amount, or item that isn't written on the document. "
    "lpo_referenced_quotation_number is important: look specifically for any "
    "reference to one of OUR quotation numbers (typically prefixed QTN or ALQ, e.g. "
    "\"QTN1234\" or \"ALQ1005\") stated anywhere on the LPO or in the email body "
    "around it (e.g. \"as per quotation QTN1234\", \"ref: QTN1234\", a Subject line "
    "mentioning it) -- this is the single most important field for matching the LPO "
    "back to the right quotation, so read carefully for it even if it's stated only "
    "once, informally, or in a different part of the thread than the PDF itself. "
    "Also mark that attachment's own entry in the attachments list as source="
    "\"lpo_document\" (not client_requirement/supplier_reference/other).\n\n"
    "Before finalizing, you may call search_similar_enquiries (to check whether "
    "this looks like a duplicate or a reminder of an enquiry already tracked from "
    "the same sender) and lookup_item_master (to verify/correct an item's brand, "
    "category, or check its current stock/price against the real catalog) as many "
    "times as useful. If search_similar_enquiries confirms this is genuinely the "
    "SAME requirement being resent (a reminder/follow-up repeating the same items, "
    "not just a similar-looking different enquiry), set duplicate_of_tracked_email_id "
    "to the matching result's tracked_email_id -- see that argument's own description "
    "for the exact bar. When you are done, call submit_classification exactly once "
    "with your final answer -- that is the only way to record a result; do not "
    "just describe your answer in text."
)


@beta_tool
def submit_triage(is_simple: bool, reasoning: str) -> str:
    """Record the triage verdict for whether this email is simple enough to
    classify with a fast/cheap model, or complex enough to need the full
    classification model. Call this exactly once.

    Args:
        is_simple: True ONLY for emails that are obviously not a real
            enquiry needing careful reading -- e.g. an out-of-office
            autoreply, a one-line acknowledgement/thank-you, a newsletter,
            a delivery/read notification -- with no attachments and nothing
            resembling an item list, pricing, brand/quantity detail, LPO,
            or submittal request. False for anything with attachments, a
            table/BOQ, multiple items, or any content that needs careful
            reading to classify correctly. When unsure, choose False.
        reasoning: One short sentence explaining the verdict.
    """
    return "Recorded"


_TRIAGE_PROMPT = (
    "You are triaging an inbox for a sales team that sells building "
    "materials, electrical/plumbing supplies, and similar trade goods. "
    "Before this email is properly classified, decide only whether it is "
    "SIMPLE (obviously not a customer enquiry -- an autoreply, newsletter, "
    "one-line acknowledgement, delivery notification, nothing to extract) "
    "or COMPLEX (anything that could be a real RFQ, submittal request, or "
    "LPO -- has attachments, an itemized list, pricing, or brand/quantity "
    "detail, or just isn't obviously irrelevant). A wrong 'simple' verdict "
    "means a real enquiry gets processed by a weaker model, so when unsure, "
    "choose COMPLEX. Call submit_triage exactly once with your verdict."
)


def _triage_is_simple(email: dict, attachments: list) -> bool:
    """Cheap pre-check (EMAILAGENT_TRIAGE_MODEL, text-only -- no attachment
    content decoded) deciding whether classify_email can use the fast/cheap
    model instead of EMAILAGENT_CLASSIFICATION_MODEL. Never raises; defaults
    to False (route to the strong model) on any failure, timeout, or
    inconclusive verdict -- a missed triage should never silently downgrade
    a real enquiry."""
    summary = (
        f"From: {email.get('sender_name', '')} <{email.get('sender', '')}>\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Attachments: {len(attachments)} "
        f"({', '.join(a.get('filename', '') for a in attachments) or 'none'})\n\n"
        f"Body:\n{email.get('body_text', '')[:2000]}"
    )
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_TRIAGE_MODEL,
            max_tokens=300,
            thinking={"type": "disabled"},
            tools=[submit_triage],
            messages=[{"role": "user", "content": f"{_TRIAGE_PROMPT}\n\n{summary}"}],
            max_iterations=2,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_triage":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"triage agent loop API error: {exc!r}")
    except Exception:
        logger.exception("triage agent loop unexpected failure")

    if captured is None:
        return False
    return bool(captured.get("is_simple", False))


def classify_email(email: dict, attachments: list) -> ClassificationResult:
    is_simple = _triage_is_simple(email, attachments)
    model_to_use = settings.EMAILAGENT_TRIAGE_MODEL if is_simple else settings.EMAILAGENT_CLASSIFICATION_MODEL
    logger.info(f"classify_email triage: is_simple={is_simple}, model={model_to_use}")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    content = build_classification_content(email, attachments)
    content.insert(0, {"type": "text", "text": _CLASSIFICATION_PROMPT})

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=model_to_use,
            # A large multi-attachment BOQ (e.g. two ~60-row Excel sheets) can
            # need well over 4096 output tokens just to emit the "items" list
            # in the submit_classification tool call -- 4096 was observed to
            # truncate mid-call (stop_reason="max_tokens") on a 118-item
            # enquiry, silently landing on items=[] even though the model's
            # own reasoning correctly described the content. This doesn't
            # cost more for a normal small enquiry (billing is by tokens
            # actually generated, not this ceiling), it just gives large
            # ones room to finish.
            max_tokens=16000,
            thinking={"type": "disabled"},
            tools=[search_similar_enquiries, lookup_item_master, submit_classification],
            messages=[{"role": "user", "content": content}],
            max_iterations=settings.EMAILAGENT_AGENT_MAX_ITERATIONS,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_classification":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"classify_email agent loop API error: {exc!r}")
    except Exception:
        logger.exception("classify_email agent loop unexpected failure")

    if captured is None:
        return ClassificationResult(
            category=CATEGORY_UNCERTAIN,
            confidence=0.0,
            reasoning="Agent did not finalize a classification (ran out of iterations, refused, or errored).",
            items=[],
            attachment_sources={},
            model_used=model_to_use,
        )

    attachment_sources = {a['filename']: a['source'] for a in captured.get('attachments', [])}
    return ClassificationResult(
        category=captured["category"],
        confidence=float(captured["confidence"]),
        reasoning=captured["reasoning"],
        items=captured.get("items", []),
        attachment_sources=attachment_sources,
        is_submittal_request=bool(captured.get("is_submittal_request", False)),
        submittal_brand=captured.get("submittal_brand", "") or "",
        submittal_project=captured.get("submittal_project", "") or "",
        submittal_client=captured.get("submittal_client", "") or "",
        submittal_consultant=captured.get("submittal_consultant", "") or "",
        submittal_main_contractor=captured.get("submittal_main_contractor", "") or "",
        submittal_mep_contractor=captured.get("submittal_mep_contractor", "") or "",
        submittal_items=captured.get("submittal_items", []) or [],
        is_lpo=bool(captured.get("is_lpo", False)),
        lpo_number=captured.get("lpo_number", "") or "",
        lpo_date=captured.get("lpo_date", "") or "",
        lpo_customer_name=captured.get("lpo_customer_name", "") or "",
        lpo_referenced_quotation_number=captured.get("lpo_referenced_quotation_number", "") or "",
        lpo_delivery_terms=captured.get("lpo_delivery_terms", "") or "",
        lpo_payment_terms=captured.get("lpo_payment_terms", "") or "",
        lpo_total_amount=captured.get("lpo_total_amount", "") or "",
        lpo_total_discount=captured.get("lpo_total_discount", "") or "",
        lpo_total_excl_vat=captured.get("lpo_total_excl_vat", "") or "",
        lpo_total_vat=captured.get("lpo_total_vat", "") or "",
        lpo_amount_in_words=captured.get("lpo_amount_in_words", "") or "",
        lpo_items=captured.get("lpo_items", []) or [],
        duplicate_of_tracked_email_id=int(captured.get("duplicate_of_tracked_email_id") or 0),
        model_used=model_to_use,
    )


@beta_tool
def submit_lpo_details(
    items: list[LPOClassificationItem],
    total_discount: str = "",
    total_excl_vat: str = "",
    total_vat: str = "",
    total_incl_vat: str = "",
    amount_in_words: str = "",
) -> str:
    """Record the re-extracted LPO line items and totals block. Call this
    exactly once, when you are done reading the document -- do not
    describe your answer in text.

    Args:
        items: Each line item in the order listed on the document's items
            table. Do not invent rows, and do not include the totals-block
            summary rows (Total Discount, Total Excl. VAT, Total VAT, Total
            Incl. VAT, Amount in Words -- those go in the separate
            arguments below instead) as items. For each item capture
            whatever the table states, exactly as written (leave a
            sub-field "" if that column isn't present): description (the
            item name/code, e.g. 'GI HANGING CLAMP 6"'), extra_description
            (a separate description column/line distinct from the item
            name, e.g. 'WITH RUBBER' -- "" if there's only one text
            column), quantity, unit (UOM), price (unit price, excl. VAT),
            discount_percent (the line's discount %, e.g. "3"), vat_amount
            (the line's VAT amount, if broken out per line), and amount
            (the line's total/extended amount).
        total_discount: The "Total Discount" amount from the document's
            totals block, exactly as written. "" if not stated.
        total_excl_vat: The "Total (Excl. VAT)" amount from the totals
            block, exactly as written. "" if not stated.
        total_vat: The "Total VAT" amount from the totals block, exactly as
            written. "" if not stated.
        total_incl_vat: The "Total (Incl. VAT)" / grand total amount from
            the totals block, exactly as written. "" if not stated.
        amount_in_words: The grand total spelled out in words, exactly as
            written (e.g. "Seven thousand five hundred six and 35/100 AED
            ONLY"). "" if not stated.
    """
    return "Recorded"


_LPO_DETAILS_PROMPT = (
    "The content below is a customer's Purchase Order / LPO document (text "
    "extracted from the PDF, or its page images if it's a scanned document "
    "with no text layer). Read its line-items table and totals block, and "
    "record everything via submit_lpo_details."
)


def extract_lpo_details(pdf_text: str, page_images: list = None) -> dict:
    """Best-effort re-extraction of just the line-items table and totals
    block from an already-processed LPO's source PDF -- used by the
    backfill_lpo_items management command to populate the item-level
    extra_description/discount_percent/vat_amount/amount and the
    LPORequest-level total_discount/total_excl_vat/total_vat/
    amount_in_words fields added after those rows were first created (see
    migration 0024), without re-running the full classify_email triage
    (which could also change is_lpo/matching side effects on a row that's
    already been reviewed/converted). Never raises; returns {'items': [],
    ...} with empty items on any failure."""
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=settings.EMAILAGENT_CLAUDE_TIMEOUT_SECS)
    content = [{"type": "text", "text": f"{_LPO_DETAILS_PROMPT}\n\n{pdf_text}"}]
    for img in (page_images or []):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(img).decode('utf-8')},
        })

    captured = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            max_tokens=8000,
            thinking={"type": "disabled"},
            tools=[submit_lpo_details],
            messages=[{"role": "user", "content": content}],
            max_iterations=3,
        )
        for message in runner:
            for block in message.content:
                if block.type == "tool_use" and block.name == "submit_lpo_details":
                    captured = block.input
                    break
            if captured is not None:
                break
    except anthropic.APIError as exc:
        logger.warning(f"extract_lpo_details agent loop API error: {exc!r}")
    except Exception:
        logger.exception("extract_lpo_details agent loop unexpected failure")

    if captured is None:
        return {'items': [], 'total_discount': '', 'total_excl_vat': '', 'total_vat': '',
                'total_incl_vat': '', 'amount_in_words': ''}
    return {
        'items': captured.get('items', []) or [],
        'total_discount': captured.get('total_discount', '') or '',
        'total_excl_vat': captured.get('total_excl_vat', '') or '',
        'total_vat': captured.get('total_vat', '') or '',
        'total_incl_vat': captured.get('total_incl_vat', '') or '',
        'amount_in_words': captured.get('amount_in_words', '') or '',
    }


def decide_status(category: str, confidence: float) -> str:
    from emailagent.models import TrackedEmail

    threshold = settings.EMAILAGENT_CONFIDENCE_THRESHOLD
    if category == CATEGORY_UNCERTAIN:
        return TrackedEmail.STATUS_NEEDS_REVIEW
    if category == CATEGORY_RFQ and confidence >= threshold:
        return TrackedEmail.STATUS_RFQ
    if category == CATEGORY_NOT_RELEVANT and confidence >= threshold:
        return TrackedEmail.STATUS_NOT_RELEVANT
    return TrackedEmail.STATUS_NEEDS_REVIEW
