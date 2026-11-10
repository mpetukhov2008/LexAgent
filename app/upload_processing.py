"""Bounded validation, extraction and OCR for locally uploaded documents."""

import asyncio
import csv
import io
import os
import struct
from pathlib import Path

from app.agent import AgentError
from app.pdf_worker import ocr_ready, tesseract_path

MAX_BYTES = 10_000_000
MAX_PIXELS = 12_000_000


def _jpeg_size(data):
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset:offset + 2], "big")
        if size < 2 or offset + size > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if size < 7:
                break
            return (int.from_bytes(data[offset + 3:offset + 5], "big"),
                    int.from_bytes(data[offset + 5:offset + 7], "big"))
        offset += size
    raise AgentError("Некорректный JPEG")


def validate_upload(data, filename):
    if not data or len(data) > MAX_BYTES:
        raise AgentError("Файл пуст или превышает 10 МБ")
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise AgentError("Файл не является PDF")
        return ext
    if ext == ".png":
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise AgentError("Файл не является PNG")
        width, height = struct.unpack(">II", data[16:24])
    elif ext in (".jpg", ".jpeg"):
        if not data.startswith(b"\xff\xd8\xff"):
            raise AgentError("Файл не является JPEG")
        width, height = _jpeg_size(data)
    else:
        raise AgentError("Поддерживаются PDF, JPG и PNG")
    if not (0 < width <= 12000 and 0 < height <= 12000 and width * height <= MAX_PIXELS):
        raise AgentError("Изображение превышает допустимое разрешение")
    return ext


async def extract_image(path, settings):
    if not settings.enable_ocr or not await asyncio.to_thread(
        ocr_ready, settings.tesseract_cmd, settings.ocr_languages
    ):
        raise AgentError("OCR недоступен. Проверьте Tesseract в разделе «Подключения»")
    executable = tesseract_path(settings.tesseract_cmd)
    tessdata = Path(executable).resolve().parent / "tessdata"
    args = [executable, str(path), "stdout", "-l", settings.ocr_languages]
    if tessdata.is_dir():
        args.extend(["--tessdata-dir", str(tessdata)])
    args.append("tsv")
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(path.parent), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=min(settings.pdf_timeout, settings.ocr_page_timeout)
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AgentError("Превышено время OCR изображения") from None
    if process.returncode or len(stdout) > 3_000_000:
        raise AgentError("Не удалось распознать изображение")
    try:
        records = csv.DictReader(io.StringIO(stdout.decode("utf-8")), delimiter="\t")
        lines, words, current, confidences = [], [], None, []
        for record in records:
            value = (record.get("text") or "").strip()
            if not value:
                continue
            line = (record.get("block_num"), record.get("par_num"), record.get("line_num"))
            if current is not None and current != line:
                lines.append(" ".join(words))
                words = []
            current = line
            words.append(value)
            try:
                score = float(record.get("conf", "-1"))
                if 0 <= score <= 100:
                    confidences.append(score)
            except ValueError:
                pass
        if words:
            lines.append(" ".join(words))
        text = "\n".join(lines)
    except (UnicodeDecodeError, ValueError, csv.Error):
        raise AgentError("OCR вернул повреждённый текст") from None
    if not text.strip():
        raise AgentError("Не удалось найти текст на изображении")
    confidence = round(sum(confidences) / len(confidences), 2) if confidences else None
    return {"pages": [{"page": 1, "text": text}],
            "page_quality": [{"page": 1, "origin": "ocr", "status": "requires_review",
                              "ocr_confidence": confidence, "requires_review": True}]}


def decode_upload(result):
    pages = result.get("pages", [])
    if not isinstance(pages, list) or not pages:
        raise AgentError("Документ не содержит читаемых страниц")
    text = "\n\n".join(p.get("text", "") for p in pages if isinstance(p, dict))
    if not text.strip():
        raise AgentError("Текст не найден. Проверьте OCR или качество скана")
    quality = result.get("page_quality", [])
    return {"text": text[:12000], "pages": len(pages),
            "ocr_used": any(q.get("origin") == "ocr" for q in quality),
            "truncated": len(text) > 12000, "review_required": True}
