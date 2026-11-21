"""TCVN3 (.VnTime) to Unicode UTF-8 converter for Vietnam GIS (.dbf/.shp/.dgn).

Strictly distinguishes legacy TCVN3 bytes from valid Vietnamese UTF-8 (including Latin-1 accented
characters like 'à', 'á', 'è', 'é', 'ê', 'ô', 'ù' so valid UTF-8 strings like 'Sông Bé' or 'Chùa'
are NEVER corrupted).
"""

from __future__ import annotations

TCVN3_MAP = {
    "\u00b8": "á", "\u00b5": "à", "\u00b6": "ả", "\u00b7": "ã", "\u00b9": "ạ",
    "\u00a8": "ă", "\u00be": "ắ", "\u00bb": "ằ", "\u00bc": "ẳ", "\u00bd": "ẵ", "\u00c6": "ặ",
    "\u00a9": "â", "\u00ca": "ấ", "\u00c7": "ầ", "\u00c8": "ẩ", "\u00c9": "ẫ", "\u00cb": "ậ",
    "\u00d0": "é", "\u00cc": "è", "\u00ce": "ẻ", "\u00cf": "ẽ", "\u00d1": "ẹ",
    "\u00aa": "ê", "\u00d5": "ế", "\u00d2": "ề", "\u00d3": "ể", "\u00d4": "ễ", "\u00d6": "ệ",
    "\u00dd": "í", "\u00d7": "ì", "\u00d8": "ỉ", "\u00dc": "ĩ", "\u00de": "ị",
    "\u00e3": "ó", "\u00df": "ò", "\u00e1": "ỏ", "\u00e2": "õ", "\u00e4": "ọ",
    "\u00ab": "ô", "\u00e8": "ố", "\u00e5": "ồ", "\u00e6": "ổ", "\u00e7": "ỗ", "\u00e9": "ộ",
    "\u00ac": "ơ", "\u00ed": "ớ", "\u00ea": "ờ", "\u00eb": "ở", "\u00ec": "ỡ", "\u00ee": "ợ",
    "\u00f3": "ú", "\u00ef": "ù", "\u00f1": "ủ", "\u00f2": "ũ", "\u00f4": "ụ",
    "\u00ad": "ư", "\u00f8": "ứ", "\u00f5": "ừ", "\u00f6": "ử", "\u00f7": "ữ", "\u00f9": "ự",
    "\u00fd": "ý", "\u00fa": "ỳ", "\u00fb": "ỷ", "\u00fc": "ỹ", "\u00fe": "ỵ",
    "\u00ae": "đ",
    "\u00a1": "Ă", "\u00a2": "Â", "\u00a3": "Ê", "\u00a4": "Ô", "\u00a5": "Ơ", "\u00a6": "Ư", "\u00a7": "Đ",
}

# ONLY characters that NEVER occur in valid Vietnamese UTF-8 text (excludes è, é, ê, ô, ù, ú, à, á, ò, ó, etc.)
STRICT_TCVN3_SIGNATURES = set(
    "\u00a1\u00a2\u00a3\u00a4\u00a5\u00a6\u00a7\u00a8\u00a9\u00aa\u00ab\u00ac\u00ad\u00ae"
    "\u00b5\u00b6\u00b7\u00b8\u00b9\u00bb\u00bc\u00bd\u00be"
    "\u00c6\u00c7\u00cb\u00d0\u00d1\u00d6\u00d7\u00d8\u00dc\u00de\u00df"
    "\u00e4\u00e5\u00e6\u00e7\u00eb\u00ee\u00ef\u00f1\u00f6\u00f7\u00f8\u00fb\u00fc\u00fe"
)

VIET_UNICODE_EXTENDED = set(
    "ăâđêôơưĂÂĐÊÔƠƯáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"
)


def is_likely_tcvn3(text: str) -> bool:
    """Return True only if text contains strict TCVN3 markers and NO extended Vietnamese Unicode."""
    if not text or not isinstance(text, str):
        return False
    if any(ord(c) > 0x00FF and c in VIET_UNICODE_EXTENDED for c in text):
        return False
    return any(c in STRICT_TCVN3_SIGNATURES for c in text)


def tcvn3_to_unicode(text: str) -> str:
    """Convert TCVN3 string (read as latin-1) to clean UTF-8 Unicode."""
    if not text or not isinstance(text, str):
        return text
    return "".join(TCVN3_MAP.get(ch, ch) for ch in text)


def smart_fix_text(text: str) -> tuple[str, bool]:
    """Auto-detect and convert TCVN3 to Unicode while preserving valid UTF-8 text."""
    if not text or not isinstance(text, str):
        return text, False
    if is_likely_tcvn3(text):
        return tcvn3_to_unicode(text), True
    return text, False
