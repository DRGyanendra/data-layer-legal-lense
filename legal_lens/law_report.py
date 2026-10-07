"""Law-report copies of a judgment (the Supreme Court Reports volumes).

A report is not laid out like the court's own PDF:

- It opens with the parties, the date, the bench and then a headnote written
  by the reporter. The headnote is not the court's text and must not be
  chunked or cited as if it were.
- Every page carries a running header: 'SUPREME COURT REPORTS [1973] Supp.
  S.C.R.' on one side and 'KESAVANANDA v. KERALA (Khanna, J.)' on the other.
  Left in, these land in the middle of sentences.
- Opinions are not headed 'JUDGMENT'. Each begins with the judge's name in
  capitals: 'RAY, J.-The validity of ...'. The running header names the
  judge whose opinion is on the page, which survives bad OCR far better
  than the opening line does.
- Paragraphs are usually unnumbered.

`read_law_report` returns None for anything that is not a report.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .paragraphs import _DELIVERED, Line, _caps_share, _join, _opening, opinion_breaks

_JUDGE_HEADER = re.compile(r"\(\s*([A-Za-z][A-Za-z .&]{1,40}?),?\s*(C\.?\s?J|JJ?)\.?\s*\)\s*\W*\d{0,4}\W*$")
_PAGE_ONLY = re.compile(r"^\W*\d{1,4}\s?[a-z]?\W*$")
_MARGIN_LETTER = re.compile(r"^\W*[A-H]\W*$")
_JURISDICTION = re.compile(
    r"^\W{0,3}(?:(?:CIVIL|CRIMINAL)\s+)?(?:APPELLATE|ORIGINAL|ADVISORY|EXTRAORDINARY|INHERENT|REVIEW)\s+"
    r"JURISDICTION\s*[:.-]", re.IGNORECASE)
_NAME_AT_START = re.compile(r"^\W{0,3}([A-Z][A-Za-z.&' ]{2,45}?)\s*(?:,|\bJJ?\b|\bC\.?\s?J\b|$)")
_NAME_TAIL = re.compile(r"^\s*,?\s*(?:C\.?\s?J\.?|JJ?\.?|[/1lI]\.)?\s*[-—–:~.]{0,3}\s*")
TOP = 3                      # running headers sit in the first lines of a page


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _letters(a), _letters(b)).ratio()


def _is_report_header(text: str) -> bool:
    """'40 SUPREME COURT REPORTS [1973] Supp. S.C.R.', however the OCR spelt it."""
    name = re.sub(r"[^A-Za-z]", "", text).upper()
    return len(name) >= 15 and SequenceMatcher(None, name[:19], "SUPREMECOURTREPORTS").ratio() >= 0.72


def _names_match(candidate: str, judge: str) -> bool:
    """'REDoY' against 'Jaganmohan Reddy'; 'HEGDE AND MuKHERJEA' against 'Hegde & Mukherjea'."""
    parts = [judge] + [w for w in re.split(r"[\s&]+", judge) if len(w) >= 3]
    return max(_similar(candidate, part) for part in parts) >= 0.6


def _opening_for(body: list[Line], first_page: int, last_page: int, judge: str) -> tuple[int, str] | None:
    """The line where `judge` begins, searched for by name between two pages.

    OCR often loses the 'J.-' after the name, or puts the name on a line of its
    own, so only the name in capitals at the start of a line is required.
    Returns the line index and what is left of the line after the name.
    """
    for i, line in enumerate(body):
        if line.page < first_page:
            continue
        if line.page > last_page:
            break
        head = _NAME_AT_START.match(line.text.strip())
        if not head:
            continue
        name = head.group(1)
        if _caps_share(name) >= 0.6 and len(_letters(name)) >= 3 and _names_match(name, judge):
            rest = line.text.strip()[head.end(1):]
            return i, _NAME_TAIL.sub("", rest, count=1)
    return None


@dataclass
class LawReport:
    header: str                                          # title, bench, headnote, counsel
    spans: list[tuple[str | None, bool, list[Line]]]     # (author, start is approximate, lines) per opinion
    notes: list[str] = field(default_factory=list)


def read_law_report(lines: list[Line]) -> LawReport | None:
    pages: dict[int, list[int]] = defaultdict(list)
    for i, line in enumerate(lines):
        if line.text.strip():
            pages[line.page].append(i)
    if len(pages) < 2:
        return None
    tops = {page: idx[:TOP] for page, idx in pages.items()}
    report_pages = sum(any(_is_report_header(lines[i].text) for i in idx) for idx in tops.values())
    judge_pages = sum(any(_JUDGE_HEADER.search(lines[i].text) and len(lines[i].text) <= 70 for i in idx)
                      for idx in tops.values())
    if report_pages < max(2, 0.15 * len(pages)) and judge_pages < max(3, 0.25 * len(pages)):
        return None

    # ---- the short title printed opposite the report header: 'RAM LAL v. STATE'
    candidates = []
    for idx in tops.values():
        for i in idx:
            text = _JUDGE_HEADER.sub("", lines[i].text).strip()
            if 6 <= len(text) <= 60 and _caps_share(text) >= 0.6 and not _is_report_header(text):
                candidates.append((i, re.sub(r"[\W\d]+$", "", text)))
    clusters: list[list[tuple[int, str]]] = []
    for item in candidates:
        home = next((c for c in clusters if _similar(c[0][1], item[1]) >= 0.6), None)
        if home is None:
            clusters.append([item])
        else:
            home.append(item)
    title_lines: set[int] = set()
    for cluster in clusters:
        if len(cluster) >= max(2, 0.15 * len(pages)):
            title_lines.update(i for i, _ in cluster)

    # ---- drop the furniture, remembering which judge each page header names
    judge_on_page: dict[int, str] = {}
    rank_on_page: dict[int, str] = {}
    furniture: set[int] = set()
    for page, idx in tops.items():
        for i in idx:
            text = lines[i].text.strip()
            named = _JUDGE_HEADER.search(text)
            if named and len(text) <= 70:
                judge_on_page[page] = named.group(1).strip()
                rank_on_page[page] = "C.J." if "C" in named.group(2).upper() else ("JJ." if "JJ" in named.group(2).upper() else "J.")
                furniture.add(i)
            elif _is_report_header(text) or i in title_lines or _PAGE_ONLY.match(text):
                furniture.add(i)
    for i, line in enumerate(lines):
        text = line.text.strip()
        if text and (_MARGIN_LETTER.match(text) or not re.search(r"[A-Za-z0-9]", text)):
            furniture.add(i)                 # margin letters A-H and specks from the scan

    # ---- where the court's text begins
    start = None
    jurisdiction = next((i for i, l in enumerate(lines) if _JURISDICTION.match(l.text.strip())), None)
    search_from = jurisdiction if jurisdiction is not None else 0
    for i in range(search_from, len(lines)):
        if i in furniture:
            continue
        text = lines[i].text.strip()
        if _DELIVERED.match(text) or _opening(text):
            start = i
            break
    notes: list[str] = []
    if start is None:
        start = jurisdiction if jurisdiction is not None else 0
        notes.append(
            "This is a law-report copy, but the point where the headnote ends and the judgment "
            "begins was not found. The headnote may be mixed into the first chunks."
        )
    header = _join([l for i, l in enumerate(lines[:start]) if i not in furniture])
    body_index = [i for i in range(start, len(lines)) if i not in furniture]
    body = [lines[i] for i in body_index]
    first_page = lines[start].page

    # ---- opinions from the running header: runs of pages naming the same judge
    runs: list[dict] = []
    named_pages = sorted((p, n) for p, n in judge_on_page.items() if p >= first_page)
    for at, (page, name) in enumerate(named_pages):
        if len(_letters(name)) < 3:
            continue                                         # OCR left too little of the name
        if runs and _similar(name, runs[-1]["names"].most_common(1)[0][0]) >= 0.55:
            runs[-1]["names"][name] += 1
            runs[-1]["ranks"][rank_on_page[page]] += 1
            runs[-1]["last"] = page
            continue
        ahead = [n for _, n in named_pages[at + 1:at + 5]]
        persists = any(_similar(name, n) >= 0.55 for n in ahead) or not ahead
        if runs and not persists:
            runs[-1]["last"] = page                          # one garbled header, not a new judge
            continue
        runs.append({"names": Counter({name: 1}), "ranks": Counter({rank_on_page[page]: 1}), "first": page, "last": page})

    # ---- opinions from the text: 'RAY, J.-The validity ...'
    text_starts = {i: (_opening(body[i].text) or (body[i].text.strip(), ""))[0] for i in opinion_breaks(body)}
    starts: list[tuple[int, str | None, bool]] = [(0, None, False)]
    previous_last = first_page
    for n, run in enumerate(runs):
        author = run["names"].most_common(1)[0][0] + ", " + run["ranks"].most_common(1)[0][0]
        if n == 0:
            starts[0] = (0, author, False)
            previous_last = run["last"]
            continue
        judge = run["names"].most_common(1)[0][0]
        found = _opening_for(body, previous_last, run["first"] + 1, judge)
        if found and found[0] > starts[-1][0]:
            at, rest = found
            body[at] = Line(body[at].page, rest, body[at].x0, body[at].gap)     # the name itself is not text
            starts.append((at, author, False))
        else:
            # The opening line was lost to OCR. An opinion starts on the page before its
            # first header (headers sit on alternate pages), if that page is not the last
            # page of the opinion before.
            page = run["first"] - 1 if run["first"] - 1 > previous_last else run["first"]
            at = next((i for i, l in enumerate(body) if l.page >= page), None)
            if at is not None and at > starts[-1][0]:
                starts.append((at, author, True))
        previous_last = run["last"]
    used = {s[0] for s in starts}
    for i, author in text_starts.items():                    # openings no header run accounts for
        near = any(abs(body[i].page - body[u].page) <= 1 for u in used if u)
        if i not in used and not near:
            starts.append((i, author, False))
    starts.sort(key=lambda item: item[0])

    spans = []
    for n, (at, author, approximate) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(body)
        if any(l.text.strip() for l in body[at:end]):
            spans.append((author, approximate, body[at:end]))
    guessed = sum(1 for _, approximate, _ in spans if approximate)
    if guessed:
        notes.append(
            f"{guessed} of {len(spans)} opinions were placed from the page header because the judge's "
            "opening line could not be read. Up to a page of text near each of those starts may be "
            "attributed to the wrong judge."
        )
    notes.append(
        "This is a law-report copy. The reporter's headnote and the page headers were left out; "
        "page numbers in citations are pages of this PDF, not of the report."
    )
    return LawReport(header, spans, notes)
