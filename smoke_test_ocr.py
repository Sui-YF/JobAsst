from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from jd_ocr import extract_jd_text


def make_image(text: str) -> bytes:
    image = Image.new("RGB", (1000, 180), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 42)
    draw.text((30, 50), text, fill="black", font=font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    result = extract_jd_text([
        ("01.png", make_image("AI Product Manager")),
        ("02.png", make_image("Bachelor degree required")),
    ])
    print(result)
    normalized = " ".join(result.lower().split())
    assert "product manager" in normalized
    assert "bachelor degree required" in normalized
    print("OCR smoke test passed")
