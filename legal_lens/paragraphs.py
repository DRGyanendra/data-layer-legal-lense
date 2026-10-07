"""Split a judgment into opinions, and each opinion into its own paragraphs.

What real Supreme Court PDFs do, and how this copes:

- A judgment with several opinions repeats the cause title and the word
  'J U D G M E N T' before each one. Each such marker opens a new segment,
  and the judge's name on the next line is recorded.
- Judges number paragraphs differently. Most write '24. The ...'; some
  write '24 The ...' with no full stop; some leave the first paragraph
  unnumbered and start at '2.'. The style is worked out per opinion.
- Quoted extracts carry the paragraph numbers of the judgment they come
  from. Those are indented, so only numbers at the opinion's own left
  position count.
- Numbered lists inside a paragraph must not be taken as paragraphs.
- Section headings sit between paragraphs. They are lifted out of the text
  and recorded as the heading of what follows. If the opinion has an index
  of its headings, that index is used to recognise them.
- Older judgments have no paragraph numbers. They are split at paragraph
  indents, or at the blank space between paragraphs, and numbered by
  position, and marked 'synthetic' so nobody cites those numbers as the
  court's.
- A second opinion does not always get its own 'JUDGMENT' heading. A
  judge's name standing alone after the end of a sentence ('M. DAS, J.
  (dissenting)'), or opening a paragraph in the law-report manner
  ('SUBBA RAO, J.-This appeal ...'), starts a new opinion, and its
  paragraph numbers may carry on from the opinion before.
- Law reports (the SCR volumes) print a headnote before the judgment and a
  running header on every page. `law_report.py` deals with those.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_DOT = re.compile(r"^\s*(\d{1,4})\.(?!\d)\s*")                       # '24. The ...'
_BARE = re.compile(r"^\s*(\d{1,4})\s+(?=[A-Z“\"‘'(\[])")             # '24 The ...'
_BRACKET = re.compile(r"^\s*\[\s?(\d{1,4})\s?\]\.?\s*")                 # '[24] The ...'
_PAREN = re.compile(r"^\s*\(\s?(\d{1,4})\s?\)\.?\s*")                   # '(24) The ...'
_PARA_WORD = re.compile(r"^\s*(?:Para|Paragraph)\.?\s*(\d{1,4})[.:]?\s*", re.IGNORECASE)   # 'Para 24. The ...'
_STYLES = (("dot", _DOT), ("bare", _BARE), ("bracket", _BRACKET), ("paren", _PAREN), ("word", _PARA_WORD))
# 'The Judgment of the Court was delivered by' (law reports and old JUDIS text)
_DELIVERED = re.compile(
    r"^\W{0,3}\w{2,4}\s+(?:follow\w+\s+)?(?:ju\w{2,7}ts?|orders?|opinions?)\s+(?:of\s+the\s+court\s+)?"
    r"w\w{2,4}\s+deliv\w+(?:\s+by)?\s*[:.]?\s*(?P<rest>.*)$", re.IGNORECASE)
# 'SUBBA RAO, J.-This appeal ...': the judge's name in capitals opening a paragraph
_OPENING = re.compile(
    r"^\W{0,3}(?P<name>[A-Z][A-Za-z.'& ]{2,60}?),?\s*(?P<rank>C\.?\s?J\.?(?:\s?I\.?)?|JJ?\.?)\s*"
    r"(?P<note>\([^)]{0,60}\))?\s*[-—–:~]{1,2}\s*(?P<rest>\S.*)$")
_OPINION_NOTE = re.compile(r"concurr|dissent|himself|herself|majority|minority|partly|partially", re.IGNORECASE)
_MARKER = re.compile(r"^(?:JUDGMENT|ORDER|COMMONORDER|JUDGEMENT):?$")
_CAUSE_TITLE = re.compile(
    r"^(?:(?:IN THE )?SUPREME COURT OF INDIA|(?:NON[- ]?)?REPORTABLE|[A-Z /]{0,40}JURISDICTION)$", re.IGNORECASE)
_AUTHOR = re.compile(
    r"^(?:(?:Dr\.?|Hon'?ble|Justice|Mr\.?|Mrs\.?|Ms\.?)\s+)*[A-Z][A-Za-z.' -]{2,45}?,?\s*"
    r"(?:C\.?\s?J\.?\s?I\.?|J\.?)\s*(?:\(.{0,80}\))?\s*:?$"
)
_INDEX = re.compile(r"^(?:INDEX|CONTENTS|TABLE OF CONTENTS)(?:\s+(?:TO|OF)\s+THE\s+JUDGMENT)?$", re.IGNORECASE)
_INDEX_ENTRY = re.compile(r"^([A-Z]{1,2}(?:\.[\dIVX]+)?)[.)]?\s+(\S.*)$")
_LETTERED_HEADING = re.compile(r"^(?:[A-Z]{1,2}(?:\.[\dIVX]+)?|[IVX]{1,4})[.)]?\s+[A-Z‘'“\"][^.]{2,90}$")
_SENTENCE_END = (".", "?", "!", ":", '"', "”", "’", ")", "]", ";")
_SIGNATURE = re.compile(
    r"^[.…_\s]*(?:C\.?J\.?I\.?|J\.?)?[.…_\s]*$"                       # '..........J.'
    r"|^[\[(][A-Za-z .,'-]{3,45}[\])]$"                               # '(INDU MALHOTRA)'
    r"|^[^A-Z]{4,}(?:C\.?J\.?I|J)\.?$"                                # OCR of a dotted line: 'see eeeecee J.'
    r"|^(?:New Delhi|NEW DELHI)[;:,.]?$"
    r"|^(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}[.;]?$"
    r"|^\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+\d{4}[.;]?$",
)

MIN_NUMBERED_PARAGRAPHS = 3      # fewer than this in an opinion: treat it as unnumbered
MIN_BEFORE_RESTART = 4           # numbering must have reached this before a restart counts
MIN_RESTART_RUN = 4              # a restart must run 1, 2, 3, 4 at least
MAX_SKIP = 2                     # tolerate up to two missed numbers (OCR)
X_BIN = 8.0                      # paragraph numbers sit within this many points of each other


@dataclass
class Line:
    page: int
    text: str
    x0: float | None = None
    gap: bool = False            # extra vertical space above this line


@dataclass
class Paragraph:
    segment: int                 # 0 for the first opinion, 1 for the next, ...
    number: int                  # the judgment's own paragraph number
    text: str
    page: int                    # page on which the paragraph starts
    heading: str | None = None   # the section heading this paragraph sits under
    label: str = ""              # how the court numbers it: '24', or '5.1' for a sub-paragraph

    def __post_init__(self) -> None:
        if not self.label:
            self.label = str(self.number)


@dataclass
class Opinion:
    index: int
    author_line: str | None      # e.g. 'R.F. Nariman, J. (Concurring)'
    numbering: str               # 'original' or 'synthetic'
    style: str                   # 'dot', 'bare' or 'none'
    page: int
    paragraphs: int = 0
    first_number: int = 1        # above 1 when the numbering carries on from the opinion before
    approximate: bool = False    # the start was placed from the page header, not found in the text


@dataclass
class SplitResult:
    header: str                    # the cause title before the first opinion
    paragraphs: list[Paragraph]
    numbering: str                 # 'original', 'synthetic' or 'mixed'
    segments: int
    headings: list[str] = field(default_factory=list)
    opinions: list[Opinion] = field(default_factory=list)
    layout: str = "judgment"       # 'judgment', or 'law_report' for an SCR-style copy with a headnote
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.paragraphs)

    def numbering_of(self, segment: int) -> str:
        return self.opinions[segment].numbering if segment < len(self.opinions) else self.numbering


# ------------------------------------------------------------------ headings

def is_heading(text: str) -> bool:
    """A short line in capitals ('SUBMISSIONS') or lettered ('B. Analysis').

    Lines containing digits are rejected unless lettered: that rules out
    citations such as '(2005) 6 SCC 1', which are otherwise all capitals.
    """
    t = text.strip()
    if not 3 <= len(t) <= 90 or t[-1] in ".;,":
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 3:
        return False
    if _LETTERED_HEADING.match(t) and len(t.split()) <= 12:
        return True
    if any(c.isdigit() for c in t):
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.9


def _clean_heading(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip().rstrip(":")).strip("()[] ")
    t = re.sub(r"\s*[.…]{3,}.*$", "", t)                 # dot leaders from an index
    if re.fullmatch(r"(?:[A-Za-z] ){2,}[A-Za-z]", t):    # 'A N A L Y S I S'
        t = t.replace(" ", "")
    return t


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _is_known(text: str, known: set[str]) -> bool:
    """Is this line one of the headings listed in the opinion's index?

    A long heading wraps differently in the index and in the body, so a
    match on the opening words is enough.
    """
    key = _key(_clean_heading(text))
    if not key or not known:
        return False
    if key in known:
        return True
    return len(key) >= 12 and any(k.startswith(key) or key.startswith(k) for k in known if len(k) >= 12)


# ------------------------------------------------------- paragraph numbering

def _candidates(lines: list[Line], pattern: re.Pattern[str]) -> list[tuple[int, int, float | None]]:
    """(line index, number, x0) for every line that starts like a paragraph."""
    found = []
    for i, line in enumerate(lines):
        m = pattern.match(line.text)
        if m:
            found.append((i, int(m.group(1)), line.x0))
    return found


def _is_restart(cands, at: int, expected: int) -> bool:
    """Does the '1.' at cands[at] open a new opinion, or is it a list?

    Follow the run 1, 2, 3, ... from here. A list inside a paragraph is short
    and the outer numbering (`expected`) comes back right after it. A new
    opinion keeps counting.
    """
    run = 1
    for _, number, _ in cands[at + 1:]:
        if number == run + 1:
            run += 1
        elif number == expected and run + 1 != expected:
            return False
    return run >= MIN_RESTART_RUN


def _confirms_skip(cands, at: int, expected: int) -> bool:
    """A number was skipped. Accept only if the sequence carries on from here."""
    number = cands[at][1]
    for _, later, _ in cands[at + 1:]:
        if later == expected:
            return False
        if later == number + 1:
            return True
    return False


def _chain(cands, allow_start_at_two: bool, first: int = 1) -> list[tuple[int, int, int]]:
    """(line index, sub-segment, number) for each real paragraph start.

    `first` is the number the opinion is expected to open with: 1, or the next
    number when an opinion carries on the numbering of the one before it.
    """
    starts: list[tuple[int, int, int]] = []
    expected, sub = first, 0
    allow_start_at_two = allow_start_at_two and first == 1
    for at, (line_index, number, _) in enumerate(cands):
        if number == expected:
            starts.append((line_index, sub, number))
            expected = number + 1
        elif not starts and number == 2 and allow_start_at_two and _confirms_skip(cands, at, 1):
            starts.append((line_index, sub, number))       # first paragraph is unnumbered
            expected = 3
        elif expected < number <= expected + MAX_SKIP and starts and _confirms_skip(cands, at, expected):
            starts.append((line_index, sub, number))
            expected = number + 1
        elif number == 1 and expected > MIN_BEFORE_RESTART and _is_restart(cands, at, expected):
            sub += 1
            starts.append((line_index, sub, number))
            expected = 2
    return starts


def _best_chain(lines: list[Line], first: int = 1) -> tuple[list[tuple[int, int, int]], str]:
    """Try every numbering style and every left position; keep the longest chain."""
    best: list[tuple[int, int, int]] = []
    best_style = "none"
    for style, pattern in _STYLES:
        cands = _candidates(lines, pattern)
        if not cands:
            continue
        positions = [c[2] for c in cands if c[2] is not None]
        groups = [cands]
        if len(positions) >= 0.8 * len(cands):
            bins = sorted({round(x / X_BIN) for x in positions})
            groups = [[c for c in cands if c[2] is not None and abs(c[2] - b * X_BIN) <= X_BIN] for b in bins]
        for group in groups:
            chain = _fill_gaps(_chain(group, allow_start_at_two=True, first=first), cands)
            # prefer the earlier style on a tie: a dotted number is hardest to mistake
            if len(chain) > len(best):
                best, best_style = chain, style
    return best, best_style


def _is_paragraph_numbering(chain, total_lines: int, style: str = "dot") -> bool:
    """Is this chain the judgment's paragraph numbering, or just a numbered list?

    An unnumbered judgment still contains lists ('1. duty 2. breach 3. damage').
    Real paragraph numbers run through the opinion; a list sits in one place.
    """
    if len(chain) < MIN_NUMBERED_PARAGRAPHS:
        return False
    # '(1)' and '[1]' are also how sub-sections, list items and footnote marks are
    # written. Accept them as paragraph numbers only when there are enough of them
    # to account for the whole opinion.
    if style in ("paren", "bracket", "word") and len(chain) < total_lines / 60:
        return False
    if total_lines < 60:
        return True
    spread = (chain[-1][0] - chain[0][0]) / total_lines
    starts_early = chain[0][0] <= 0.45 * total_lines
    longest = max(b[0] - a[0] for a, b in zip(chain, chain[1:]))
    # one 'paragraph' swallowing a third of the opinion means two list items far apart
    return spread >= 0.4 and starts_early and longest <= 0.35 * total_lines


def _fill_gaps(chain, cands):
    """A paragraph number typed at a different indent is missed by the position
    filter. If exactly that number sits between its neighbours, take it back."""
    if len(chain) < 2:
        return chain
    filled = [chain[0]]
    for prev, nxt in zip(chain, chain[1:]):
        if nxt[1] == prev[1] and nxt[2] > prev[2] + 1:
            for missing in range(prev[2] + 1, nxt[2]):
                hit = next((c for c in cands if c[1] == missing and filled[-1][0] < c[0] < nxt[0]), None)
                if hit:
                    filled.append((hit[0], prev[1], missing))
        filled.append(nxt)
    return filled


# ----------------------------------------------------------------- segments

def _is_marker(text: str) -> bool:
    squeezed = re.sub(r"\s+", "", text)
    return bool(_MARKER.match(squeezed)) and text.strip() == text.strip().upper()


def _find_opinions(lines: list[Line]) -> list[tuple[int, int]]:
    """(start of the cause title, index of the marker line) for each opinion."""
    found: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _is_marker(line.text):
            continue
        if found and i - found[-1][1] <= 6:
            found[-1] = (found[-1][0], i)          # 'JUDGMENT:' then 'J U D G M E N T'
            continue
        if not found:
            found.append((0, i))
            continue
        # The cause title repeated above this marker: the run of title lines nearest to it.
        low = max(found[-1][1] + 1, i - 40)
        titled = [j for j in range(low, i) if _CAUSE_TITLE.match(lines[j].text.strip())]
        if titled:
            start = titled[-1]
            for j in reversed(titled[:-1]):
                if start - j > 4:
                    break
                start = j
            found.append((start, i))
    return found


def _caps_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


def _opening(text: str) -> tuple[str, str] | None:
    """'SUBBA RAO, J.-This appeal ...' -> ('SUBBA RAO, J.', 'This appeal ...').

    Only a name in capitals counts. 'Bachawat, J.: held that' in running text
    is a reference to a judge, not the start of his opinion.
    """
    m = _OPENING.match(text.strip())
    if not m or _caps_share(m.group("name")) < 0.75 or len(re.sub(r"[^A-Za-z]", "", m.group("name"))) < 3:
        return None
    author = f"{m.group('name').strip()}, {m.group('rank').strip()}"
    if m.group("note"):
        author += " " + m.group("note")
    return author, m.group("rest").strip()


def _prepare(lines: list[Line]) -> list[Line]:
    """Normalise how an opinion opens.

    'The Judgment of the Court was delivered by' is dropped, and a name that
    opens the first paragraph is put on a line of its own, so the rest of the
    code sees every opinion start the same way.
    """
    out: list[Line] = []
    seen = 0
    for line in lines:
        text = line.text.strip()
        if text:
            seen += 1
        if seen <= 12 and text:
            delivered = _DELIVERED.match(text)
            if delivered:
                rest = delivered.group("rest").strip()
                if rest:
                    out.append(Line(line.page, rest, line.x0, line.gap))
                continue
            opened = _opening(text)
            if opened:
                out.append(Line(line.page, opened[0], line.x0, line.gap))
                out.append(Line(line.page, opened[1], line.x0, False))
                seen = 99
                continue
        out.append(line)
    return out


def opinion_breaks(lines: list[Line]) -> list[int]:
    """Line indexes where a further opinion starts without a 'JUDGMENT' heading.

    Two forms: a judge's name on a line of its own after the end of a
    sentence, and a name in capitals opening a paragraph ('RAY, J.-The ...').
    """
    breaks: list[int] = []
    content = [i for i, l in enumerate(lines) if l.text.strip()]
    for pos, i in enumerate(content):
        if pos < 8 or pos + 3 >= len(content):
            continue                                   # the opinion's own name, or a closing signature
        text = lines[i].text.strip()
        before = lines[content[pos - 1]].text.strip()
        after = lines[content[pos + 1]].text.strip()
        ended = before.endswith(_SENTENCE_END) or bool(_SIGNATURE.match(before))
        if not ended:
            continue
        if _opening(text):
            breaks.append(i)
            continue
        if not _AUTHOR.match(text) or _is_marker(text) or _SIGNATURE.match(after):
            continue
        name = re.split(r",|\bC\.?\s?J|\bJ\.", text)[0]
        note = re.search(r"\((.*)\)", text)
        if _caps_share(name) >= 0.8 or (note and _OPINION_NOTE.search(note.group(1))) or _SIGNATURE.match(before):
            breaks.append(i)
    return breaks


def _strip_signature(block: list[Line]) -> list[Line]:
    """Remove the dotted signature lines, judge names, place and date at the end."""
    block = list(block)
    while len(block) > 1 and (not block[-1].text.strip() or _SIGNATURE.match(block[-1].text.strip())):
        block.pop()
    return block


def _join(lines: list[Line]) -> str:
    return re.sub(r"\s+", " ", " ".join(l.text.strip() for l in lines if l.text.strip())).strip()


def _split_trailing_headings(block: list[Line], known: set[str]) -> tuple[list[Line], list[str]]:
    """Peel heading lines off the end of a paragraph's lines.

    A heading is only taken when the line before it ends a sentence, so the
    capitalised tail of a wrapped sentence is never mistaken for one.
    """
    content = [l for l in block if l.text.strip()]
    headings: list[str] = []
    # A heading from the index that wraps onto a second or third line.
    for at in range(len(content) - 2, max(len(content) - 4, 0), -1):
        joined = _key(_clean_heading(" ".join(l.text.strip() for l in content[at:])))
        if _is_known(content[at].text, known) and any(k.startswith(joined) for k in known):
            headings = [_clean_heading(" ".join(l.text.strip() for l in content[at:]))]
            content = content[:at]
            break
    while len(content) > 1:
        last = content[-1].text
        if not (_is_known(last, known) or is_heading(last)):
            break
        before = content[-2].text.rstrip()
        if not (before.endswith(_SENTENCE_END) or is_heading(before) or _is_known(before, known)):
            break
        headings.insert(0, _clean_heading(content.pop().text))
    return content, headings


_SUB = re.compile(r"^\s*(\d{1,3})\.(\d{1,2})\.?\s+(?=\S)")
_TITLE = re.compile(r"^\s*\d{1,4}\.?\s+([A-Z][A-Z0-9 ,&'’–—:/()-]{5,90})$")


def _cut_known_heading(content: list[Line], known: set[str]) -> tuple[list[Line], str | None]:
    """A heading from the index in the middle of a block, followed by an epigraph.

    The heading is removed and returned; the epigraph stays where it is.
    """
    for i in range(len(content) - 1, 0, -1):
        text = content[i].text.strip()
        if _is_known(text, known) and len(text) <= 90 and not text.endswith("."):
            return content[:i] + content[i + 1:], _clean_heading(text)
    return content, None


def _sub_paragraphs(content: list[Line], number: int) -> list[tuple[str, list[Line]]]:
    """Split '5. TITLE / 5.1. ... / 5.2. ...' into its sub-paragraphs.

    Returns (label, lines) pairs; a single pair when there are no sub-paragraphs.
    """
    cuts = []
    expected = 1
    for i, line in enumerate(content):
        m = _SUB.match(line.text)
        if m and int(m.group(1)) == number and int(m.group(2)) == expected:
            cuts.append(i)
            expected += 1
    if len(cuts) < 2:
        return [(str(number), content)]
    pieces = []
    if _join(content[:cuts[0]]) and cuts[0] > 0:
        pieces.append((str(number), content[:cuts[0]]))
    for n, start in enumerate(cuts):
        end = cuts[n + 1] if n + 1 < len(cuts) else len(content)
        pieces.append((f"{number}.{n + 1}", content[start:end]))
    return pieces


def _lead_in(lead: list[Line]) -> tuple[str | None, set[str], list[Line], str | None]:
    """Read what sits between the marker and the first numbered paragraph.

    Returns the author line, the headings listed in an index, any body text
    (an unnumbered first paragraph), and the heading in force at the end.
    """
    author = None
    known: set[str] = set()
    def squeezed(text: str) -> str:              # 'C O N T E N T S' -> 'CONTENTS'
        t = text.strip()
        return t.replace(" ", "") if re.fullmatch(r"(?:[A-Za-z] ){2,}[A-Za-z]", t) else t

    index_at = next((i for i, l in enumerate(lead) if _INDEX.match(squeezed(l.text))), None)
    entries: list[str] = []
    body_from = 0
    if index_at is not None:
        for i in range(index_at + 1, len(lead)):
            text = lead[i].text.strip()
            body_from = i
            if _AUTHOR.match(text):
                break
            if _INDEX_ENTRY.match(text):
                title = _key(_clean_heading(text))
                if len(title) >= 8 and any(_key(e) == title or _key(e).startswith(title) for e in entries):
                    break                                   # a heading seen again: the index is over
                entries.append(_clean_heading(text))
            elif entries and text:
                entries[-1] += " " + _clean_heading(text)   # an entry that wraps onto the next line
            body_from = i + 1
        known = {_key(e) for e in entries}
    # The judge's name heads the opinion: within its first lines, or right after its
    # index. A name further down is a signature, not a heading.
    limit = 8 if index_at is None else max(8, body_from + 2)
    for i, l in enumerate(lead[:limit]):
        if _AUTHOR.match(l.text.strip()) and not _is_marker(l.text):
            author = l.text.strip()
            body_from = max(body_from, i + 1)
            break

    body: list[Line] = []
    heading = None
    for l in lead[body_from:]:
        text = l.text.strip()
        if not text:
            continue
        if _INDEX.match(squeezed(text)):
            continue
        if _is_known(text, known) or (is_heading(text) and not body):
            heading = _clean_heading(text)
            continue
        if _AUTHOR.match(text) and not body:
            author = author or text
            continue
        body.append(l)
    return author, known, body, heading


def _indent_paragraphs(lines: list[Line], segment: int, heading: str | None) -> list[Paragraph]:
    """Unnumbered text: start a paragraph at each first-line indent.

    Falls back to packing lines into blocks when positions are unknown or
    the text has no indents.
    """
    content = [l for l in lines if l.text.strip()]
    if not content:
        return []
    positions = [round(l.x0) for l in content if l.x0 is not None]
    blocks: list[list[Line]] = []
    if len(positions) >= 0.8 * len(content):
        margin = Counter(positions).most_common(1)[0][0]
        for line in content:
            indented = line.x0 is not None and line.x0 - margin > 6
            prev_ends = bool(blocks) and blocks[-1][-1].text.rstrip().endswith(_SENTENCE_END)
            if not blocks or (indented and prev_ends):
                blocks.append([line])
            else:
                blocks[-1].append(line)
    enough = max(3, len(content) // 60)
    if len(blocks) < enough:                               # no indents: paragraphs set apart by blank space
        spaced: list[list[Line]] = []
        for line in content:
            prev_ends = bool(spaced) and spaced[-1][-1].text.rstrip().endswith(_SENTENCE_END)
            if not spaced or (line.gap and prev_ends):
                spaced.append([line])
            else:
                spaced[-1].append(line)
        if len(spaced) >= enough:
            blocks = spaced
    if len(blocks) < enough:                               # neither: pack by size
        blocks, size = [[]], 0
        for line in content:
            blocks[-1].append(line)
            size += len(line.text) + 1
            if size >= 1400 and line.text.rstrip().endswith((".", "?", '"', "”")) or size >= 2800:
                blocks.append([])
                size = 0
        blocks = [b for b in blocks if b]
    return [Paragraph(segment, n, _join(b), b[0].page, heading) for n, b in enumerate(blocks, start=1)]


def _split_opinion(lines: list[Line], first_segment: int,
                   continue_from: int | None = None) -> tuple[list[Paragraph], list[Opinion], list[str]]:
    """Paragraphs of one opinion. May yield more than one segment if the
    numbering restarts inside it. `continue_from` is the last paragraph number
    of the opinion before, tried when this one does not start again at 1."""
    lines = _prepare(lines)
    starts, style = _best_chain(lines)
    numbered = _is_paragraph_numbering(starts, len(lines), style)
    if not numbered and style in ("paren", "bracket", "word"):
        # the longest chain was a list in brackets; the real numbering may be a shorter dotted one
        for name, pattern in _STYLES[:2]:
            cands = _candidates(lines, pattern)
            chain = _fill_gaps(_chain(cands, allow_start_at_two=True), cands)
            if _is_paragraph_numbering(chain, len(lines), name):
                starts, style, numbered = chain, name, True
                break
    first_number = 1
    if not numbered and continue_from:
        carried, carried_style = _best_chain(lines, first=continue_from + 1)
        if _is_paragraph_numbering(carried, len(lines), carried_style):
            starts, style, numbered, first_number = carried, carried_style, True, continue_from + 1
    lead = lines[:starts[0][0]] if numbered else lines[:80]
    author, known, lead_body, heading = _lead_in(lead)
    page = next((l.page for l in lines if l.text.strip()), 0)
    found: list[str] = [heading] if heading else []

    if not numbered:
        body_lines = lines
        if author:
            at = next(i for i, l in enumerate(lines) if l.text.strip() == author)
            above = [l for l in lines[:at] if l.text.strip() and not is_heading(l.text)]
            if above:                                  # text comes first: that name is a signature
                author = None
            else:                                      # skip the author line and the headings above it
                body_lines = lines[at + 1:]
        paragraphs = _indent_paragraphs(_strip_signature(body_lines), first_segment, None)
        return paragraphs, [Opinion(first_segment, author, "synthetic", "none", page, len(paragraphs))], []

    paragraphs: list[Paragraph] = []
    opinions = [Opinion(first_segment, author, "original", style, page, first_number=first_number)]
    current = heading
    if starts[0][2] == 2 and first_number == 1 and lead_body:   # the unnumbered first paragraph
        content, trailing = _split_trailing_headings(lead_body, known)
        paragraphs.append(Paragraph(first_segment, 1, _join(content), content[0].page, heading))
        if trailing:
            current = trailing[-1]
            found.extend(trailing)
    for position, (line_index, sub, number) in enumerate(starts):
        segment = first_segment + sub
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = lines[line_index:end]
        if position + 1 == len(starts) or starts[position + 1][1] != sub:
            block = _strip_signature(block)
        content, trailing = _split_trailing_headings(block, known)
        inner = None
        if not trailing and known:
            content, inner = _cut_known_heading(content, known)
        if segment >= first_segment + len(opinions):
            opinions.append(Opinion(segment, None, "original", style, content[0].page))
            current = None                               # headings do not carry into the next opinion
        # '5. HISTORICAL BACKGROUND' is a title for that paragraph and its sub-paragraphs.
        title = _TITLE.match(content[0].text)
        own_heading = _clean_heading(title.group(1)) if title and len(content) > 1 else None
        if own_heading:
            found.append(own_heading)
        for label, piece in _sub_paragraphs(content, number):
            if own_heading and label == str(number) and len(piece) == 1:
                continue                                 # the title line alone is not a paragraph
            paragraphs.append(Paragraph(segment, number, _join(piece), piece[0].page,
                                        own_heading or current, label))
        if (trailing or inner) and position + 1 < len(starts):
            current = trailing[-1] if trailing else inner
            found.extend(trailing or [inner])
    for opinion in opinions:
        opinion.paragraphs = sum(1 for p in paragraphs if p.segment == opinion.index)
    return paragraphs, opinions, found


def _cut_at_breaks(span: list[Line]) -> list[list[Line]]:
    """Split one stretch of text where a further opinion starts unannounced."""
    cuts = [0] + opinion_breaks(span) + [len(span)]
    return [span[a:b] for a, b in zip(cuts, cuts[1:]) if any(l.text.strip() for l in span[a:b])]


def split_paragraphs(lines: list[Line]) -> SplitResult:
    """Split cleaned judgment lines into opinions and numbered paragraphs."""
    from .law_report import read_law_report

    notes: list[str] = []
    layout = "judgment"
    given_authors: dict[int, tuple[str | None, bool]] = {}      # span index -> (author, approximate)
    report = read_law_report(lines)
    if report is not None:
        layout = "law_report"
        header = report.header
        notes = list(report.notes)
        spans = []
        for author, approximate, part in report.spans:
            given_authors[len(spans)] = (author, approximate)
            spans.append(part)
        marks = []
    else:
        marks = _find_opinions(lines)
        # A first marker far into the document is not the start of the judgment: a
        # law report puts its only 'ORDER' at the very end. Everything before it
        # must still be read, as an opinion without a marker.
        late = bool(marks) and marks[0][1] > max(400, 0.2 * len(lines))
        if marks and not late:
            header = _join(lines[:marks[0][1]])
            spans = []
            for n, (title_at, marker_at) in enumerate(marks):
                end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
                spans.append(lines[marker_at + 1:end])
        elif late:
            header = ""
            marks = [(marks[0][1], marks[0][1])] + marks[1:]
            spans = [lines[:marks[0][0]]]
            for n, (title_at, marker_at) in enumerate(marks):
                end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
                spans.append(lines[marker_at + 1:end])
        else:
            header, spans = "", [lines]

    paragraphs: list[Paragraph] = []
    opinions: list[Opinion] = []
    headings: list[str] = []
    for at, span in enumerate(spans):
        last_number = None
        # a law report's opinions are already separated; elsewhere look for unannounced ones
        parts = [span] if report is not None else _cut_at_breaks(span)
        for part in parts:
            paras, ops, found = _split_opinion(part, len(opinions), continue_from=last_number)
            if not paras:
                continue
            if at in given_authors and part is span:
                author, approximate = given_authors[at]
                ops[0].author_line = ops[0].author_line or author
                ops[0].approximate = approximate
            paragraphs.extend(paras)
            opinions.extend(ops)
            headings.extend(found)
            last_number = paras[-1].number if ops[-1].numbering == "original" else None

    if report is None and not marks and opinions and opinions[0].numbering == "original" and paragraphs:
        first_line = next((i for i, l in enumerate(lines) if l.text.strip() and paragraphs[0].text.startswith(l.text.strip()[:30])), 0)
        header = _join(lines[:first_line])

    kinds = {o.numbering for o in opinions}
    numbering = kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else "synthetic")
    return SplitResult(header, paragraphs, numbering, len(opinions), headings, opinions, layout, notes)
