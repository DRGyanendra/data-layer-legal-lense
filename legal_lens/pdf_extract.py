"""PDF to clean lines of text, with the page and left edge of each line.

Supreme Court PDFs carry more than the judgment text: a digital-signature
stamp on page one, footnotes, superscript footnote markers inside
sentences, page numbers and running headers. Reading the page as plain
text mixes all of that into the paragraphs. This reader looks at font
size and position instead:

  body text        the dominant font size on the page
  footnotes        smaller text in a block at the foot of the page; kept
                   separately, because citations often live there
  superscripts     small digits inside a body line; dropped
  signature stamp  tiny lettering; dropped

pdfplumber is the backend this was developed and tested against. PyMuPDF
is supported for speed but has not been run in testing. Pages with no
text layer are scans: they go to Tesseract when it is available and are
reported when it is not, so a judgment is never loaded with silent holes.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .paragraphs import Line

MIN_TEXT_CHARS = 40     # a page with less text than this is treated as a scan
SMALL = 0.78            # below this fraction of body size: footnote or superscript
TINY = 0.5              # below this: signature stamp or footnote marker

_PAGE_NUMBER = re.compile(r"^\s*(?:page\s+)?-?\s*\d{1,4}\s*-?(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
_RUNNING_PART = re.compile(r"^\s*PART\s+[A-Z]{1,2}\s*$")
_STAMP = re.compile(r"signature not verified|digitally signed by|^reason:\s*$", re.IGNORECASE)
# Lines that sites and publishers print on every page of a judgment.
_SITE = re.compile(
    r"indian\s?kanoon\s*-\s*https?://|^https?://\S+\s*\d*$|scc\s?online\s+web\s+edition|truepr?int|"
    r"^page\s+\d+\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),|printed\s+for\s*:|"
    r"^digital\s+supreme\s+court\s+reports$|manupatra|^this\s+is\s+a\s+true\s+copy",
    re.IGNORECASE)

# Glyphs that Supreme Court PDFs use in place of ordinary quotation marks.
_GLYPHS = str.maketrans({"―": "“", "‖": "”", "‗": "‘", "„": "‘", "‟": "’", " ": " ", "": "•"})


# JUDIS text files spell out Windows-1252 punctuation as octal escapes, often with
# the leading digit lost: '\\026' is a dash, '\\005' an ellipsis.
_ESCAPES = [
    (re.compile(r"(?:\\[02]?05)+"), "…"), (re.compile(r"\\[02]?26"), "–"), (re.compile(r"\\[02]?27"), "—"),
    (re.compile(r"\\[02]?23"), "“"), (re.compile(r"\\[02]?24"), "”"),
    (re.compile(r"\\[02]?21"), "‘"), (re.compile(r"\\[02]?22"), "’"),
]


def _tidy(text: str) -> str:
    text = text.translate(_GLYPHS)
    if "\\" in text:
        for pattern, replacement in _ESCAPES:
            text = pattern.sub(replacement, text)
    return text


@dataclass
class Page:
    number: int                                   # 1-based
    lines: list[str]
    x0: list[float | None] = field(default_factory=list)      # left edge of each line, in points
    footnotes: list[str] = field(default_factory=list)
    ocr: bool = False                             # text came from OCR
    needs_ocr: bool = False                       # scan, and OCR was not possible
    gap: list[bool] = field(default_factory=list)             # True where a line follows extra vertical space
    columns: int = 1                              # 2 when the page was set in two columns


@dataclass
class Extraction:
    pages: list[Page]
    backend: str
    sha256: str
    warnings: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[Line]:
        out = []
        for p in self.pages:
            xs = p.x0 if len(p.x0) == len(p.lines) else [None] * len(p.lines)
            gaps = getattr(p, "gap", [])
            gaps = gaps if len(gaps) == len(p.lines) else [False] * len(p.lines)
            out.extend(Line(p.number, text, x, g) for text, x, g in zip(p.lines, xs, gaps))
        return out

    @property
    def text(self) -> str:
        return "\n".join(text for p in self.pages for text in p.lines)

    @property
    def footnotes_by_page(self) -> dict[int, list[str]]:
        return {p.number: p.footnotes for p in self.pages if p.footnotes}


# ---------------------------------------------------------------- page model

@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float


def _line_text(words: list[_Word]) -> str:
    """Join words left to right; pieces that touch are one word ('15' + 'th')."""
    words = sorted(words, key=lambda w: w.x0)
    out = words[0].text
    for prev, word in zip(words, words[1:]):
        out += ("" if word.x0 - prev.x1 < 1.0 else " ") + word.text
    return out


def _cluster(words: list[_Word], tolerance: float = 3.0) -> list[list[_Word]]:
    """Group words into lines by their baseline."""
    lines: list[list[_Word]] = []
    for word in sorted(words, key=lambda w: (w.bottom, w.x0)):
        if lines and abs(lines[-1][-1].bottom - word.bottom) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


def _dominant_size(words: list[_Word]) -> float | None:
    weight: Counter[float] = Counter()
    for w in words:
        weight[round(w.size, 1)] += len(w.text)
    return weight.most_common(1)[0][0] if weight else None


def _gaps(tops: list[float]) -> list[bool]:
    """True for each line that sits further below the one before it than the usual pitch."""
    steps = [b - a for a, b in zip(tops, tops[1:]) if b - a > 1]
    if len(steps) < 4:
        return [False] * len(tops)
    pitch = sorted(steps)[len(steps) // 2]
    return [False] + [(b - a) > 1.45 * pitch for a, b in zip(tops, tops[1:])]


def _gutter(words: list[_Word]) -> float | None:
    """The x position of the gap between two columns, or None for an ordinary page.

    Printed law reports (AIR and others) set judgments in two columns. Read
    straight across, the columns interleave and the text is nonsense.
    """
    if len(words) < 120:
        return None
    left, right = min(w.x0 for w in words), max(w.x1 for w in words)
    width = right - left
    if width < 300:
        return None
    rows = _cluster(words)
    best, best_bridged = None, None
    x = left + 0.38 * width
    while x <= left + 0.62 * width:
        bridged = on_left = on_right = 0
        for row in rows:
            before = [w for w in row if w.x1 <= x]
            after = [w for w in row if w.x0 >= x]
            crossing = len(before) + len(after) < len(row)
            # an ordinary line runs through x: a word sits on it, or the words either side are a space apart
            if crossing or (before and after and min(w.x0 for w in after) - max(w.x1 for w in before) < 12):
                bridged += 1
                continue
            on_left += bool(before)
            on_right += bool(after)
        if min(on_left, on_right) >= 10 and (best_bridged is None or bridged < best_bridged):
            best, best_bridged = x, bridged
        x += 3
    if best is None or best_bridged > max(2, 0.08 * len(rows)):
        return None
    if min(sum(1 for w in words if w.x1 <= best), sum(1 for w in words if w.x0 >= best)) < 50:
        return None
    return best


def _page_from_words(number: int, words: list[_Word], doc_size: float | None = None,
                     split_columns: bool = True) -> Page:
    if not words:
        return Page(number, [])
    gutter = _gutter(words) if split_columns else None
    if gutter is not None:
        lhs = [w for w in words if (w.x0 + w.x1) / 2 < gutter]
        rhs = [w for w in words if (w.x0 + w.x1) / 2 >= gutter]
        a = _page_from_words(number, lhs, doc_size, split_columns=False)
        b = _page_from_words(number, rhs, doc_size, split_columns=False)
        shift = (min(w.x0 for w in rhs) - min(w.x0 for w in lhs)) if lhs and rhs else 0.0
        return Page(number, a.lines + b.lines, a.x0 + [round(x - shift, 1) for x in b.x0],
                    a.footnotes + b.footnotes, gap=a.gap + b.gap, columns=2)
    body_size = _dominant_size(words)
    # A page that is mostly quotation or footnotes has a small dominant size;
    # fall back to the document's body size so its body text is still recognised.
    if doc_size and body_size < 0.9 * doc_size:
        body_size = doc_size

    # 1. The signature stamp: tiny lettering. Drop everything tiny in its band.
    tiny_letters = [w for w in words if w.size < TINY * body_size and re.search(r"[A-Za-z]", w.text)]
    if tiny_letters:
        lo = min(w.top for w in tiny_letters) - 3
        hi = max(w.bottom for w in tiny_letters) + 3
        words = [w for w in words if not (w.size < TINY * body_size and lo <= w.top <= hi)]

    body = _cluster([w for w in words if w.size >= SMALL * body_size])
    small_words = [w for w in words if w.size < SMALL * body_size]

    # 2. Small words that sit inside a body line are superscripts. Digits are
    #    footnote markers and go; letters ('th' in '15th') are stitched back in.
    loose: list[_Word] = []
    for word in small_words:
        middle = (word.top + word.bottom) / 2
        host = next((l for l in body if min(w.top for w in l) - 4 <= middle <= max(w.bottom for w in l)
                     and min(w.x0 for w in l) - 2 <= word.x0 <= max(w.x1 for w in l) + 12), None)
        if host is None:
            loose.append(word)
        elif not re.fullmatch(r"[\d,*†‡]+", word.text):
            host.append(word)
    small = _cluster(loose)

    # 3. Small lines in one block at the foot of the page are footnotes, if any
    #    of them starts with a marker. Small lines elsewhere are quoted extracts.
    ordered = sorted([("body", l) for l in body] + [("small", l) for l in small],
                     key=lambda item: min(w.top for w in item[1]))
    cut = len(ordered)
    while cut > 0:
        kind, line = ordered[cut - 1]
        if kind == "small" or re.fullmatch(r"\d{1,4}", _line_text(line).strip()):
            cut -= 1
        else:
            break
    zone = [item for item in ordered[cut:] if item[0] == "small"]
    # A footnote marker is a bare number; '22. The court...' is a quoted paragraph.
    has_marker = any(re.match(r"^\d{1,3}(?:\s+\S|\s*$)", _line_text(l)) for _, l in zone)
    if zone and has_marker:
        kept = ordered[:cut] + [item for item in ordered[cut:] if item[0] == "body"]
        # A footnote's marker is raised and tiny, so it clusters apart from its text.
        # Regroup the whole zone by vertical centre to put each marker back in front.
        rows: list[list[_Word]] = []
        for word in sorted((w for _, l in zone for w in l), key=lambda w: (w.top + w.bottom) / 2):
            centre = (word.top + word.bottom) / 2
            if rows and abs(centre - sum((w.top + w.bottom) / 2 for w in rows[-1]) / len(rows[-1])) <= 4:
                rows[-1].append(word)
            else:
                rows.append([word])
        footnotes = _footnotes([_line_text(row) for row in rows])
    else:
        kept, footnotes = ordered, []

    lines = [_tidy(_line_text(l)) for _, l in kept]
    x0 = [round(min(w.x0 for w in l), 1) for _, l in kept]
    gap = _gaps([max(w.bottom for w in l) for _, l in kept])
    return Page(number, lines, x0, [_tidy(f) for f in footnotes], gap=gap)


def _footnotes(lines: list[str]) -> list[str]:
    """'1' / 'text' / 'more text' / '2' / 'text' -> ['1 text more text', '2 text']."""
    notes: list[str] = []
    last = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        marker = re.fullmatch(r"(\d{1,3})", line)
        inline = re.match(r"^(\d{1,3})\s+(\S.*)$", line)
        if marker:
            notes.append(marker.group(1))
            last = int(marker.group(1))
        elif inline and (not notes or int(inline.group(1)) == last + 1):
            notes.append(f"{inline.group(1)} {inline.group(2)}")
            last = int(inline.group(1))
        elif notes:
            notes[-1] += " " + line
        else:
            notes.append(line)
    return notes


# ---------------------------------------------------------------- backends

def _ocr_page(image, number: int) -> Page | None:
    """OCR one page image into lines with their left edge."""
    try:
        import pytesseract
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception:           # pytesseract or the tesseract binary is missing, or it failed
        return None
    scale = 72.0 / 300.0
    grouped: dict[tuple[int, int, int], list[int]] = {}
    for i, text in enumerate(data["text"]):
        if text.strip():
            grouped.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    rows = sorted(grouped.values(), key=lambda idx: (min(data["top"][i] for i in idx), min(data["left"][i] for i in idx)))
    lines = [" ".join(data["text"][i] for i in sorted(idx, key=lambda i: data["left"][i])).translate(_GLYPHS) for idx in rows]
    x0 = [round(min(data["left"][i] for i in idx) * scale, 1) for idx in rows]
    gap = _gaps([max(data["top"][i] + data["height"][i] for i in idx) * scale for idx in rows])
    return Page(number, lines, x0, ocr=True, gap=gap)


def _extract_pdfplumber(path: Path, ocr: bool) -> list[Page]:
    import pdfplumber

    def words_of(page) -> list[_Word]:
        raw = page.extract_words(extra_attrs=["size"], keep_blank_chars=False)
        return [_Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"], w["size"]) for w in raw]

    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        sample: list[_Word] = []
        for page in pdf.pages[:20]:
            sample.extend(words_of(page))
            page.flush_cache()
        doc_size = _dominant_size(sample)

    pages: list[Page] = []
    batch = 40                        # reopen the file in batches: pdfplumber's memory grows with every page read
    for first in range(1, total + 1, batch):
        with pdfplumber.open(path, pages=list(range(first, min(total, first + batch - 1) + 1))) as pdf:
            for page in pdf.pages:
                number = page.page_number
                words = words_of(page)
                if sum(len(w.text) for w in words) < MIN_TEXT_CHARS:
                    recognised = _ocr_page(page.to_image(resolution=300).original, number) if ocr else None
                    if recognised and recognised.lines:
                        pages.append(recognised)
                    else:
                        pages.append(Page(number, [w.text for w in words], needs_ocr=bool(page.images)))
                else:
                    pages.append(_page_from_words(number, words, doc_size))
                page.flush_cache()
    return pages


def _extract_pymupdf(path: Path, ocr: bool) -> list[Page]:
    import fitz  # PyMuPDF

    pages: list[Page] = []
    with fitz.open(path) as doc:
        for number, page in enumerate(doc, start=1):
            words: list[_Word] = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        x0, top, x1, bottom = span["bbox"]
                        cursor = x0
                        pieces = span["text"].split(" ")
                        width = (x1 - x0) / max(1, len(span["text"]))
                        for piece in pieces:
                            if piece:
                                words.append(_Word(piece, cursor, cursor + width * len(piece), top, bottom, span["size"]))
                            cursor += width * (len(piece) + 1)
            if sum(len(w.text) for w in words) < MIN_TEXT_CHARS:
                recognised = None
                if ocr:
                    from PIL import Image

                    pix = page.get_pixmap(dpi=300)
                    recognised = _ocr_page(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), number)
                if recognised and recognised.lines:
                    pages.append(recognised)
                else:
                    pages.append(Page(number, [w.text for w in words], needs_ocr=bool(page.get_images())))
            else:
                pages.append(_page_from_words(number, words))
    return pages


# ---------------------------------------------------------------- clean-up

def _strip_furniture(pages: list[Page]) -> None:
    """Remove page numbers, running headers and signature-stamp lines, in place."""
    def shape(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())

    edge_counts: Counter[str] = Counter()
    for page in pages:
        content = [l for l in page.lines if l.strip()]
        for line in set(map(shape, content[:2] + content[-2:])):
            edge_counts[line] += 1
    repeated = {
        line for line, n in edge_counts.items()
        if len(pages) >= 3 and n >= max(3, 0.6 * len(pages)) and len(line) > 3
        and not re.match(r"^#\.? [a-z]", line)        # a numbered paragraph is never a running header
    }

    for page in pages:
        has_x0 = len(page.x0) == len(page.lines)
        content_idx = [i for i, l in enumerate(page.lines) if l.strip()]
        edges = set(content_idx[:2] + content_idx[-2:])
        gaps_in = getattr(page, "gap", [])
        has_gap = len(gaps_in) == len(page.lines)
        lines, xs, gaps = [], [], []
        dropped = False
        for i, line in enumerate(page.lines):
            at_edge = i in edges
            if _STAMP.search(line) or _SITE.search(line):
                dropped = True
                continue
            if at_edge and (_PAGE_NUMBER.match(line) or _RUNNING_PART.match(line) or shape(line) in repeated):
                dropped = True
                continue
            lines.append(line.rstrip())
            xs.append(page.x0[i] if has_x0 else None)
            # space left by a removed header is not a paragraph break
            gaps.append(bool(gaps_in[i]) and not dropped and bool(lines[:-1]) if has_gap else False)
            dropped = False
        page.lines, page.x0, page.gap = lines, xs, gaps


_MALFORMED = re.compile(r"[A-Za-z][^A-Za-z0-9\s'’‘“”\"./,;:()\[\]&–—-]+[A-Za-z]|[a-z][A-Z][a-z]|[A-Za-z]\d[A-Za-z]|[A-Za-z][_~^|\\<>{}][A-Za-z]?")


def text_quality(pages: list[Page]) -> float:
    """Share of words that look like OCR damage: symbols or digits inside a word."""
    words = bad = 0
    for page in pages[:: max(1, len(pages) // 60)]:       # a sample is enough
        for line in page.lines:
            for word in line.split():
                if len(word) >= 4:
                    words += 1
                    bad += bool(_MALFORMED.search(word))
    return bad / words if words else 0.0


CACHE_VERSION = 5        # raise when the reading logic changes, so old caches are ignored


def _cache_file(cache_dir: Path, sha256: str, backend: str, ocr: bool) -> Path:
    return cache_dir / f"{sha256[:24]}-v{CACHE_VERSION}-{backend}-{'ocr' if ocr else 'noocr'}.json.gz"


def _load_cache(file: Path, sha256: str) -> Extraction | None:
    import gzip
    import json
    try:
        data = json.loads(gzip.decompress(file.read_bytes()))
        pages = [Page(**p) for p in data["pages"]]
        return Extraction(pages, data["backend"], sha256, data["warnings"])
    except Exception:           # unreadable or from another version: read the PDF again
        return None


def _save_cache(file: Path, extraction: Extraction) -> None:
    import dataclasses
    import gzip
    import json
    file.parent.mkdir(parents=True, exist_ok=True)
    data = {"backend": extraction.backend, "warnings": extraction.warnings,
            "pages": [dataclasses.asdict(p) for p in extraction.pages]}
    file.write_bytes(gzip.compress(json.dumps(data).encode()))


def extract_pdf(path: str | Path, *, backend: str = "auto", ocr: bool = True,
                cache_dir: str | Path | None = None) -> Extraction:
    """Read a judgment PDF. `backend` is 'auto', 'pdfplumber' or 'pymupdf'.

    With `cache_dir`, the result is stored and reused while the PDF is
    unchanged. OCR of a scanned judgment takes minutes; the cache makes
    every later run instant.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No PDF at {path}")
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    chosen = backend
    if backend == "auto":
        try:
            import pdfplumber  # noqa: F401
            chosen = "pdfplumber"
        except ImportError:
            chosen = "pymupdf"

    cache = _cache_file(Path(cache_dir), sha256, chosen, ocr) if cache_dir else None
    if cache and cache.exists():
        cached = _load_cache(cache, sha256)
        if cached:
            return cached

    if chosen == "pdfplumber":
        pages = _extract_pdfplumber(path, ocr)
    elif chosen == "pymupdf":
        pages = _extract_pymupdf(path, ocr)
    else:
        raise ValueError(f"Unknown backend {backend!r}")

    _strip_furniture(pages)

    warnings = []
    missing = [p.number for p in pages if p.needs_ocr]
    if missing:
        warnings.append(
            f"{len(missing)} page(s) are scans with no text and were not read by OCR: "
            f"{missing[:15]}{'...' if len(missing) > 15 else ''}. "
            "Install Tesseract and pytesseract (and do not pass --no-ocr), then re-run."
        )
    garbled = text_quality(pages)
    if garbled > 0.008:
        warnings.append(
            f"The text in this PDF looks like poor OCR: about {garbled:.0%} of words are malformed "
            "(for example 'REP?llTS', 'lcgisl_ativc'). Section numbers, names and citations taken "
            "from it will be unreliable. Use a cleaner copy of the judgment if one exists."
        )
    two_col = [p.number for p in pages if getattr(p, "columns", 1) == 2]
    if two_col:
        warnings.append(
            f"{len(two_col)} page(s) are set in two columns (a printed law report). The columns were "
            "read one after the other; check a page against the PDF."
        )
    recognised = [p.number for p in pages if p.ocr]
    if recognised:
        warnings.append(
            f"{len(recognised)} page(s) were read by OCR. Check section numbers and "
            "names on those pages; OCR confuses 0/O and 1/l."
        )
    extraction = Extraction(pages, chosen, sha256, warnings)
    if cache and not any(p.needs_ocr for p in pages):
        _save_cache(cache, extraction)
    return extraction
