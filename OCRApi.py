import requests
import base64
import json

# Use the endpoint you discovered
url = "http://localhost:8085/layout-parsing"
image_path = r"C:\Users\njwye\Documents\py\UndetectedSelenium\Downloads\[賴氏族譜][不分卷] - Chiayi. Chinese Clan Genealogy Records 1857 pipe Raoping\0026.jpg"

with open(image_path, "rb") as f:
    img_str = base64.b64encode(f.read()).decode('utf-8')

# PaddleX layout-parsing usually expects this structure
payload = {
    "file": img_str,
    "fileType": 1  # 1 indicates an image
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    data = response.json()
    # The result usually contains 'doc_content' (the text/markdown) 
    # and 'layout_result' (coordinates of boxes)
    print(json.dumps(data))
else:
    print(f"Error {response.status_code}: {response.text}")