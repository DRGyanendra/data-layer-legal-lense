"""Label each paragraph with its rhetorical role: who is speaking, and why.

Research on Indian judgments (the OpenNyAI BUILD corpus, LegalSeg) divides
a judgment into roles such as facts, issues, arguments of each side,
analysis and ruling. For this product the distinction that matters most is
between what a party argued and what the court held: a retrieved passage
that says "the negligence need not be gross" may be counsel's submission,
which the court then rejected.

This is a rule-based labeller built on the stock phrases Indian judgments
use ('learned counsel for the appellant submitted', 'per contra', 'the
appeal is allowed'). It is deliberately cautious: a paragraph with no
clear cue is 'unlabelled', never guessed. Trained sentence classifiers
reach about 0.77 to 0.79 F1 on this task, and mix up the two sides'
arguments most often, so treat these labels as hints for display and
ranking. Do not hard-filter on them until they have been measured.

Three rules came from running it on real judgments:
- Cues are looked for outside quotation marks only. A judgment quotes other
  judgments at length, and 'the appeal is dismissed' inside a quotation is
  not this court's order.
- A ruling is only recognised near the end of an opinion.
- The State is the appellant in some cases and the respondent in others, so
  'learned counsel for the State' is assigned a side only when the case name
  shows which side the State is on.
"""

from __future__ import annotations

import re

from .citations import find_case_refs, find_statute_refs
from .paragraphs import Paragraph

ROLES = [
    "facts", "issue", "argument_petitioner", "argument_respondent", "argument",
    "lower_court", "statute", "precedent", "analysis", "ruling", "unlabelled",
]
COURT_VOICE = {"analysis", "ruling"}          # the court speaking for itself
PARTY_VOICE = {"argument_petitioner", "argument_respondent", "argument"}

_I = re.IGNORECASE
_OPENING = 420       # arguments announce the speaker at the start of the paragraph

_SUBMIT = (r"(?:submit(?:s|ted)?|contend(?:s|ed)?|argu(?:es|ed)|urg(?:es|ed)|canvass(?:es|ed)|point(?:s|ed) out"
           r"|rel(?:ies|ied)|plead(?:s|ed)|assail(?:s|ed)|defend(?:s|ed)|request(?:s|ed)|took us through)")
_PETITIONER = r"(?:appellants?|petitioners?|applicants?|plaintiffs?|revisionists?)\b"
_RESPONDENT = r"(?:respondents?|defendants?|caveators?)\b"
_STATE = r"(?:State|Union(?: of India)?|Revenue|prosecution|Government|C\.?B\.?I\.?)(?![a-z])"
_COUNSEL = r"(?:learned )?(?:senior )?(?:counsel|advocate|amicus(?: curiae)?)"
_LAW_OFFICER = (r"learned (?:additional )?(?:solicitor|advocate|attorney) general|learned (?:additional |special )?public prosecutor"
                r"|learned (?:a\.?s\.?g\.?|a\.?a\.?g\.?|a\.?g\.?)\b")


def _side(party: str) -> re.Pattern[str]:
    return re.compile(
        rf"{_COUNSEL}[^.]{{0,90}}?(?:for|on behalf of|representing) (?:the )?{party}"
        rf"|(?:on behalf of|for) (?:the )?{party}[^.]{{0,160}}?{_SUBMIT}"
        rf"|\bthe {party} (?:{_SUBMIT}|would (?:submit|contend|argue)|ha(?:s|ve) (?:{_SUBMIT}))", _I)


_ARG_PETITIONER = _side(_PETITIONER)
_ARG_RESPONDENT = _side(_RESPONDENT)
_ARG_STATE = re.compile(_side(_STATE).pattern + "|" + _LAW_OFFICER, _I)
_ARG_NAMED = re.compile(      # 'Shri Rakesh Dwivedi, learned senior advocate, appearing in ...'
    r"(?:Shri|Sri|Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Smt\.?)\s+[A-Z][\w.' ]{2,40},?\s+learned (?:senior )?(?:counsel|advocate|amicus)", _I)
_SUBMISSION_VERB = re.compile(rf"\b{_SUBMIT}\b|\bsubmissions?\b|\bcontentions?\b|\bvehemently\b", _I)
_HEARD_BOTH = re.compile(r"\b(?:we|i) have heard\b|\bheard (?:the )?learned counsel\b", _I)
_PER_CONTRA = re.compile(r"^\W*[\d.]*\W*(?:per contra|on the other hand|in reply|in response|in rebuttal)\b", _I)
_CONTINUES = re.compile(
    rf"^\W*[\d.]*\W*(?:it (?:was|is|has been) (?:further |also |next |lastly |then |finally )?{_SUBMIT}"
    rf"|(?:he|she|they|learned (?:senior )?counsel) (?:has |have )?(?:further |also |next |lastly |then |finally )?{_SUBMIT}"
    r"|according to (?:him|her|them|learned counsel)|(?:his|her|their) (?:next|further|last) (?:submission|contention))", _I)

_ISSUE = re.compile(
    r"(?:questions?|issues?|points?) (?:that |which )?(?:arises?|falls?|fall) for (?:our )?(?:consideration|determination|decision)"
    r"|(?:questions?|issues?|points?) for (?:our )?(?:consideration|determination|decision)"
    r"|the (?:short|only|sole|core|principal|moot|substantial) (?:question|issue|point)"
    r"|following (?:questions?|issues?|points?) (?:arise|arises|were framed|was framed|were referred|was referred)", _I)
_RULING = re.compile(
    r"(?:appeals?|petitions?|writ petitions?|review petitions?|applications?|references?|suit) "
    r"(?:is|are|stands?|shall stand) (?:accordingly |hereby |therefore |thus )?(?:partly )?(?:allowed|dismissed|disposed of|answered|decreed|rejected)"
    r"|\b(?:we|i) (?:accordingly |therefore |hereby |thus )?(?:allow|dismiss) (?:the |this |these |both )?(?:appeals?|petitions?|applications?|writ|review|suit|revision)"
    r"|\b(?:we|i) (?:accordingly |therefore |hereby |thus )?(?:set aside|quash|dispose of|remand|remit|direct that|answer the reference|hold and declare|declare that)"
    r"|(?:judgment|order|conviction|sentence|proceedings?|f\.?i\.?r\.?)[^.]{0,80}?(?:is|are|stands?) (?:hereby |accordingly )?(?:set aside|quashed|affirmed|upheld|confirmed|restored)"
    r"|(?:is|are|stands?) (?:hereby )?(?:struck down|declared (?:to be )?unconstitutional|overruled)"
    r"|the reference is answered|no order as to costs|ordered accordingly|pending applications?", _I)
_LOWER_COURT = re.compile(
    r"the (?:learned )?(?:trial court|trial judge|sessions (?:judge|court)|high court|single judge|division bench"
    r"|tribunal|magistrate|first appellate court|appellate (?:court|authority)|commission|court below|courts below)"
    r"[^.]{0,140}?(?:held|convicted|acquitted|dismissed|allowed|decreed|rejected|found|observed|quashed|sentenced"
    r"|took the view|declined|refused|upheld|affirmed|confirmed|concluded|opined|granted)", _I)
_STATUTE_QUOTE = re.compile(
    r"(?:reads?|provides?|runs?|stipulates?|lays? down) (?:as (?:under|follows)|thus)"
    r"|(?:is|are) (?:extracted|reproduced|quoted|set out) (?:below|hereunder|herein ?below|as under)", _I)
_PRECEDENT = re.compile(
    r"this court (?:in|has held|had held|held|observed|laid down|ruled)|it was (?:held|observed|laid down)"
    r"|(?:constitution|larger|three-judge|two-judge|coordinate|full|nine-judge|seven-judge|five-judge) bench"
    r"|(?:was|were) (?:followed|approved|affirmed|overruled|distinguished)|\(supra\)", _I)
_FACTS = re.compile(
    r"the (?:brief |relevant |essential |material |undisputed )?facts|facts (?:of the case|in brief|leading to)"
    r"|the prosecution (?:case|story|version)|factual (?:matrix|background|backdrop)|briefly stated"
    r"|f\.?i\.?r\.? (?:was|came to be) (?:registered|lodged)|first information report (?:was|with) "
    r"|charge-?sheet (?:was|came to be) filed|(?:was|were) (?:charged|arrested|tried|convicted) ", _I)
_ANALYSIS = re.compile(
    r"\b(?:we|i) (?:are|am) of the (?:considered |firm |clear )?(?:view|opinion)"
    r"|\b(?:we|i) (?:find|hold|think|see no|do not (?:find|agree|think)|have (?:considered|given|carefully)|are unable|am unable|agree|note|conclude)\b"
    r"|in (?:our|my) (?:view|opinion|considered|judgment)|having (?:heard|considered|regard)"
    r"|it is (?:well |now |trite |no longer )?(?:settled|res integra)|the law is (?:well )?settled|we now (?:turn|proceed)", _I)

_HEADING_ROLES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"SUBMISSION|CONTENTION|ARGUMENT", _I), "argument"),
    (re.compile(r"\bISSUES?\b|QUESTION|POINTS? FOR|THE REFERENCE", _I), "issue"),
    (re.compile(r"\bFACTS?\b|BACKGROUND|PROSECUTION CASE|CHRONOLOGY", _I), "facts"),
    (re.compile(r"ANALYSIS|DISCUSSION|REASON|CONSIDERATION|FINDING|CONCLUSION|SUMMATION", _I), "analysis"),
    (re.compile(r"\bORDER\b|DIRECTION|RESULT|RELIEF|DISPOSITION|OPERATIVE", _I), "ruling"),
]
_QUOTATION = re.compile(r"“[^”]*(?:”|$)|\"[^\"]*(?:\"|$)")
_PARTY_SPLIT = re.compile(r"\s+(?:v\.?|vs\.?|versus)\s+", _I)


def quoted_share(text: str) -> float:
    """Fraction of the text that sits inside double quotation marks.

    A paragraph that is mostly quotation is someone else's words: an
    extract from a statute or an earlier judgment.
    """
    if not text:
        return 0.0
    inside = sum(len(m.group()) for m in _QUOTATION.finditer(text))
    return round(min(1.0, inside / len(text)), 2)


def state_side(case_name: str) -> str | None:
    """'respondent' for 'Jacob Mathew v. State of Punjab', 'petitioner' for
    'State of Haryana v. Bhajan Lal', None when neither or both are the State."""
    parts = _PARTY_SPLIT.split(case_name, maxsplit=1)
    if len(parts) != 2:
        return None
    first, second = (bool(re.search(_STATE, p)) for p in parts)
    if first == second:
        return None
    return "petitioner" if first else "respondent"


def _role_from_heading(heading: str | None, state: str | None) -> str | None:
    if not heading:
        return None
    for pattern, role in _HEADING_ROLES:
        if pattern.search(heading):
            if role == "argument":
                if re.search(_PETITIONER, heading, _I):
                    return "argument_petitioner"
                if re.search(_RESPONDENT, heading, _I):
                    return "argument_respondent"
                if re.search(_STATE, heading) and state:
                    return f"argument_{state}"
            return role
    return None


def _label(para: Paragraph, previous: str, near_end: bool, state: str | None) -> str:
    own = _QUOTATION.sub(" ", para.text)          # the court's own words in this paragraph
    opening = own[:_OPENING]
    heading_role = _role_from_heading(para.heading, state)

    # 1. Explicit, high-precision cues: who is speaking.
    speakers: list[tuple[int, str]] = []
    for pattern, role in ((_ARG_PETITIONER, "argument_petitioner"), (_ARG_RESPONDENT, "argument_respondent"),
                          (_ARG_STATE, f"argument_{state}" if state else "argument"), (_ARG_NAMED, "argument")):
        m = pattern.search(opening)
        if m:
            speakers.append((m.start(), role))
    sides = {role for _, role in speakers if role != "argument"}
    if speakers and _SUBMISSION_VERB.search(opening) and not (_HEARD_BOTH.search(opening) and len(sides) > 1):
        specific = [s for s in speakers if s[1] != "argument"]
        return min(specific or speakers)[1]
    if _PER_CONTRA.match(own):
        if previous == "argument_petitioner":
            return "argument_respondent"
        if previous == "argument_respondent":
            return "argument_petitioner"
        return "argument"
    if _CONTINUES.match(own):
        return previous if previous in PARTY_VOICE else (heading_role if heading_role in PARTY_VOICE else "argument")
    if heading_role in PARTY_VOICE:
        return heading_role

    # 2. Explicit cues for the court's own acts.
    if _ISSUE.search(own):
        return "issue"
    if near_end and _RULING.search(own):
        return "ruling"
    if _LOWER_COURT.search(opening):
        return "lower_court"

    # 3. A section heading, where the judgment has one.
    if heading_role:
        return heading_role

    # 4. Softer cues.
    quoted = quoted_share(para.text)
    if _STATUTE_QUOTE.search(own) and find_statute_refs(para.text) and quoted >= 0.3:
        return "statute"
    if _ANALYSIS.search(own):
        return "analysis"
    if find_case_refs(para.text) and (quoted >= 0.4 or _PRECEDENT.search(own)):
        return "precedent"
    if _FACTS.search(own):
        return "facts"
    return "unlabelled"


def label_paragraphs(paragraphs: list[Paragraph], state: str | None = None) -> list[str]:
    """One role per paragraph, in order. `state` is which side the State is on,
    from `state_side(case_name)`, or None when unknown."""
    sizes: dict[int, int] = {}
    for p in paragraphs:
        sizes[p.segment] = sizes.get(p.segment, 0) + 1
    seen: dict[int, int] = {}
    roles: list[str] = []
    previous = "unlabelled"
    for index, para in enumerate(paragraphs):
        if index and paragraphs[index - 1].segment != para.segment:
            previous = "unlabelled"
        seen[para.segment] = seen.get(para.segment, 0) + 1
        remaining = sizes[para.segment] - seen[para.segment]
        near_end = remaining < max(6, round(0.12 * sizes[para.segment]))
        role = _label(para, previous, near_end, state)
        roles.append(role)
        previous = role
    return roles
