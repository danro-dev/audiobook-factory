"""Editable section representation, live TTS chunk analysis, and project sidecar persistence."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .epub_parser import Block, Book, Chapter
from .textproc import chunk_paragraph, normalize_text

WORDS_PER_MIN = 155

_BREAK_RE = re.compile(r"^(?:\*\s*\*\s*\*|-{3,}|_{3,})$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_ALL_CAPS_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ]{4,}\b")
_LONG_NUM_RE = re.compile(r"\b\d{4,}\b")
_WEIRD_SYM_RE = re.compile(r"[\[\]{}<>|~^]")


@dataclass
class ChapterSnapshot:
    title: str
    kind: str
    blocks: list[Block]


def clone_blocks(blocks: list[Block]) -> list[Block]:
    return [Block(kind=b.kind, text=b.text, level=b.level) for b in blocks]


def snapshot_book(book: Book) -> dict[int, ChapterSnapshot]:
    """Capture original chapter state keyed by Chapter.index for dirty-tracking & restore."""
    return {
        ch.index: ChapterSnapshot(title=ch.title, kind=ch.kind, blocks=clone_blocks(ch.blocks))
        for ch in book.chapters
    }


def is_chapter_edited(ch: Chapter, snapshots: dict[int, ChapterSnapshot]) -> bool:
    snap = snapshots.get(ch.index)
    if snap is None:
        return True
    if ch.title != snap.title or ch.kind != snap.kind:
        return True
    if len(ch.blocks) != len(snap.blocks):
        return True
    return any(
        (a.kind != b.kind or a.text != b.text or a.level != b.level)
        for a, b in zip(ch.blocks, snap.blocks)
    )


def blocks_to_text(blocks: list[Block]) -> str:
    """Serialize structured chapter blocks to human-editable plain markup."""
    parts: list[str] = []
    for b in blocks:
        if b.kind == "break":
            parts.append("* * *")
        elif b.kind == "heading":
            hashes = "#" * max(1, min(6, b.level or 2))
            parts.append(f"{hashes} {b.text}")
        else:
            if b.text.strip():
                parts.append(b.text.strip())
    return "\n\n".join(parts)


def text_to_blocks(text: str) -> list[Block]:
    """Parse human-editable markup back into structured `Block` instances."""
    raw = text.strip()
    if not raw:
        return []
    blocks: list[Block] = []
    paragraphs = re.split(r"\r?\n\s*\r?\n", raw)
    for para in paragraphs:
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue
        # Handle headings or scene breaks even if someone didn't leave blank lines around them
        para_buf: list[str] = []

        def flush_buf():
            if para_buf:
                joined = re.sub(r"\s+", " ", " ".join(para_buf)).strip()
                if joined:
                    blocks.append(Block(kind="para", text=joined))
                para_buf.clear()

        for ln in lines:
            if _BREAK_RE.match(ln):
                flush_buf()
                blocks.append(Block(kind="break"))
                continue
            m = _HEADING_RE.match(ln)
            if m:
                flush_buf()
                blocks.append(Block(kind="heading", text=m.group(2).strip(), level=len(m.group(1))))
                continue
            para_buf.append(ln)
        flush_buf()
    return blocks


def _chunk_warnings(text: str, max_chars: int) -> list[str]:
    warns: list[str] = []
    if len(text) > max_chars:
        warns.append("Largo")
    if _ALL_CAPS_RE.search(text):
        warns.append("MAYÚSCULAS")
    if _LONG_NUM_RE.search(text):
        warns.append("Números")
    if _WEIRD_SYM_RE.search(text):
        warns.append("Símbolos")
    return warns


def analyze_chapter_tts(title: str, blocks: list[Block], lang: str, max_chars: int = 350,
                        read_titles: bool = True, speed: float = 1.0) -> dict:
    """Simulate `Converter._synthesize_chapter` normalization + chunking for live preview."""
    items: list[dict] = []
    warnings_total = 0
    chunk_count = 0

    first_is_heading = bool(blocks) and blocks[0].kind == "heading"
    if read_titles and not first_is_heading and title.strip():
        norm_title = normalize_text(title, lang, is_heading=True)
        if norm_title:
            w = _chunk_warnings(norm_title, max_chars)
            warnings_total += len(w)
            items.append({"kind": "title", "text": norm_title, "chars": len(norm_title), "warnings": w})

    for b in blocks:
        if b.kind == "break":
            items.append({"kind": "break", "text": "Pausa de escena", "chars": 0, "warnings": []})
            continue
        norm = normalize_text(b.text, lang, is_heading=(b.kind == "heading"))
        if not norm:
            continue
        if b.kind == "heading":
            w = _chunk_warnings(norm, max_chars)
            warnings_total += len(w)
            items.append({"kind": "heading", "text": norm, "chars": len(norm), "warnings": w})
            continue
        for c in chunk_paragraph(norm, lang, max_chars):
            chunk_count += 1
            w = _chunk_warnings(c, max_chars)
            warnings_total += len(w)
            items.append({"kind": "chunk", "text": c, "chars": len(c), "warnings": w})

    words = sum(len(b.text.split()) for b in blocks if b.kind != "break")
    chars = sum(len(b.text) for b in blocks if b.kind != "break")
    est_minutes = words / (WORDS_PER_MIN * max(0.25, float(speed or 1.0)))
    return {
        "items": items,
        "words": words,
        "chunks": chunk_count,
        "chars": chars,
        "est_minutes": est_minutes,
        "warnings": warnings_total,
    }


def clone_book_and_chapters(book: Book, chapters: list[Chapter]) -> tuple[Book, list[Chapter]]:
    """Create an isolated snapshot of `book` and `chapters` for safe background queuing."""
    cloned_chapters = [
        Chapter(
            index=ch.index,
            href=ch.href,
            title=ch.title,
            kind=ch.kind,
            blocks=clone_blocks(ch.blocks),
            include_default=ch.include_default,
        )
        for ch in chapters
    ]
    cloned_book = Book(
        path=book.path,
        title=book.title,
        authors=list(book.authors),
        language_iso=book.language_iso,
        kokoro_lang=book.kokoro_lang,
        year=book.year,
        cover_bytes=book.cover_bytes,
        cover_ext=book.cover_ext,
        chapters=cloned_chapters,
        subtitle=book.subtitle,
    )
    return cloned_book, cloned_chapters


def project_sidecar_path(book_path: str) -> Path:
    p = Path(book_path)
    return p.with_suffix(p.suffix + ".audiobook.json")


def save_project_edits(book: Book, selected_indices: set[int], snapshots: dict[int, ChapterSnapshot],
                       extra: Optional[dict] = None) -> Path:
    """Persist metadata, chapter selections, order, and edited section content next to the source book."""
    out = project_sidecar_path(book.path)
    chapters_data = []
    for ch in book.chapters:
        entry = {
            "index": ch.index,
            "href": ch.href,
            "title": ch.title,
            "kind": ch.kind,
            "selected": ch.index in selected_indices,
            "edited": is_chapter_edited(ch, snapshots),
        }
        if entry["edited"]:
            entry["blocks"] = [{"kind": b.kind, "text": b.text, "level": b.level} for b in ch.blocks]
        chapters_data.append(entry)

    payload = {
        "version": 1,
        "title": book.title,
        "subtitle": book.subtitle,
        "authors": list(book.authors),
        "year": book.year,
        "chapters": chapters_data,
        "extra": extra or {},
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_project_edits(book: Book) -> Optional[dict]:
    """Load sidecar edits if present and apply them onto `book` in-place. Returns selection/extra dict."""
    sidecar = project_sidecar_path(book.path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None

    if data.get("title"):
        book.title = data["title"]
    if "subtitle" in data:
        book.subtitle = data["subtitle"] or ""
    if isinstance(data.get("authors"), list) and data["authors"]:
        book.authors = [str(a) for a in data["authors"] if str(a).strip()]
    if "year" in data:
        book.year = data["year"]

    by_idx = {ch.index: ch for ch in book.chapters}
    selected_indices: set[int] = set()
    reordered: list[Chapter] = []
    seen: set[int] = set()

    for entry in data.get("chapters", []):
        idx = entry.get("index")
        ch = by_idx.get(idx)
        if ch is None:
            if isinstance(entry.get("blocks"), list) and idx is not None:
                ch = Chapter(
                    index=int(idx),
                    href=entry.get("href", ""),
                    title=entry.get("title", f"Section {idx}"),
                    kind=entry.get("kind", "chapter"),
                    blocks=[
                        Block(kind=b.get("kind", "para"), text=b.get("text", ""), level=int(b.get("level", 0)))
                        for b in entry["blocks"]
                    ],
                    include_default=bool(entry.get("selected", True)),
                )
            else:
                continue
        else:
            seen.add(idx)
            if entry.get("title"):
                ch.title = entry["title"]
            if entry.get("kind"):
                ch.kind = entry["kind"]
            if entry.get("edited") and isinstance(entry.get("blocks"), list):
                ch.blocks = [
                    Block(kind=b.get("kind", "para"), text=b.get("text", ""), level=int(b.get("level", 0)))
                    for b in entry["blocks"]
                ]
        if entry.get("selected", ch.include_default):
            selected_indices.add(ch.index)
        reordered.append(ch)

    for ch in book.chapters:
        if ch.index not in seen:
            reordered.append(ch)
            if ch.include_default:
                selected_indices.add(ch.index)

    book.chapters = reordered
    return {
        "selected_indices": selected_indices,
        "extra": data.get("extra", {}),
    }
