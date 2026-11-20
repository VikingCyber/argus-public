import re

# OCR may return visually identical Latin letters for Russian plate symbols.
_LATIN_TO_CYRILLIC = str.maketrans("ABEKMHOPCTXY", "АВЕКМНОРСТУХ")


def normalize_plate(value: str) -> str:
    """Return a comparable Russian plate string."""
    if not value:
        return ""
    value = value.upper().translate(_LATIN_TO_CYRILLIC)
    return re.sub(r"[\s.\-_]", "", value)
