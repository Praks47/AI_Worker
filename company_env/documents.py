"""Document repository: search + read real PDF files from disk."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

# (path, mtime_ns, size) -> (text, page_count, warning)
_CACHE: dict[tuple, tuple[str, int, str | None]] = {}


def _extract(path: Path) -> tuple[str, int, str | None]:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        try:
            with pymupdf.open(path) as doc:
                text = "\n".join(page.get_text() for page in doc).strip()
                pages = doc.page_count
            warning = None if text else "no extractable text (scanned image?)"
            _CACHE[key] = (text, pages, warning)
        except Exception as exc:  # corrupt / unreadable file
            _CACHE[key] = ("", 0, f"could not read file: {exc}")
    return _CACHE[key]


def _doc_id(rel: str) -> str:
    return "DOC-" + hashlib.sha1(rel.encode()).hexdigest()[:8].upper()


class DocumentStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _all(self) -> list[Path]:
        return sorted(self.root.rglob("*.pdf")) if self.root.exists() else []

    def _meta(self, path: Path) -> dict:
        rel = path.relative_to(self.root)
        text, pages, warning = _extract(path)
        st = path.stat()
        return {
            "document_id": _doc_id(rel.as_posix()),
            "filename": path.name,
            "folder": rel.parts[0] if len(rel.parts) > 1 else "",
            "uploaded_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "size_bytes": st.st_size,
            "page_count": pages,
            "_text": text,
            "_warning": warning,
        }

    @staticmethod
    def _public(meta: dict) -> dict:
        return {k: v for k, v in meta.items() if not k.startswith("_")}

    @staticmethod
    def _snippet(text: str, terms: list[str], width: int = 220) -> str:
        flat = " ".join(text.split())
        low = flat.lower()
        pos = min((low.find(t) for t in terms if t in low), default=0)
        start = max(0, pos - 40)
        return flat[start:start + width]

    def search(self, q: str = "", folder: str | None = None, limit: int = 20) -> list[dict]:
        terms = [t for t in q.lower().split() if t]
        out = []
        for path in self._all():
            meta = self._meta(path)
            if folder and meta["folder"] != folder:
                continue
            haystack = (meta["filename"] + " " + meta["_text"]).lower()
            if all(t in haystack for t in terms):
                item = self._public(meta)
                item["snippet"] = self._snippet(meta["_text"], terms)
                out.append(item)
        out.sort(key=lambda d: (d["uploaded_at"], d["filename"]), reverse=True)
        return out[:limit]

    def get(self, document_id: str) -> dict | None:
        for path in self._all():
            meta = self._meta(path)
            if meta["document_id"] == document_id:
                item = self._public(meta)
                item["text"] = meta["_text"]
                item["extraction_warning"] = meta["_warning"]
                return item
        return None

    def path_for(self, document_id: str) -> Path | None:
        for path in self._all():
            if _doc_id(path.relative_to(self.root).as_posix()) == document_id:
                return path
        return None

    def id_for_filename(self, filename: str | None) -> str | None:
        if not filename:
            return None
        for path in self._all():
            if path.name == filename:
                return _doc_id(path.relative_to(self.root).as_posix())
        return None
