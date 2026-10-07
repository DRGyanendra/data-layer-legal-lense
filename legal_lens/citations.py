"""Find statute references and case citations in judgment text.

Statute references are normalised to one form, so 'IPC 304A',
'Section 304-A of the Indian Penal Code' and 'S. 304A, I.P.C.' all become
code='IPC', section='304A'.

Limits, stated plainly:
- A bare 'Section 304A' with no Act named nearby is skipped. Guessing the
  Act would put wrong edges in the graph.
- Ranges ('Sections 299 to 304') are skipped.
- Case names are a best-effort hint taken from the words before a citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ids import base_section, citation_key, normalize_section

# --------------------------------------------------------------------------
# OCR repair, applied only to the copy of the text used for extraction
# --------------------------------------------------------------------------

_OCR_ZERO = re.compile(r"(?<=\d)[Oo](?=\d)|(?<=\d)[Oo](?=[A-Z]\b)")
_OCR_ONE = re.compile(r"(?<=\d)[lI](?=\d)")


def repair_ocr_digits(text: str) -> tuple[str, int]:
    """'Section 3O4A' -> 'Section 304A'. Returns the text and the fix count.

    Replacements keep the string length, so match offsets still line up
    with the original text.
    """
    text, zeros = _OCR_ZERO.subn("0", text)
    text, ones = _OCR_ONE.subn("1", text)
    return text, zeros + ones


# --------------------------------------------------------------------------
# Statute references
# --------------------------------------------------------------------------

_SEC = (
    r"(?<!\d)\d{1,3}(?!\d)"
    r"(?:\s?-\s?[A-Z]{1,2}|[A-Z]{1,2})?(?![A-Za-z])"
    r"(?:\s?\(\s?[0-9A-Za-z]{1,3}\s?\))*"
)
_SEP = (
    r"(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or|&)\s+|\s*/\s*|\s+read\s+with\s+|\s+r/w\s+)"
    r"(?:[Ss]ections?\s+|[Ss]ec\.\s*)?"
)
_LIST = rf"{_SEC}(?:{_SEP}{_SEC})*"

# Order matters: BNSS must be tried before BNS.
_KNOWN_ACTS: list[tuple[str, str]] = [
    ("BNSS", r"B\.?N\.?S\.?S\.?(?![A-Za-z])|Bharatiya\s+Nagarik\s+Suraksha\s+Sanhita"),
    ("BNS", r"B\.?N\.?S\.?(?![A-Za-z])|Bharatiya\s+Nyaya\s+Sanhita"),
    ("IPC", r"I\.?P\.?C\.?(?![A-Za-z])|Indian\s+Penal\s+Code|Penal\s+Code"),
    (
        "CrPC",
        r"Cr\.?\s?P\.?\s?C\.?(?![A-Za-z])|Code\s+of\s+Criminal\s+Procedure"
        r"|Criminal\s+Procedure\s+Code",
    ),
]
_KNOWN_ALT = "|".join(f"(?:{p})" for _, p in _KNOWN_ACTS)
_GENERIC_ACT = (
    r"(?:[A-Z][A-Za-z'&.-]*\s+(?:(?:of|and|for|the|to|in)\s+)*){1,8}Act(?:,?\s*\d{4})?"
)
_ACT = rf"(?:{_KNOWN_ALT}|{_GENERIC_ACT})"

# 'Section 304A of the Indian Penal Code', 'Sections 302 and 307 IPC', 'u/s 154 CrPC'
_SECTION_THEN_ACT = re.compile(
    rf"(?:\b[Ss]ections?|\b[Ss]ecs?\.|\bS\.|\b[Uu]/[Ss]\.?|§)\s*(?P<list>{_LIST})"
    rf"\s*,?\s*(?:of\s+(?:the\s+)?)?(?P<act>{_ACT})"
)
# 'IPC 304A', 'IPC Sec. 304A', 'BNS Section 106(1)'
_ACT_THEN_SECTION = re.compile(
    r"(?<![A-Za-z.])(?P<act>I\.?P\.?C\.?|Cr\.?\s?P\.?\s?C\.?|B\.?N\.?S\.?S\.?|B\.?N\.?S\.?)"
    rf"\s+(?P<kw>(?:[Ss]ections?|[Ss]ec\.?|§)\s*)?(?P<list>{_LIST})"
)
_ART = (
    r"(?<!\d)\d{1,3}(?!\d)(?:-?[A-Z]{1,2})?(?![A-Za-z])"
    r"(?:\s?\(\s?[0-9A-Za-z]{1,3}\s?\))*"
)
_ARTICLES = re.compile(
    rf"\bArticles?\s+(?P<list>{_ART}(?:(?:\s*,\s*(?:and\s+)?|\s+(?:and|or|&)\s+|\s+read\s+with\s+)"
    rf"(?:Articles?\s+)?{_ART})*)"
)
_NOT_CONSTITUTION = re.compile(r"^\s*of\s+(?!(?:the\s+)?Constitution)", re.IGNORECASE)

# 'Section 154 of the Penal Code' in a passage on Ugandan law is not the IPC.
# A bare 'Penal Code' is read as the IPC unless a foreign jurisdiction is
# named close by. 'Indian Penal Code' and 'IPC' are never affected.
_BARE_PENAL_CODE = re.compile(r"Penal\s+Code")
_FOREIGN = re.compile(
    r"\b(?:Ugand|Kenya|Nigeria|Tanzania|Zambia|Malawi|Botswana|Zimbabwe|South\s+Africa"
    r"|Singapore|Malaysia|Straits|Pakistan|Bangladesh|Sri\s+Lanka|Ceylon|Nepal|Burm|Myanmar"
    r"|Canad|Australia|Queensland|New\s+Zealand|Fiji|Hong\s+Kong|Belize|Trinidad|Jamaica"
    r"|German|French|France|Ital|Spanish|Spain|Turk|Japan|Korea|Philippin|Chin(?:a|ese)"
    r"|California|Texas|Model\s+Penal\s+Code)"
)


def _foreign_code(text: str, start: int, end: int) -> bool:
    return bool(_FOREIGN.search(text[max(0, start - 200):end + 120]))


@dataclass(frozen=True)
class StatuteRef:
    code: str          # 'IPC', 'CrPC', 'BNS', 'BNSS', 'Constitution', or an Act's name
    section: str       # normalised: '304A', '156(3)', '21'
    raw: str           # the text as it appeared
    start: int         # offset in the text

    @property
    def statute_id(self) -> str:
        return f"{self.code}:{self.section}"

    @property
    def base_section(self) -> str:
        return base_section(self.section)


def _classify_act(act_text: str) -> str:
    for code, pattern in _KNOWN_ACTS:
        if re.fullmatch(pattern, act_text.strip()):
            return code
    # Generic Act: tidy the name, keep the year.
    name = re.sub(r"\s+", " ", act_text).strip()
    return re.sub(r"^(?:the)\s+", "", name, flags=re.IGNORECASE)


def find_statute_refs(text: str) -> list[StatuteRef]:
    """All statute references in the text, in order of appearance."""
    fixed, _ = repair_ocr_digits(text)
    refs: list[StatuteRef] = []
    consumed_acts: set[int] = set()

    section_first = list(_SECTION_THEN_ACT.finditer(fixed))
    section_starts = {m.start() for m in section_first}

    for m in section_first:
        code = _classify_act(m.group("act"))
        consumed_acts.add(m.start("act"))
        if _BARE_PENAL_CODE.fullmatch(m.group("act").strip()) and _foreign_code(fixed, m.start(), m.end()):
            continue
        for item in re.finditer(_SEC, m.group("list")):
            refs.append(
                StatuteRef(code, normalize_section(item.group()), m.group(), m.start())
            )

    for m in _ACT_THEN_SECTION.finditer(fixed):
        # 'Section 304A IPC 2 years', 'Section 497 IPC. Section 198 treats ...':
        # the IPC token belongs to the match before it, so what follows it is
        # not an IPC section.
        if m.start("act") in consumed_acts:
            continue
        # '... under Section 497 IPC. Section 198 CrPC deals with ...': the
        # number after the full stop belongs to the Act named after it.
        if m.group("kw") and m.start("kw") in section_starts:
            continue
        code = _classify_act(m.group("act"))
        for item in re.finditer(_SEC, m.group("list")):
            refs.append(
                StatuteRef(code, normalize_section(item.group()), m.group(), m.start())
            )

    for m in _ARTICLES.finditer(fixed):
        if _NOT_CONSTITUTION.match(fixed[m.end():m.end() + 40]):
            continue  # 'Article 17 of the ICCPR'
        for item in re.finditer(_ART, m.group("list")):
            refs.append(
                StatuteRef("Constitution", normalize_section(item.group()), m.group(), m.start())
            )

    refs.sort(key=lambda r: r.start)
    seen: set[tuple[str, str, int]] = set()
    unique = []
    for r in refs:
        key = (r.code, r.section, r.start)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# --------------------------------------------------------------------------
# Case citations
# --------------------------------------------------------------------------

_REPORTERS: list[tuple[str, re.Pattern[str], str]] = [
    ("SCC", re.compile(r"\((\d{4})\)\s*(\d{1,2})\s*SCC\s*(?!\()(\d{1,4})(?!\d)"), "({0}) {1} SCC {2}"),
    ("SCC", re.compile(r"(?<![\d(])(\d{4})\s*\((\d{1,2})\)\s*SCC\s*(?!\()(\d{1,4})(?!\d)"), "({0}) {1} SCC {2}"),
    ("SCC-Cri", re.compile(r"(?<!\d)(\d{4})\s*SCC\s*\(Cri\)\s*(\d{1,5})(?!\d)"), "{0} SCC (Cri) {1}"),
    ("AIR", re.compile(r"\bAIR\s*(\d{4})\s*(SC|[A-Z][a-z]{1,4}|[A-Z]{2,4})\s*(\d{1,5})(?!\d)"), "AIR {0} {1} {2}"),
    ("SCR", re.compile(r"[\[(](\d{4})[\])]\s*(\d{1,2})\s*S\.?C\.?R\.?\s*(\d{1,4})(?!\d)"), "({0}) {1} SCR {2}"),
    ("SCR", re.compile(r"(?<![\d(\[])(\d{4})\s+S\.?C\.?R\.?\s+(\d{1,4})(?!\d)"), "{0} SCR {1}"),
    ("SCC-Supp", re.compile(r"\(?(\d{4})\)?\s*Supp\.?\s*(?:\((\d)\)\s*)?SCC\s*(\d{1,4})(?!\d)"), "{0} Supp ({1}) SCC {2}"),
    ("INSC", re.compile(r"(?<!\d)(\d{4})\s*INSC\s*(\d{1,4})(?!\d)"), "{0} INSC {1}"),
    ("WLR", re.compile(r"[\[(](\d{4})[\])]\s*(\d)\s*W\.?L\.?R\.?\s*(\d{1,4})(?!\d)"), "[{0}] {1} WLR {2}"),
    ("AllER", re.compile(r"[\[(](\d{4})[\])]\s*(\d)\s*All\s*E\.?R\.?\s*(\d{1,4})(?!\d)"), "[{0}] {1} All ER {2}"),
]

_VERSUS = re.compile(r"\s(v\.|vs\.?|versus|V\.|Vs\.?)\s")
_NAME_JOINERS = {"of", "and", "the", "&"}
_LEAD_INS = {"see", "also", "in", "vide", "cf", "per", "and", "but", "thus", "as", "held", "following", "refer"}
_NAME_ABBREVIATIONS = {"ltd", "co", "pvt", "corpn", "corp", "inc", "dr", "mr", "mrs", "smt", "sri", "shri",
                       "m/s", "govt", "anr", "ors", "st", "bros", "etc", "retd", "col", "maj", "capt", "lt"}
_PARALLEL_GAP = re.compile(r"^\s*[:=,]\s*$")


@dataclass
class CaseRef:
    citations: list[str]                 # canonical forms, parallel citations together
    name_hint: str | None                # 'Jacob Mathew v. State of Punjab', if found
    start: int
    keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.keys:
            self.keys = [citation_key(c) for c in self.citations]


def _name_before(text: str, start: int) -> str | None:
    """Look back from a citation for 'A v. B'."""
    window = text[max(0, start - 180):start]
    hits = list(_VERSUS.finditer(window))
    if not hits:
        return None
    last = hits[-1]
    left_words = window[:last.start()].split()
    right = window[last.end():]

    picked: list[str] = []
    for word in reversed(left_words):
        bare = word.strip(",;:()[]\"'“”‘’")
        if not bare:
            break
        if bare[0].isupper() or bare.lower() in _NAME_JOINERS:
            picked.append(bare)
        else:
            break
        is_initials = bool(re.fullmatch(r"(?:[A-Z]\.){1,4}[A-Z]?\.?", word.strip(",;:()[]\"'“”‘’")))
        is_abbrev = bare.lower().rstrip(".") in _NAME_ABBREVIATIONS
        if word[-1:] in ".;:" and len(bare) > 2 and picked[1:] and not is_initials and not is_abbrev:
            # a full stop on a real word means the previous sentence ended here
            picked.pop()
            break
    while picked and picked[-1].lower().rstrip(".") in _NAME_JOINERS | _LEAD_INS:
        picked.pop()
    left = " ".join(reversed(picked))

    right = re.split(r"\s*(?:[\[(]|,?\s*reported\b|,?\s*\d{4}\b|:)", right, maxsplit=1)[0]
    right = right.strip(" ,;.-")
    if not left or not right or len(right) > 90:
        return None
    return f"{left} v. {right}"


def find_case_refs(text: str) -> list[CaseRef]:
    """Reporter citations in the text. Parallel citations of one case are grouped."""
    hits: list[tuple[int, int, str, str]] = []
    for reporter, pattern, template in _REPORTERS:
        for m in pattern.finditer(text):
            rendered = template.format(*[g or "" for g in m.groups()]).replace(" () ", " ")
            hits.append((m.start(), m.end(), reporter, rendered))
    hits.sort()

    # Drop a hit that sits inside an earlier one (the two SCC patterns can overlap).
    cleaned: list[tuple[int, int, str, str]] = []
    for hit in hits:
        if cleaned and hit[0] < cleaned[-1][1]:
            continue
        cleaned.append(hit)

    groups: list[list[tuple[int, int, str, str]]] = []
    for hit in cleaned:
        if groups:
            prev = groups[-1][-1]
            gap = text[prev[1]:hit[0]]
            if _PARALLEL_GAP.match(gap) and hit[2] not in {h[2] for h in groups[-1]}:
                groups[-1].append(hit)
                continue
        groups.append([hit])

    refs = []
    for group in groups:
        citations = list(dict.fromkeys(h[3] for h in group))
        refs.append(CaseRef(citations, _name_before(text, group[0][0]), group[0][0]))
    return refs
