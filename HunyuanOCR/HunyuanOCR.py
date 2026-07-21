import base64
import math
import mimetypes
import re
from io import BytesIO

from openai import OpenAI, BadRequestError
from PIL import Image

client = OpenAI(
    api_key="EMPTY",
    base_url="http://127.0.0.1:9000/v1"
)

MAX_RESIZE_ATTEMPTS = 5
RESIZE_MARGIN = 0.95


def encode_image(path, scale=1.0):
    mime_type, _ = mimetypes.guess_type(path)

    if mime_type is None or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    with Image.open(path) as image:
        image.load()

        if scale < 1.0:
            new_size = (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )

            print(f"Resizing {image.size} -> {new_size}")

            image = image.resize(
                new_size,
                Image.Resampling.LANCZOS
            )

        if image.mode != "RGB":
            image = image.convert("RGB")

        buffer = BytesIO()

        image.save(
            buffer,
            format="JPEG",
            quality=95
        )

        b64_data = base64.b64encode(
            buffer.getvalue()
        ).decode("utf-8")

    return f"data:image/jpeg;base64,{b64_data}"


def extract_mm_token_error(error):
    message = str(error)

    match = re.search(
        r"image item with (\d+) embedding tokens, "
        r"which exceeds the pre-allocated encoder cache size (\d+)",
        message
    )

    if not match:
        return None

    return int(match.group(1)), int(match.group(2))


def extractText(image_path):
    print("Starting OCR for image:", image_path)

    scale = 1.0

    for attempt in range(MAX_RESIZE_ATTEMPTS):
        data_uri = encode_image(image_path, scale)

        try:
            response = client.chat.completions.create(
                model="tencent/HunyuanOCR",
                max_tokens=8192,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_uri
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

            result = response.choices[0].message.content

            print("OCR Result:", result)

            return result

        except BadRequestError as error:
            token_error = extract_mm_token_error(error)

            if token_error is None:
                raise

            actual_tokens, cache_size = token_error

            print(
                f"Image too large: {actual_tokens} tokens "
                f"(cache limit: {cache_size})"
            )

            resize_factor = math.sqrt(
                cache_size / actual_tokens
            ) * RESIZE_MARGIN

            scale *= resize_factor

            print(
                f"Retrying with scale {scale:.4f} "
                f"(attempt {attempt + 2}/{MAX_RESIZE_ATTEMPTS})"
            )

    raise RuntimeError(
        f"Failed to resize image within "
        f"{MAX_RESIZE_ATTEMPTS} attempts"
    )