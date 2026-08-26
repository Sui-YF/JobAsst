from __future__ import annotations

from io import BytesIO
from pathlib import Path
import os


class OCRError(RuntimeError):
    pass


def _result_lines(result) -> list[str]:
    if result is None:
        return []
    txts = getattr(result, "txts", None)
    if txts is not None:
        return [str(text).strip() for text in txts if str(text).strip()]
    # Compatibility with older RapidOCR tuple/list output.
    raw = result[0] if isinstance(result, tuple) else result
    lines = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            text_part = item[1]
            if isinstance(text_part, (list, tuple)):
                text_part = text_part[0]
            if str(text_part).strip():
                lines.append(str(text_part).strip())
    return lines


def extract_jd_text(images: list[tuple[str, bytes]]) -> str:
    if not images:
        return ""
    try:
        import numpy as np
        from PIL import Image
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise OCRError("OCR增强能力当前不可用。你仍可直接粘贴或编辑JD文本；如需OCR，请安装 requirements-ocr.txt。") from exc

    try:
        engine = RapidOCR()
        sections = []
        for index, (filename, content) in enumerate(images, start=1):
            if Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise OCRError("JD截图仅支持PNG、JPG、JPEG或WEBP。")
            if len(content) > max(1, int(os.getenv("MAX_UPLOAD_MB", "10"))) * 1024 * 1024:
                raise OCRError("JD截图超过上传大小限制。")
            image = Image.open(BytesIO(content)).convert("RGB")
            result = engine(np.asarray(image))
            lines = _result_lines(result)
            if lines:
                sections.append(f"【截图{index}：{filename}】\n" + "\n".join(lines))
        if not sections:
            raise OCRError("没有从截图中识别到文字。请检查图片清晰度或手动粘贴JD。")
        return "\n\n".join(sections)
    except OCRError:
        raise
    except Exception as exc:
        raise OCRError(f"截图文字识别失败：{exc}") from exc
