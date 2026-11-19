"""PDF -> EPUB conversion.

PDFs carry no document structure, so the goal here is only to produce a *clean* EPUB: chapters
found by heading patterns, paragraphs rebuilt from extracted lines, running headers/footers and
page numbers removed. The result then goes through the normal EPUB parser, which applies the
app's own text clean-up, so what gets narrated is already tidy.

Scanned PDFs (page images with no text layer) cannot be read without OCR; `convert_pdf` detects
that case and raises `ScannedPdfError` instead of producing an empty book.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Optional

from ebooklib import epub

# "Capitulo 3", "Cap. IV", "Parte segunda", "Chapter 12", "Prologo", ...
CHAPTER_RE = re.compile(
    r"^\s*(?:(?:cap[ií]tulo|cap\.|parte|libro|tomo|acto|chapter|part|book)\s+"
    r"(?:[ivxlcdm]+|\d+|[a-záéíóúüñ]{3,})\b"
    r"|(?:pr[oó]logo|ep[ií]logo|introducci[oó]n|prefacio|dedicatoria|ap[eé]ndice|"
    r"agradecimientos|bibliograf[ií]a|nota\s+(?:del|de\s+la)\s+autor))\b",
    re.IGNORECASE,
)
# A bare page number, e.g. "12", "- 12 -", "xii"
PAGE_NUMBER_RE = re.compile(r"^\s*[-–—(\[]?\s*(?:\d{1,4}|[ivxlcdm]{1,7})\s*[-–—)\]]?\s*$", re.IGNORECASE)
_SENTENCE_END = ".!?…:;,»\"'”’)"
_TERMINALS = ".!?…»\"'”’)"



class ScannedPdfError(RuntimeError):
    """Raised when the PDF has no usable text layer (it would need OCR)."""


def _join_lines(lines: list[str]) -> str:
    """Rebuild a paragraph from PDF lines, undoing end-of-line hyphenation."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + line          # "narrati-" + "on" -> "narrati" + "on"
        else:
            out = out + " " + line
    return re.sub(r"\s+", " ", out).strip()


def _fingerprint(text: str) -> str:
    """Normalise a line so that running headers with changing numbers still match."""
    return re.sub(r"\d+", "#", text.strip().lower())


Block = tuple[str, float]  # block text and the largest line height in it


def extract_pages(pdf_path: str | Path) -> list[list[Block]]:
    """Return one list of (text, font size) blocks per page, in reading order.

    The font size is kept because it is the only reliable way to tell a chapter heading from a
    sentence: text heuristics fail as soon as the heading wraps, or when pdfminer glues the
    heading to the first line of the chapter.
    """
    from pdfminer.high_level import extract_pages as pdf_extract_pages
    from pdfminer.layout import LAParams, LTTextContainer, LTTextLine

    pages: list[list[Block]] = []
    laparams = LAParams(line_margin=0.45, char_margin=2.0, word_margin=0.15, detect_vertical=False)
    for layout in pdf_extract_pages(str(pdf_path), laparams=laparams):
        blocks: list[Block] = []
        for element in layout:
            if not isinstance(element, LTTextContainer):
                continue
            lines = [ln for ln in element if isinstance(ln, LTTextLine)]
            text = _join_lines([ln.get_text() for ln in lines])
            if text:
                blocks.append((text, max((ln.height for ln in lines), default=0.0)))
        pages.append(blocks)
    return pages


def _body_size(pages: list[list[Block]]) -> float:
    """The font size that carries most of the text: the body size of the book."""
    from collections import Counter

    weights: Counter[float] = Counter()
    for blocks in pages:
        for text, size in blocks:
            if size > 0:
                weights[round(size, 1)] += len(text)
    return weights.most_common(1)[0][0] if weights else 0.0


def _strip_running_lines(pages: list[list[Block]]) -> list[list[Block]]:
    """Drop headers/footers/page numbers at page edges that repeat across the book."""
    if len(pages) < 3:
        return pages
    from collections import Counter

    counts: Counter[str] = Counter()
    for blocks in pages:
        for candidate in {blocks[0][0], blocks[-1][0]} if blocks else ():
            counts[_fingerprint(candidate)] += 1
    threshold = max(3, int(len(pages) * 0.35))

    cleaned: list[list[Block]] = []
    for blocks in pages:
        blocks = list(blocks)
        while blocks and (counts[_fingerprint(blocks[0][0])] >= threshold or PAGE_NUMBER_RE.match(blocks[0][0])):
            blocks.pop(0)
        while blocks and (counts[_fingerprint(blocks[-1][0])] >= threshold or PAGE_NUMBER_RE.match(blocks[-1][0])):
            blocks.pop()
        cleaned.append(blocks)
    return cleaned


def _merge_blocks(blocks: list[Block], body_size: float = 0.0) -> list[tuple[str, bool]]:
    """Join consecutive lines into (paragraph, is_heading) pairs.

    pdfminer often returns one container per printed line, so a paragraph arrives as several
    blocks. A line continues into the next one unless it ends with sentence punctuation; a
    heading always stands alone, which matters because pdfminer happily glues the heading to the
    first line of the chapter and the heading would then be lost as a chapter title.
    """
    def is_heading(text: str, size: float) -> bool:
        if _looks_like_heading(text):
            return True
        return body_size > 0 and size >= body_size * 1.12

    paragraphs: list[tuple[str, bool]] = []
    buf = ""
    buf_head = False
    for raw, size in blocks:
        text = raw.strip()
        if not text:
            continue
        head = is_heading(text, size)
        if not buf:
            buf, buf_head = text, head
        elif buf_head or head or buf[-1] in _TERMINALS:
            paragraphs.append((buf, buf_head))
            buf, buf_head = text, head
        elif buf.endswith("-") and len(buf) > 1 and buf[-2].isalpha():
            buf = buf[:-1] + text          # undo end-of-line hyphenation
        else:
            buf = buf + " " + text
    if buf:
        paragraphs.append((buf, buf_head))
    return [(re.sub(r"\s+", " ", p).strip(), h) for p, h in paragraphs if p.strip()]


_TOC_MARKER = re.compile(r"^\s*(tabla de conten(?:ido|idos)|(?:í|i)ndice|contenidos?|sumario|contents)\b",
                         re.IGNORECASE)


def _drop_contents(paragraphs: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Remove the table of contents.

    A PDF usually opens with one, and its entries look exactly like chapter headings, so leaving
    it in turns the book into a pile of empty chapters. The contents is the run of short blocks
    that follows the "Tabla de contenido"/"Índice" marker; the run ends at the first real
    paragraph of the book.
    """
    kept: list[tuple[str, bool]] = []
    i, n = 0, len(paragraphs)
    while i < n:
        if _TOC_MARKER.match(paragraphs[i][0]):
            # skip the marker and every short entry after it, up to the first real paragraph
            j = i + 1
            while j < n and j - i < 120 and len(paragraphs[j][0].split()) <= 15:
                j += 1
            if j > i + 1 or i + 1 >= n:
                i = j
                continue
        kept.append(paragraphs[i])
        i += 1

    if len(kept) == len(paragraphs):
        # No marker anywhere: several headings in a row, with no text between them, is still a
        # contents -- but only near the beginning, so a real chapter list at the end survives.
        run = 0
        limit = min(len(kept), max(30, len(kept) // 4))
        for i in range(limit):
            text, head = kept[i]
            if head and len(text.split()) <= 14:
                run += 1
                if run >= 5:
                    return kept[: i - run + 1] + kept[i + 1:]
            else:
                run = 0
    return kept


def _looks_like_heading(par: str) -> bool:
    text = par.strip()
    if not text or len(text) > 120:
        return False
    if CHAPTER_RE.match(text):
        return True
    # a short ALL-CAPS line without a sentence ending is almost always a title
    return (len(text) <= 60 and text.upper() == text and any(c.isalpha() for c in text)
            and text[-1] not in _SENTENCE_END)


def _split_title(text: str, max_words: int = 15) -> tuple[str, str]:
    """Split a heading that swallowed the first line of its chapter into (title, rest).

    pdfminer puts the heading and the following line in one container more often than not, so the
    title would otherwise be the heading plus a full sentence, and that sentence would never be
    read (it is dropped from the body).
    """
    text = text.strip()
    words = text.split()
    if len(words) <= max_words:
        return text, ""
    for m in re.finditer(r"[.!?…]", text):
        head = text[: m.end()].strip()
        if len(head.split()) <= max_words:
            return head, text[m.end():].strip()
        break
    return " ".join(words[:max_words]), " ".join(words[max_words:])


def _split_chapters(paragraphs: list[tuple[str, bool]]) -> list[tuple[str, list[str]]]:
    """Group (paragraph, is_heading) pairs into (chapter title, paragraphs)."""
    chapters: list[tuple[str, list[str]]] = []
    title: Optional[str] = None
    body: list[str] = []

    def flush():
        nonlocal title, body
        if title or body:
            chapters.append((title or "Texto", body))
        title, body = None, []

    for par, is_head in paragraphs:
        if is_head:
            head_text, rest = _split_title(par)
            if title and not body:      # a heading wrapped over two blocks
                title = f"{title} {head_text}".strip()
            else:
                flush()
                title = head_text
            if rest:
                body.append(rest)
        else:
            if len(par) < 2:
                continue
            body.append(par)
    flush()
    return [(t, [p for p in ps if p.strip()]) for t, ps in chapters if ps]


def _pdf_meta(pdf_path: Path) -> tuple[str, str]:
    """Best-effort (title, author) from the PDF document info dictionary."""
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser

        with pdf_path.open("rb") as f:
            doc = PDFDocument(PDFParser(f))
            meta = doc.info[0] if doc.info else {}

        def value(key: str) -> str:
            raw = meta.get(key, "")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-16", "ignore") if raw[:2] in (b"\xfe\xff", b"\xff\xfe") else raw.decode("latin-1", "ignore")
            return str(raw or "").strip()

        return value("Title"), value("Author")
    except Exception:
        return "", ""


def convert_pdf(pdf_path: str | Path, out_epub: Optional[str | Path] = None, *,
                title: Optional[str] = None, author: Optional[str] = None, language: str = "",
                on_log: Optional[Callable[[str], None]] = None) -> str:
    """Convert a text PDF into an EPUB. Returns the path written.

    Raises ScannedPdfError when the PDF has no text layer (needs OCR).
    """
    log = on_log or (lambda m: None)
    pdf_path = Path(pdf_path)
    out_epub = Path(out_epub) if out_epub else pdf_path.with_suffix(".epub")

    pages = extract_pages(pdf_path)
    if not pages:
        raise ScannedPdfError("the PDF has no pages")
    pages = _strip_running_lines(pages)

    paragraphs = _drop_contents(_merge_blocks([b for page in pages for b in page], _body_size(pages)))
    total_chars = sum(len(text) for text, _ in paragraphs)
    per_page = total_chars / max(1, len(pages))
    if per_page < 60:
        raise ScannedPdfError(
            f"only {per_page:.0f} characters of text per page — this PDF looks scanned (image pages). "
            "It needs OCR before it can be converted.")

    meta_title, meta_author = _pdf_meta(pdf_path)
    book_title = title or meta_title or pdf_path.stem
    book_author = author or meta_author

    chapters = _split_chapters(paragraphs)
    if not chapters:
        raise ScannedPdfError("no readable text could be grouped into chapters")

    log(f"PDF: {len(pages)} pages, {total_chars:,} characters, {len(chapters)} chapter(s) detected")

    book = epub.EpubBook()
    book.set_identifier("pdf-" + hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:16])
    book.set_title(book_title)
    book.set_language(language or "es")
    if book_author:
        book.add_author(book_author)

    style = epub.EpubItem(uid="style", file_name="style/book.css", media_type="text/css",
                          content="body { margin: 5%; line-height: 1.5; } h1 { page-break-before: always; }")
    book.add_item(style)

    items = []
    for i, (ch_title, paragraphs_) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=ch_title, file_name=f"chap{i:03d}.xhtml", lang=language or "es")
        body = "\n".join(f"<p>{_escape(p)}</p>" for p in paragraphs_)
        item.content = (f"<html><head><title>{_escape(ch_title)}</title>"
                        f'<link rel="stylesheet" href="style/book.css"/></head>'
                        f"<body><h1>{_escape(ch_title)}</h1>\n{body}</body></html>")
        book.add_item(item)
        items.append(item)

    book.toc = tuple(items)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    # The nav document stays in the manifest (EPUB 3 requires it) but not in the spine: as a
    # reading section it would show up as an empty "nav" chapter in the app.
    book.spine = list(items)

    out_epub.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_epub), book)
    log(f"wrote {out_epub.name}: '{book_title}'" + (f" — {book_author}" if book_author else ""))
    return str(out_epub)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
