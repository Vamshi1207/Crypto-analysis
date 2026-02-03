from flask import Flask, request, jsonify
import base64
import io
from PIL import Image
import numpy as np
import cv2
import pytesseract

pytesseract.pytesseract.tesseract_cmd = r"C:\Users\vamsh\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"


app = Flask(__name__)

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    image_b64 = data.get('image')
    
    if not image_b64:
        return jsonify({"error": "No image provided"}), 400
        

    # Decode base64 to image
    image_bytes = base64.b64decode(image_b64)
    img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    img_np = np.array(img_pil)
    img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

    # OPTIONAL: preprocessing
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)

    cv2.imwrite("cropped_debug.png", thresh)
    print("Saved debug image: cropped_debug.png")
    
    # OCR
    text = pytesseract.image_to_string(thresh, config='--psm 6')
    print("OCR Result:")
    print(text)

    # Parse out indicator values
    results = parse_indicators(text)
    
    return jsonify(results)

def parse_indicators(text):
    lines = text.split('\n')
    data = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if 'BB %B' in line:
            parts = line.split()
            data['bb_percent_b'] = parts[-1]
        if 'RSI' in line:
            parts = line.split()
            data['rsi'] = parts[-1]
        if 'MACD' in line:
            parts = line.split()
            data['macd'] = parts[-3:]
    return data

if __name__ == '__main__':
    app.run(port=5000)
