"""Amounts in words for the quotation total, e.g. 1,107.75 -> ONE THOUSAND ONE HUNDRED AND SEVEN AND SEVENTY FIVE
FILS ONLY."""
from decimal import ROUND_HALF_UP, Decimal

_ONES = ['ZERO', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE', 'TEN', 'ELEVEN', 'TWELVE',
         'THIRTEEN', 'FOURTEEN', 'FIFTEEN', 'SIXTEEN', 'SEVENTEEN', 'EIGHTEEN', 'NINETEEN']
_TENS = ['', '', 'TWENTY', 'THIRTY', 'FORTY', 'FIFTY', 'SIXTY', 'SEVENTY', 'EIGHTY', 'NINETY']
_SCALES = [(10 ** 9, 'BILLION'), (10 ** 6, 'MILLION'), (10 ** 3, 'THOUSAND')]


def _below_thousand(n):
    words = []
    if n >= 100:
        words.append(_ONES[n // 100] + ' HUNDRED')
        n %= 100
        if n:
            words.append('AND')
    if n >= 20:
        words.append(_TENS[n // 10] + (' ' + _ONES[n % 10] if n % 10 else ''))
    elif n or not words:
        words.append(_ONES[n])
    return ' '.join(words)


def number_to_words(n):
    """A whole number in capital English words (up to the billions)."""
    n = int(n)
    if n == 0:
        return 'ZERO'
    parts = []
    for size, name in _SCALES:
        if n >= size:
            parts.append(_below_thousand(n // size) + ' ' + name)
            n %= size
    if n:
        # "ONE THOUSAND AND FIVE", but "ONE THOUSAND ONE HUNDRED" (the AND only joins the last group when it is
        # below one hundred)
        if parts and n < 100:
            parts.append('AND')
        parts.append(_below_thousand(n))
    return ' '.join(parts)


def amount_in_words(amount, currency='', minor='FILS'):
    """e.g. Decimal('1107.75') -> 'ONE THOUSAND ONE HUNDRED AND SEVEN AND SEVENTY FIVE FILS ONLY'.

    `currency` (e.g. 'UAE DIRHAMS') is put in front only when given; the quotation prints the words without it.
    """
    amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    whole, fils = divmod(int(amount * 100), 100)
    text = f'{currency} {number_to_words(whole)}'.strip()
    if fils:
        text += f' AND {number_to_words(fils)} {minor}'
    return text + ' ONLY'
