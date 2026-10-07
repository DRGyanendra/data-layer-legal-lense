"""Read a judgment's own details from its text: parties, date, bench, citation.

`data/cases.yaml` is still the checked record and always wins. This module
is for every judgment that has no entry there, so that any Supreme Court
PDF can be chunked and loaded without someone typing its details first.

Four layouts are understood:

- the court's own PDF: 'IN THE SUPREME COURT OF INDIA ... X ...Petitioner
  VERSUS Y ...Respondent', signatures and 'New Delhi; September 26, 2018'
  at the end;
- the old JUDIS text: 'PETITIONER:', 'RESPONDENT:', 'DATE OF JUDGMENT:',
  'BENCH:' labels at the top;
- a law report (SCR): parties, 'v.', date and the bench in brackets, then a
  headnote;
- an Indian Kanoon printout: 'X vs Y on 5 August, 2005', 'Bench: ...'.

Everything found is a reading of the page, not a checked fact. `missing`
lists what could not be found, and the quality verdict reports it.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from .ids import slugify
from .manifest import CaseMeta, Opinion
from .paragraphs import Line, SplitResult

_MONTHS = ("january february march april may june july august september october november december").split()
_MONTH = "|".join(_MONTHS)
_DATE_MDY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*((?:19|20)\d{{2}})\b", re.IGNORECASE)
_DATE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?({_MONTH})\s*,?\s*((?:19|20)\d{{2}})\b", re.IGNORECASE)
_DATE_NUM = re.compile(r"\b(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*((?:19|20)\d{2})\b")
_DATE_PREFIX = re.compile(r"^(?:new\s+delhi|dated?|date\s+of\s+(?:judge?ment|order|decision))\s*[;:,.-]?\s*", re.IGNORECASE)

_VERSUS = re.compile(r"^\W{0,3}(?:versus|vs?\.?|v/s\.?|u\.)\W{0,3}$", re.IGNORECASE)
_CASE_NUMBER = re.compile(
    r"\b(?:petition|appeal|case|reference|suit|application|s\.?l\.?p|w\.?p|c\.?a|crl\.?a)\b.*\b(?:nos?\.?|of\s+(?:19|20)\d{2})"
    r"|diary\s+no|\b\d{1,6}\s*/\s*(?:19|20)\d{2}\b", re.IGNORECASE)
_NOT_A_PARTY = re.compile(
    r"^(?:(?:non[- ]?)?reportable|in\s+the\s+.*court.*|.*jurisdiction|with|and|in|item\s+no.*|court\s+no.*|"
    r"(?:19|20)\d{2}\s+insc\s+\d+|.*\binsc\b.*|j\s?u\s?d\s?g\s?e?\s?m\s?e\s?n\s?t|o\s?r\s?d\s?e\s?r|coram.*|"
    r"\(.*jj?\.?\)|date\s*:.*|\W*)$", re.IGNORECASE)
_ROLE = re.compile(
    r"\s*(?:[.…·\s]{2,}|…|\.\.+)?\s*\b(?:petitioners?|appellants?|respondents?|applicants?|plaintiffs?|defendants?|"
    r"complainants?)\s*(?:\(\s*s\s*\))?\s*\.?$", re.IGNORECASE)
_AND_OTHERS = re.compile(r"\s*(?:,|&|\band\b)\s*(?:ors?|others?|anr|another|etc)\b\.?.*$", re.IGNORECASE)
_THROUGH = re.compile(r"\s+(?:thr|through|rep\.?\s+by|represented\s+by|by\s+its|by\s+lrs?)\b.*$", re.IGNORECASE)
_SMALL_WORDS = {"of", "and", "the", "in", "for", "on", "by", "to", "at", "v", "vs", "through"}

_SIGN_LINE = re.compile(r"^[.…_\s]*[.…_]{3,}[.…_,\s]*(?:C\.?J\.?I\.?|J)\.?\s*$|^[^A-Z]{4,}(?:C\.?J\.?I|J)\.?$|^(?:C\.?J\.?I|J)\.$")
_SIGN_NAME = re.compile(r"^[\[(]\s*((?:Dr\.?\s+|Justice\s+)?[A-Z][A-Za-z.' -]{2,45}?)\s*[\])]\s*,?\s*(?:C\.?J\.?I\.?|J\.?)?$")
_RANK = re.compile(r",?\s*\b(?:C\.?\s?J\.?\s?I?\.?|JJ?\.?)\s*$")
_TITLES = re.compile(r"^(?:hon'?ble\s+)?(?:the\s+)?(?:(?:mr|mrs|ms|dr|shri|smt)\.?\s+)*(?:chief\s+)?(?:justice\s+)?(?:(?:mr|mrs|ms|dr)\.?\s+)?", re.IGNORECASE)
_NEUTRAL = re.compile(r"\b((?:19|20)\d{2})\s+INSC\s+(\d{1,4})\b")
_OPINION_KIND = [
    ("partly_dissenting", re.compile(r"partly\s+dissent|partial(?:ly)?\s+dissent|dissenting\s+in\s+part", re.IGNORECASE)),
    ("dissenting", re.compile(r"dissent|minority", re.IGNORECASE)),
    ("concurring", re.compile(r"concurr", re.IGNORECASE)),
    ("majority", re.compile(r"majority", re.IGNORECASE)),
]


def _caps_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


def title_case(text: str) -> str:
    """'JARNAIL SINGH' -> 'Jarnail Singh'; initials and mixed-case text are left alone."""
    text = re.sub(r"\s+", " ", text).strip()
    if _caps_share(text) < 0.8:
        # already mixed case: only settle 'State Of Punjab' into 'State of Punjab'
        return re.sub(r"(?<=\s)(Of|And|The|In|For|On|By|To)(?=\s)", lambda m: m.group().lower(), text)
    words = []
    for n, word in enumerate(text.split(" ")):
        low = word.lower()
        if n and low in _SMALL_WORDS:
            words.append(low)
        elif re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]?\.?", word):         # K.S.  M/s.
            words.append(word.upper())
        else:
            words.append(re.sub(r"[A-Za-z]+", lambda m: m.group().capitalize(), word))
    return " ".join(words)


def _clean_party(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .,;:-–—…")
    text = _ROLE.sub("", text)
    text = _AND_OTHERS.sub("", text)
    text = _THROUGH.sub("", text)
    text = text.strip(" ,;:-–—…")
    text = text.rstrip(". ")
    if re.search(r"\b(?:[A-Za-z]\.)+[A-Za-z]$", text):       # 'State of U.P' -> 'State of U.P.'
        text += "."
    return title_case(text)


def _parse_date(text: str) -> dt.date | None:
    for pattern, order in ((_DATE_MDY, "mdy"), (_DATE_DMY, "dmy"), (_DATE_NUM, "num")):
        m = pattern.search(text)
        if not m:
            continue
        try:
            if order == "mdy":
                return dt.date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
            if order == "dmy":
                return dt.date(int(m.group(3)), _MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))        # Indian order: day first
        except ValueError:
            continue
    return None


def _is_date_line(text: str) -> dt.date | None:
    """A line that is a date and nothing else: 'New Delhi; September 26, 2018.'"""
    rest = _DATE_PREFIX.sub("", text.strip())
    for pattern in (_DATE_MDY, _DATE_DMY, _DATE_NUM):
        m = pattern.search(rest)
        if m and len(rest) - (m.end() - m.start()) <= 4:
            return _parse_date(rest)
    return None


def clean_judge(name: str) -> str:
    """'HON'BLE MR. JUSTICE A.K. VERMA' / 'Dr Dhananjaya Y Chandrachud, J' -> the bare name."""
    name = re.sub(r"\s+", " ", name).strip(" .,;:()[]")
    name = re.sub(r"\(.*$", "", name).strip()
    name = _RANK.sub("", name).strip(" ,")
    name = re.sub(r"^CJI\s+", "", name)
    name = _TITLES.sub("", name).strip()
    name = re.sub(r"(?<=[A-Za-z]\.)(?=[A-Za-z]{2,})", " ", name)           # 'P.K.BALASUBRAMANYAN'
    return title_case(name)


def _surname(name: str) -> str:
    parts = re.sub(r"[^A-Za-z ]", " ", name).split()
    return parts[-1].lower() if parts else ""


def same_judge(a: str, b: str) -> bool:
    """'Palekar' and 'D. G. PALEK.AR' (as OCR left it) are the same judge."""
    sa, sb = _surname(a), _surname(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    la, lb = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
    if len(sa) >= 5 and lb.endswith(sa) or len(sb) >= 5 and la.endswith(sb):
        return True
    return min(len(sa), len(sb)) >= 5 and SequenceMatcher(None, sa, sb).ratio() >= 0.8


def _add_judge(bench: list[str], name: str) -> None:
    name = clean_judge(name)
    if len(re.sub(r"[^A-Za-z]", "", name)) < 3 or len(name) > 45:
        return
    if not any(same_judge(name, b) for b in bench):
        bench.append(name)


def _split_bench(text: str) -> list[str]:
    """'CJI R.C. LAHOTI,G.P. MATHUR & P.K.BALASUBRAMANYAN' or '(S. M. SIKRI, C. J., J. M. SHELAT AND ... JJ.)'."""
    text = text.strip().strip("()[]")
    text = re.sub(r"\(\s*CJ\s*\)", "", text)
    text = re.sub(r"\s+(?:AND|and|&)\s+", ", ", text)
    names: list[str] = []
    for piece in re.split(r"\s*[,;]\s*", text):
        piece = piece.strip()
        if not piece or re.fullmatch(r"(?:C\.?\s?J\.?\s?I?\.?|JJ?\.?)", piece):
            continue
        _add_judge(names, piece)
    return names


def _judis_names(lines: list[str]) -> list[str]:
    """JUDIS lists the bench surname first: 'KRISHNAIYER, V.R.' / 'BHAGWATI, P.N.'"""
    names: list[str] = []
    for line in lines:
        m = re.fullmatch(r"([A-Z][A-Z .'-]+),\s*((?:[A-Z]\.?\s?){1,4})(?:\s*\(.*\))?", line.strip())
        if m:
            _add_judge(names, f"{m.group(2).strip()} {m.group(1).strip()}")
        else:
            for name in _split_bench(line):
                _add_judge(names, name)
    return names


def _labelled(lines: list[str], label: str, stop: re.Pattern[str]) -> list[str]:
    """The text under a JUDIS label such as 'PETITIONER:', up to the next label."""
    for i, line in enumerate(lines):
        m = re.match(rf"^\s*{label}\s*:?\s*(.*)$", line, re.IGNORECASE)
        if not m:
            continue
        out = [m.group(1).strip()] if m.group(1).strip() else []
        for nxt in lines[i + 1:i + 12]:
            if stop.match(nxt.strip()):
                break
            if nxt.strip():
                out.append(nxt.strip())
        return out
    return []


_JUDIS_LABEL = re.compile(
    r"^(?:CASE\s+NO|PETITIONER|RESPONDENT|DATE\s+OF\s+JUDGE?MENT|BENCH|JUDGE?MENT|ACT|HEADNOTE|CITATION|"
    r"CITATOR\s+INFO|APPELLANT|VS?\.?|Vs\.)\s*:?\s*.*$", re.IGNORECASE)


def _judis_citations(lines: list[str]) -> list[str]:
    """'1978 AIR 597', '1978 SCR (2) 621' -> 'AIR 1978 SC 597', '(1978) 2 SCR 621'."""
    out = []
    for line in _labelled(lines, "CITATION", _JUDIS_LABEL):
        for year, number in re.findall(r"\b((?:19|20)\d{2})\s+AIR\s+(\d{1,5})\b", line):
            out.append(f"AIR {year} SC {number}")
        for year, reporter, volume, page in re.findall(r"\b((?:19|20)\d{2})\s+(SCR|SCC)\s+\((\d{1,2})\)\s*(\d{1,4})\b", line):
            out.append(f"({year}) {volume} {reporter} {page}")
    return list(dict.fromkeys(out))


def opinion_from(author_line: str | None, bench: list[str]) -> tuple[str | None, list[str], str]:
    """Author, judges joining, and the kind of opinion, from the line that heads it."""
    if not author_line:
        return None, [], "unknown"
    note = re.search(r"\((.*)\)", author_line)
    kind = "unknown"
    joined: list[str] = []
    if note:
        for name, pattern in _OPINION_KIND:
            if pattern.search(note.group(1)):
                kind = name
                break
        with_ = re.search(r"(?:himself|herself|myself)\s*(?:,|and)\s*(.*)", note.group(1), re.IGNORECASE)
        if with_:
            joined = _split_bench(with_.group(1))
    author = clean_judge(author_line)
    names = [author] if "&" not in author and " and " not in author.lower() else _split_bench(author)
    resolved = []
    for name in names + joined:
        match = next((b for b in bench if same_judge(b, name)), None)
        resolved.append(match or name)
    return resolved[0], resolved[1:], kind


def read_meta(pdf_path: str | Path, lines: list[Line], split: SplitResult) -> CaseMeta:
    """Work out the case record from the judgment text. Never raises: what is
    not found is left empty and named in `missing`."""
    pdf_path = Path(pdf_path)
    texts = [l.text.strip() for l in lines]
    head = [t for t in texts[:140] if t]
    missing: list[str] = []
    notes: list[str] = []

    petitioner = respondent = ""
    date: dt.date | None = None
    bench: list[str] = []
    citations: list[str] = []
    layout = "court"

    judis = any(re.match(r"^PETITIONER\s*:", t) for t in head[:12])
    kanoon = re.match(r"^(.{3,150}?)\s+vs?\.?\s+(.{3,150}?)\s+on\s+(\d{1,2}\s+[A-Za-z]+,?\s+(?:19|20)\d{2})$", head[0]) if head else None

    if judis:
        layout = "judis"
        petitioner = " ".join(_labelled(head, "PETITIONER", _JUDIS_LABEL)[:3])
        respondent = " ".join(_labelled(head, "RESPONDENT", _JUDIS_LABEL)[:3])
        for t in head[:30]:
            m = re.match(r"^DATE\s+OF\s+JUDGE?MENT\s*:?\s*(.*)$", t, re.IGNORECASE)
            if m:
                date = _parse_date(m.group(1))
                break
        bench = _judis_names(_labelled(head, "BENCH", _JUDIS_LABEL)[:8])
        citations = _judis_citations(head)
    elif kanoon:
        layout = "indian_kanoon"
        petitioner, respondent = kanoon.group(1), kanoon.group(2)
        date = _parse_date(kanoon.group(3))
        for t in head[:12]:
            if t.lower().startswith("bench:"):
                bench = _split_bench(t.split(":", 1)[1])
            elif t.lower().startswith("equivalent citations:"):
                citations = [c.strip() for c in t.split(":", 1)[1].split(",") if c.strip()]
    else:
        at = next((i for i, t in enumerate(head[:80]) if _VERSUS.match(t)), None)
        if at is not None:
            above: list[str] = []
            for t in reversed(head[max(0, at - 5):at]):
                if _NOT_A_PARTY.match(t) or _CASE_NUMBER.search(t) or _parse_date(t):
                    break
                above.insert(0, t)
                if _ROLE.search(t) and len(above) > 1:
                    above = above[1:] if _ROLE.search(above[0]) and len(above) > 2 else above
            below: list[str] = []
            for t in head[at + 1:at + 6]:
                if _NOT_A_PARTY.match(t) or _CASE_NUMBER.search(t) or _is_date_line(t):
                    break
                below.append(t)
                if _ROLE.search(t):
                    break
            petitioner, respondent = " ".join(above), " ".join(below)

    if split.layout == "law_report":
        layout = "law_report"
        for t in head[:14]:
            found = _is_date_line(t)
            if found and not date:
                date = found
        opened = next((i for i, t in enumerate(head[:16]) if t.startswith("(") and re.search(r"\bC\.?\s?J|\bJJ?\b", t)), None)
        if opened is not None and not bench:
            block = ""
            for t in head[opened:opened + 6]:
                block += " " + t
                if ")" in t:
                    break
            bench = _split_bench(block)

    # ---- bench: the signatures at the end of each opinion (the court's own PDFs)
    if not bench:
        content = [t for t in texts if t]
        for prev, cur in zip(content, content[1:]):
            name = _SIGN_NAME.match(cur)
            if name and _SIGN_LINE.match(prev):
                _add_judge(bench, name.group(1))
        coram = next((i for i, t in enumerate(head[:60]) if re.match(r"^(?:coram|present|before)\b", t, re.IGNORECASE)), None)
        if not bench and coram is not None:
            for t in head[coram:coram + 6]:
                t = re.sub(r"^(?:coram|present|before)\s*:?\s*", "", t, flags=re.IGNORECASE)
                if re.search(r"justice", t, re.IGNORECASE):
                    _add_judge(bench, t)

    # ---- opinions, and any author the bench list missed
    opinions: list[Opinion] = []
    for found in split.opinions:
        author, joined, kind = opinion_from(found.author_line, bench)
        if author:
            for name in [author, *joined]:
                _add_judge(bench, name)
            author = next((b for b in bench if same_judge(b, author)), author)
            joined = [next((b for b in bench if same_judge(b, j)), j) for j in joined]
        opinions.append(Opinion(author or "", kind, joined))
    if len(opinions) == 1:
        # one opinion is the judgment of the court, whoever signed it
        opinions[0].type = "majority"
        if not opinions[0].author and len(bench) == 1:
            opinions[0].author = bench[0]
    else:
        for o in opinions:
            if o.type == "unknown" and o.author and bench and 2 * (1 + len(o.joined_by)) > len(bench):
                o.type = "majority"                       # the author and those joining outnumber the rest
    named_opinions = [o for o in opinions if o.author]
    if len(opinions) > 1 and not named_opinions:
        # Several parts and no judge named on any of them: these are orders of the
        # whole bench (a separate opinion always carries its author's name).
        for o in opinions:
            o.type = "majority"
        opinions_out: list[Opinion] = opinions
        notes.append(f"The file holds {len(opinions)} orders of the court, none attributed to one judge.")
    elif len(named_opinions) != len(opinions):
        notes.append("Not every opinion names its author; those are labelled 'unknown'.")
        opinions_out = opinions                          # positions must still line up with the PDF
    else:
        opinions_out = opinions
    if len(opinions) > 1 and any(o.type == "unknown" for o in opinions):
        notes.append(
            "The judgment has several opinions and the PDF does not say which is the majority. "
            "They are labelled 'unknown' until `opinions` is filled in for this case in data/cases.yaml."
        )

    # ---- date: the dated line that closes each opinion, under the signatures.
    # A date standing alone elsewhere is usually a date in the case's own story
    # ('20.12.2004', the cut-off in Vineeta Sharma), so position decides, not frequency.
    if not date:
        content = [t for t in texts if t]
        closing: list[dt.date] = []
        for i, t in enumerate(content):
            if len(t) > 60:
                continue
            found = _is_date_line(t)
            if not found:
                continue
            before = content[max(0, i - 2):i]
            placed = bool(_DATE_PREFIX.match(t)) or any(
                re.match(r"^new\s+delhi\b", b, re.IGNORECASE) or _SIGN_NAME.match(b) or _SIGN_LINE.match(b) for b in before)
            if placed:
                closing.append(found)
        if closing:
            date = Counter(closing).most_common(1)[0][0]
        else:
            for t in head[:40]:
                if re.search(r"\b(?:decided\s+on|date\s+of\s+(?:decision|judge?ment|order)|pronounced\s+on|dated)\b", t, re.IGNORECASE):
                    date = _parse_date(t)
                    if date:
                        break

    # ---- citation and case number
    for t in head[:40]:
        m = _NEUTRAL.search(t)
        if m and not citations:
            citations.append(f"{m.group(1)} INSC {m.group(2)}")
    if judis:
        case_number = " ".join(_labelled(head, r"CASE\s+NO\.?", _JUDIS_LABEL)[:1]) or None
    else:
        case_number = next((re.sub(r"\s+", " ", t) for t in head[:40]
                            if _CASE_NUMBER.search(t) and len(t) <= 110 and not t.endswith(":")), None)

    # ---- court
    joined_head = " ".join(head[:60]).upper()
    high = re.search(r"HIGH\s+COURT\s+OF\s+(?:JUDICATURE\s+(?:AT|OF)\s+)?([A-Z &]+?)(?:\s+AT\b|\s+BENCH\b|\s{2}|$|\s+[A-Z]\.)", joined_head)
    if "SUPREME COURT OF INDIA" in joined_head or judis or layout == "law_report":
        court = "Supreme Court"
    elif high:
        court = f"{high.group(1).strip().title()} High Court"
    else:
        court = "Supreme Court"
        notes.append("The court is not named in the file; Supreme Court was assumed.")

    petitioner, respondent = _clean_party(petitioner), _clean_party(respondent)
    if petitioner and respondent:
        name = f"{petitioner} v. {respondent}"
    else:
        name = title_case(re.sub(r"[_-]+", " ", pdf_path.stem))
        missing.append("parties")
    if not date:
        missing.append("date")
    if not bench:
        missing.append("bench")
    if not citations:
        missing.append("citation")

    lead = petitioner if petitioner and not re.match(r"^(?:state|union|commissioner|director|collector)\b", petitioner, re.IGNORECASE) \
        else f"{petitioner} v {respondent}" if petitioner else pdf_path.stem
    lead = re.sub(r"^(?:justice|dr|shri|smt|mr|mrs|ms|m/s|his holiness|sri)\.?\s+", "", lead, flags=re.IGNORECASE)
    lead = re.sub(r"\(.*?\)", "", lead)
    case_id = "_".join(slugify(lead).split("_")[:6]) or slugify(pdf_path.stem)
    if date:
        case_id += f"_{date.year}"

    return CaseMeta(
        case_id=case_id, name=name, date=date, court=court, citations=citations, bench=bench,
        file=str(pdf_path), opinions=opinions_out, notes=" ".join(notes) or None,
        source="auto", missing=missing, case_number=case_number, layout=layout,
    )
