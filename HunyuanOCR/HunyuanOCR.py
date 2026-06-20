import base64
from openai import OpenAI
import mimetypes

client = OpenAI(api_key="EMPTY", base_url="http://localhost:9000/v1")

def encode_image(path):
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None or not mime_type.startswith("image/"):
        # fallback if mimetypes can't figure it out (rare, e.g. unusual extensions)
        mime_type = "image/jpeg"

    with open(path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{b64_data}"

def extractText(image_path):
    data_uri = encode_image(image_path)

    response = client.chat.completions.create(
        model="tencent/HunyuanOCR",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": "This is a Chinese genealogy document (族譜). Transcribe all text exactly in Traditional Chinese. Preserve original order, line breaks, and punctuation. Mark uncertain characters with []. Output plain text only."}
            ]
        }],
        extra_body={
            #"repetition_penalty": 1.1
        }
    )
    print(response)
    return response.choices[0].message.content