"""Disposable PDF/OCR process. Paths and executable are supplied only by trusted code."""

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium


def tesseract_path(command="tesseract"):
    found = shutil.which(command)
    if found:
        return found
    local = Path(__file__).resolve().parents[1] / "data/ocr-engine-5.5.3/tesseract.exe"
    if command == "tesseract" and local.is_file():
        return str(local)
    if os.name == "nt" and command == "tesseract":
        candidate = (
            Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Tesseract-OCR/tesseract.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return None


def ocr_ready(command="tesseract", languages="rus+eng"):
    executable = tesseract_path(command)
    if not executable or not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3}){0,3}", languages):
        return False
    try:
        result = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return result.returncode == 0 and set(languages.split("+")).issubset(
            set(result.stdout.splitlines())
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def recognize(bitmap, executable, languages, timeout):
    with tempfile.TemporaryDirectory(prefix="lexagent-ocr-") as directory:
        target = Path(directory) / "page.pgm"
        if bitmap.mode != "L" or bitmap.width * bitmap.height > 12_000_000:
            raise ValueError("Unexpected OCR bitmap")
        with target.open("wb") as stream:
            stream.write(f"P5\n{bitmap.width} {bitmap.height}\n255\n".encode("ascii"))
            for row in range(bitmap.height):
                start = row * bitmap.stride
                stream.write(bytes(bitmap.buffer[start : start + bitmap.width]))
        result = subprocess.run(
            [executable, str(target), "stdout", "-l", languages, "--psm", "3", "tsv"],
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode:
            raise ValueError("OCR engine failed")
        words, confidences, lines, current = [], [], [], None
        for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"):
            text = (row.get("text") or "").strip()
            if not text:
                continue
            line = (row.get("block_num"), row.get("par_num"), row.get("line_num"))
            if current is not None and line != current:
                lines.append(" ".join(words))
                words = []
            current = line
            words.append(text)
            try:
                confidence = float(row["conf"])
                if 0 <= confidence <= 100:
                    confidences.append(confidence)
            except (ValueError, KeyError):
                pass
        if words:
            lines.append(" ".join(words))
        return "\n".join(lines), (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        )


def process(path, *, enable_ocr=True, command="tesseract", languages="rus+eng", timeout=20):
    pages, quality = [], []
    ready = enable_ocr and ocr_ready(command, languages)
    with pdfium.PdfDocument(path) as document:
        if not 0 < len(document) <= 150:
            raise ValueError("PDF page limit")
        for index in range(len(document)):
            with closing(document[index]) as page:
                with closing(page.get_textpage()) as textpage:
                    text = textpage.get_text_range().replace("\r\n", "\n")
                origin, confidence, status = "pdf_text", None, "unassessed"
                if not text.strip():
                    origin, status = "unknown", "ocr_unavailable" if not ready else "ocr_failed"
                    if ready:
                        width, height = page.get_size()
                        scale = min(3, (12_000_000 / max(1, width * height)) ** 0.5)
                        with closing(page.render(scale=scale, grayscale=True)) as bitmap:
                            try:
                                text, confidence = recognize(
                                    bitmap,
                                    tesseract_path(command),
                                    languages,
                                    timeout,
                                )
                                origin, status = "ocr", "requires_review"
                            except (ValueError, subprocess.TimeoutExpired, OSError):
                                text = ""
                pages.append({"page": index + 1, "text": text[:20000]})
                quality.append(
                    {
                        "page": index + 1,
                        "origin": origin,
                        "status": status,
                        "ocr_confidence": confidence,
                        "truncated": len(text) > 20000,
                        "requires_review": True,
                    }
                )
    return {"pages": pages, "page_quality": quality}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--command", default="tesseract")
    parser.add_argument("--languages", default="rus+eng")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    try:
        value = process(
            args.source,
            enable_ocr=args.ocr,
            command=args.command,
            languages=args.languages,
            timeout=args.timeout,
        )
        Path(args.output).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return 1  # PDF/parser/vendor contents must not cross the public error boundary.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
