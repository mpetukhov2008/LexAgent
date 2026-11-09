"""Content-addressed original PDFs, retained independently of the API response cache."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from app.agent import AgentError


class OriginalStore:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AgentError("Некорректный идентификатор оригинала")
        return self.root / digest[:2] / (digest + ".pdf")

    def put(self, raw):
        if not isinstance(raw, bytes) or not raw.startswith(b"%PDF") or len(raw) > 10_000_000:
            raise AgentError("Источник не вернул пригодный PDF")
        digest = hashlib.sha256(raw).hexdigest()
        target = self.path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise AgentError("Нарушена целостность сохранённого оригинала")
            return digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest

    def get(self, digest):
        target = self.path(digest)
        try:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise AgentError("Нарушена целостность сохранённого оригинала")
        except OSError:
            raise AgentError("Оригинал PDF отсутствует в архиве") from None
        return target
