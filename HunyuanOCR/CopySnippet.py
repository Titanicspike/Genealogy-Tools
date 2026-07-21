import sys
import time
from openai import OpenAI, BadRequestError
import base64
import mimetypes
import subprocess
import unicodedata

def clean_ocr_text(text):
    # Explicitly remove common invisible characters
    invisible_chars = {
        "\uFEFF",  # BOM / zero-width no-break space
        "\u200B",  # zero-width space
        "\u200C",  # zero-width non-joiner
        "\u200D",  # zero-width joiner
        "\u2060",  # word joiner
        "\u00AD",  # soft hyphen
    }

    text = "".join(c for c in text if c not in invisible_chars)

    # Remove other Unicode formatting/control characters,
    # while preserving normal whitespace
    text = "".join(
        c for c in text
        if unicodedata.category(c) not in {"Cf", "Cc"}
        or c in "\n\r\t"
    )

    return text

def encode_image(path):
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None or not mime_type.startswith("image/"):
        # fallback if mimetypes can't figure it out (rare, e.g. unusual extensions)
        mime_type = "image/jpeg"

    with open(path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{b64_data}"

client = OpenAI(
    api_key="EMPTY",
    base_url="http://127.0.0.1:9000/v1"
)
response = client.chat.completions.create(
    model="tencent/HunyuanOCR",
    max_tokens=8192,
    messages=[{
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": encode_image(sys.argv[1])
                }
            },
            {
                "type": "text",
                "text": (
                    "這是一份中文族譜，可能包含直排、橫排、"
                    "印刷及手寫文字。請依照原有閱讀順序，"
                    "以繁體中文逐字轉錄所有文字，保留換行及標點。"
                    "無法確定的字請以 [ ] 標示。只輸出純文字。"
                )
            }
        ]
    }],
    extra_body={
        "repetition_penalty": 1.3,
        "temperature": 0.2,
    }
)

result = clean_ocr_text(response.choices[0].message.content)
print(result)
print([(c, f"U+{ord(c):04X}") for c in result])
b64 = base64.b64encode(result.encode("utf-16-le")).decode("ascii")
cmd = (
    f'$bytes=[Convert]::FromBase64String("{b64}"); '
    f'$text=[Text.Encoding]::Unicode.GetString($bytes); '
    f'Set-Clipboard -Value $text'
)
subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], check=True)