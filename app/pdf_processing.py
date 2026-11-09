"""Bounded, cancellable PDF process; keeps native parsers outside the web/worker process."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from app.agent import AgentError
from app.config import ROOT


async def extract_document(path, settings):
    with tempfile.TemporaryDirectory(prefix="lexagent-pdf-") as directory:
        output = Path(directory) / "result.json"
        arguments = [
            sys.executable,
            "-m",
            "app.pdf_worker",
            str(path),
            str(output),
            "--command",
            settings.tesseract_cmd,
            "--languages",
            settings.ocr_languages,
            "--timeout",
            str(settings.ocr_page_timeout),
        ]
        if settings.enable_ocr:
            arguments.append("--ocr")
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=ROOT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=settings.pdf_timeout)
            if process.returncode or not output.exists() or output.stat().st_size > 20_000_000:
                raise AgentError("Источник не вернул пригодный PDF")
            return json.loads(output.read_text(encoding="utf-8"))
        except (asyncio.TimeoutError, ValueError, OSError):
            raise AgentError("Не удалось обработать PDF в установленное время") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
