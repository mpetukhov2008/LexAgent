"""User-owned PDF / image OCR input boundaries and browser-facing contract."""

import asyncio
import struct

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.config import Settings
from app.main import create_app
from app.upload_processing import decode_upload, validate_upload


def png_bytes(width=3, height=2):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" +
            struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" +
            b"\x00\x00\x00\x00")


def test_file_types_and_size_validation():
    assert validate_upload(b"%PDF-1.7\nvalid", "document.pdf") == ".pdf"
    assert validate_upload(png_bytes(), "document.png") == ".png"
    assert validate_upload(b"\xff\xd8\xff\xc0\x00\x07\x08\x00\x03\x00\x04", "x.jpg") == ".jpg"
    for data, name in [(b"bad", "document.pdf"),
                       (b"", "document.pdf"),
                       (b"%PDF" + b"x" * 10_000_000, "document.pdf"),
                       (png_bytes(12000, 12000), "document.png"),
                       (b"<svg/>", "evil.svg"),
                       (b"not-an-image", "scan.jpg")]:
        with pytest.raises(AgentError):
            validate_upload(data, name)


def test_extracted_preview_marks_truncation_and_ocr():
    preview = decode_upload({
        "pages": [{"page": 1, "text": "А" * 13000}],
        "page_quality": [{"origin": "ocr"}],
    })
    assert len(preview["text"]) == 12000
    assert preview["truncated"] and preview["ocr_used"] and preview["review_required"]
    with pytest.raises(AgentError, match="Текст не найден"):
        decode_upload({"pages": [{"page": 1, "text": ""}]})


def test_upload_api_pdf_image_and_boundary(tmp_path, monkeypatch):
    settings = Settings(database=tmp_path / "test.sqlite3",
                        document_dir=tmp_path / "originals", worker_enabled=False)

    async def pdf_text(path, settings):
        assert path.suffix == ".pdf"
        return {"pages": [{"page": 1, "text": "Доказательство: документ ответчика"}],
                "page_quality": [{"origin": "pdf_text"}]}

    async def image_ocr(path, settings):
        assert path.suffix == ".png"
        return {"pages": [{"page": 1, "text": "Распознанный текст скана"}],
                "page_quality": [{"origin": "ocr"}]}

    monkeypatch.setattr("app.pdf_processing.extract_document", pdf_text)
    monkeypatch.setattr("app.main.extract_image", image_ocr)
    with TestClient(create_app(settings)) as client:
        pdf = b"%PDF-1.7\n" + b"0" * 301000
        response = client.post("/api/documents/extract", content=pdf,
                               headers={"X-Document-Name": "document.pdf"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert "Доказательство" in data["text"] and not data["ocr_used"]
        assert client.get(data["original_url"]).content == pdf
        assert client.post("/api/runs", content=b"x" * 300001).status_code == 413

        response = client.post("/api/documents/extract", content=png_bytes(),
                               headers={"X-Document-Name": "document.png"})
        assert response.status_code == 200, response.text
        assert response.json()["ocr_used"] and not response.json().get("original_url")

        assert client.post("/api/documents/extract", content=b"bad",
                           headers={"X-Document-Name": "document.pdf"}).status_code == 422
        assert client.post("/api/documents/extract", content=png_bytes(),
                           headers={"X-Document-Name": "document.png",
                                    "Origin": "https://untrusted.example"}).status_code == 403
        assert client.post("/api/documents/extract", content=b"x" * 10_000_001,
                           headers={"X-Document-Name": "document.pdf"}).status_code == 413
