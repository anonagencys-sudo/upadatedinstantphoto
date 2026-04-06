from flask import Flask, request, render_template, send_file, jsonify
from PIL import Image, ImageOps
from io import BytesIO
from dotenv import load_dotenv
from flask_cors import CORS
import requests
import os
import random

load_dotenv()  # must be called before any os.getenv() for .env values to be available

app = Flask(__name__)
CORS(app)

# Keys are read lazily so Railway env vars (set after module load) are always picked up
_REMOVE_BG_KEY_NAMES = ["REMOVE_BG_KEY_1", "REMOVE_BG_KEY_2", "REMOVE_BG_KEY_3", "REMOVE_BG_KEY_4"]

def get_remove_bg_key():
    available = [os.getenv(k) for k in _REMOVE_BG_KEY_NAMES if os.getenv(k)]
    if not available:
        raise ValueError("No remove.bg API keys configured")
    return random.choice(available)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/aadhaar-scanner")
def aadhaar_scanner():
    return render_template("aadhaar-scanner.html")


@app.route("/pan-scanner")
def pan_scanner():
    return render_template("pan-scanner.html")


def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return (255, 255, 255)
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def process_single_image(input_image_bytes, bg_color_rgb=(255, 255, 255)):
    """Remove background and apply chosen background colour."""

    response = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        files={"image_file": input_image_bytes},
        data={"size": "auto"},
        headers={"X-Api-Key": get_remove_bg_key()},
    )

    if response.status_code != 200:
        try:
            error_info = response.json()
            if error_info.get("errors"):
                error_code = error_info["errors"][0].get("code", "unknown_error")
                raise ValueError(f"bg_removal_failed:{error_code}:{response.status_code}")
        except ValueError:
            raise
        except Exception:
            pass
        raise ValueError(f"bg_removal_failed:unknown:{response.status_code}")

    img = Image.open(BytesIO(response.content))

    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, bg_color_rgb)
        background.paste(img, mask=img.split()[-1])
        passport_img = background
    else:
        passport_img = img.convert("RGB")

    return passport_img


@app.route("/process", methods=["POST"])
def process():
    print("==== /process endpoint hit ====")

    passport_width = int(request.form.get("width", 390))
    passport_height = int(request.form.get("height", 480))
    border = int(request.form.get("border", 2))
    spacing = int(request.form.get("spacing", 10))
    bg_color_hex = request.form.get("bg_color", "#FFFFFF")
    bg_color_rgb = hex_to_rgb(bg_color_hex)
    margin_x = 60
    margin_y = 60
    horizontal_gap = 15
    a4_w, a4_h = 2480, 3508

    images_data = []

    i = 0
    while f"image_{i}" in request.files:
        file = request.files[f"image_{i}"]
        copies = int(request.form.get(f"copies_{i}", 6))
        images_data.append((file.read(), copies))
        i += 1

    if not images_data and "image" in request.files:
        file = request.files["image"]
        copies = int(request.form.get("copies", 6))
        images_data.append((file.read(), copies))

    if not images_data:
        return "No image uploaded", 400

    print(f"DEBUG: Processing {len(images_data)} image(s)")

    passport_images = []
    for idx, (img_bytes, copies) in enumerate(images_data):
        print(f"DEBUG: Processing image {idx + 1} with {copies} copies")
        try:
            img = process_single_image(img_bytes, bg_color_rgb)
            img = img.resize((passport_width, passport_height), Image.LANCZOS)
            img = ImageOps.expand(img, border=border, fill="black")
            passport_images.append((img, copies))
        except ValueError as e:
            err_str = str(e)
            if "410" in err_str or "face" in err_str.lower():
                return jsonify({"error": "face_detection_failed"}), 410
            elif "429" in err_str or "quota" in err_str.lower():
                return jsonify({"error": "quota_exceeded"}), 429
            else:
                print(err_str)
                return jsonify({"error": err_str}), 500

    paste_w = passport_width + 2 * border
    paste_h = passport_height + 2 * border

    pages = []
    current_page = Image.new("RGB", (a4_w, a4_h), "white")
    x, y = margin_x, margin_y

    def new_page():
        nonlocal current_page, x, y
        pages.append(current_page)
        current_page = Image.new("RGB", (a4_w, a4_h), "white")
        x, y = margin_x, margin_y

    for passport_img, copies in passport_images:
        for _ in range(copies):
            if x + paste_w > a4_w - margin_x:
                x = margin_x
                y += paste_h + spacing
            if y + paste_h > a4_h - margin_y:
                new_page()
            current_page.paste(passport_img, (x, y))
            print(f"DEBUG: Placed at x={x}, y={y}")
            x += paste_w + horizontal_gap

    pages.append(current_page)
    print(f"DEBUG: Total pages = {len(pages)}")

    output = BytesIO()
    if len(pages) == 1:
        pages[0].save(output, format="PDF", dpi=(300, 300))
    else:
        pages[0].save(
            output,
            format="PDF",
            dpi=(300, 300),
            save_all=True,
            append_images=pages[1:],
        )
    output.seek(0)

    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="passport-sheet.pdf",
    )


def crop_card_from_image(img):
    """
    Detect card edges using perspective detection and crop to card shape.
    Falls back to intelligent centre crop at 85.6x54mm (credit card) ratio if detection fails.
    """
    CARD_RATIO = 85.6 / 54.0  # standard ID card ratio (landscape)

    try:
        import cv2
        import numpy as np

        img_rgb = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 30, 120)
        edges = cv2.dilate(edges, None, iterations=2)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        card_contour = None
        img_area = img_rgb.shape[0] * img_rgb.shape[1]

        for c in contours[:5]:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            area = cv2.contourArea(c)
            if len(approx) == 4 and area > img_area * 0.1:
                card_contour = approx
                break

        if card_contour is not None:
            pts = card_contour.reshape(4, 2).astype("float32")
            # Order: top-left, top-right, bottom-right, bottom-left
            s = pts.sum(axis=1)
            diff = np.diff(pts, axis=1)
            ordered = np.array([pts[np.argmin(s)], pts[np.argmin(diff)],
                                pts[np.argmax(s)], pts[np.argmax(diff)]], dtype="float32")

            widthA = np.linalg.norm(ordered[2] - ordered[3])
            widthB = np.linalg.norm(ordered[1] - ordered[0])
            maxW = int(max(widthA, widthB))
            heightA = np.linalg.norm(ordered[1] - ordered[2])
            heightB = np.linalg.norm(ordered[0] - ordered[3])
            maxH = int(max(heightA, heightB))

            if maxW > 0 and maxH > 0:
                dst = np.array([[0, 0], [maxW - 1, 0], [maxW - 1, maxH - 1], [0, maxH - 1]], dtype="float32")
                M = cv2.getPerspectiveTransform(ordered, dst)
                warped = cv2.warpPerspective(img_rgb, M, (maxW, maxH))
                result = Image.fromarray(warped)
                # Ensure landscape
                if result.height > result.width:
                    result = result.rotate(90, expand=True)
                return result

    except ImportError:
        pass

    # Fallback: centre crop to card ratio
    orig_w, orig_h = img.size
    orig_ratio = orig_w / orig_h

    if orig_ratio > CARD_RATIO:
        new_w = int(orig_h * CARD_RATIO)
        left = (orig_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, orig_h))
    else:
        new_h = int(orig_w / CARD_RATIO)
        top = (orig_h - new_h) // 2
        img = img.crop((0, top, orig_w, top + new_h))

    return img


def build_card_sheet(card_images, copies, card_w_px, card_h_px, filename):
    """
    Place `copies` of each card image on A4 pages (300 DPI).
    card_w_px / card_h_px = target size in pixels.
    Returns BytesIO PDF.
    """
    A4_W, A4_H = 2480, 3508
    MARGIN = 80
    GAP = 30

    pages = []
    current_page = Image.new("RGB", (A4_W, A4_H), "white")
    x, y = MARGIN, MARGIN

    def new_page():
        nonlocal current_page, x, y
        pages.append(current_page)
        current_page = Image.new("RGB", (A4_W, A4_H), "white")
        x, y = MARGIN, MARGIN

    for card_img in card_images:
        for _ in range(copies):
            if x + card_w_px > A4_W - MARGIN:
                x = MARGIN
                y += card_h_px + GAP
            if y + card_h_px > A4_H - MARGIN:
                new_page()
            current_page.paste(card_img, (x, y))
            x += card_w_px + GAP

    pages.append(current_page)

    output = BytesIO()
    if len(pages) == 1:
        pages[0].save(output, format="PDF", dpi=(300, 300))
    else:
        pages[0].save(output, format="PDF", dpi=(300, 300), save_all=True, append_images=pages[1:])
    output.seek(0)
    return output


@app.route("/process-aadhaar", methods=["POST"])
def process_aadhaar():
    print("==== /process-aadhaar hit ====")

    front_file = request.files.get("front")
    back_file = request.files.get("back")
    if not front_file or not back_file:
        return "Missing front or back image", 400

    copies = int(request.form.get("copies", 4))

    # Aadhaar card: 85.6mm x 54mm at 300 DPI = 1012 x 638 px (landscape)
    CARD_W = 1012
    CARD_H = 638

    try:
        front_img = Image.open(front_file.stream).convert("RGB")
        back_img = Image.open(back_file.stream).convert("RGB")

        front_card = crop_card_from_image(front_img).resize((CARD_W, CARD_H), Image.LANCZOS)
        back_card = crop_card_from_image(back_img).resize((CARD_W, CARD_H), Image.LANCZOS)

        # Add thin border
        front_card = ImageOps.expand(front_card, border=2, fill="black")
        back_card = ImageOps.expand(back_card, border=2, fill="black")

        # Place front copies then back copies (photocopy style — all fronts then all backs)
        card_w_px = CARD_W + 4
        card_h_px = CARD_H + 4

        output = build_card_sheet(
            [front_card] * copies + [back_card] * copies,
            1, card_w_px, card_h_px, "aadhaar-sheet.pdf"
        )

        return send_file(output, mimetype="application/pdf", as_attachment=True, download_name="aadhaar-sheet.pdf")

    except Exception as e:
        print(f"Aadhaar error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/process-pan", methods=["POST"])
def process_pan():
    print("==== /process-pan hit ====")

    pan_file = request.files.get("pan")
    if not pan_file:
        return "Missing PAN image", 400

    copies = int(request.form.get("copies", 4))

    # PAN card: 85.6mm x 54mm at 300 DPI = 1012 x 638 px (landscape)
    CARD_W = 1012
    CARD_H = 638

    try:
        pan_img = Image.open(pan_file.stream).convert("RGB")
        pan_card = crop_card_from_image(pan_img).resize((CARD_W, CARD_H), Image.LANCZOS)
        pan_card = ImageOps.expand(pan_card, border=2, fill="black")

        card_w_px = CARD_W + 4
        card_h_px = CARD_H + 4

        output = build_card_sheet([pan_card], copies, card_w_px, card_h_px, "pan-sheet.pdf")

        return send_file(output, mimetype="application/pdf", as_attachment=True, download_name="pan-sheet.pdf")

    except Exception as e:
        print(f"PAN error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
