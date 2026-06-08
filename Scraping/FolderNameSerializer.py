import re
import unicodedata

def SerializeFolderName(text: str) -> str:
    """
    Convert any string into a human-readable, Windows-safe folder name.
    Replaces symbols with meaningful words where possible.
    """

    # Normalize unicode (é -> e, etc.)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))

    # Symbol replacements (human readable)
    replacements = {
        "&": " and ",
        "@": " at ",
        "%": " percent ",
        "$": " dollar ",
        "#": " number ",
        "+": " plus ",
        "=": " equals ",
        "<": " less than ",
        ">": " greater than ",
        "*": " star ",
        "/": " or ",
        "\\": " or ",
        "|": " pipe ",
        ":": " - ",
        '"': "",
        "?": "",
        "!": "",
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    # Remove any remaining invalid Windows filename characters
    text = re.sub(r'[<>:"/\\|?*]', " ", text)

    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Remove trailing dots or spaces (Windows rule)
    text = text.rstrip(" .")

    # Avoid Windows reserved names
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if text.upper() in reserved:
        text = f"{text} folder"

    return text or "folder"