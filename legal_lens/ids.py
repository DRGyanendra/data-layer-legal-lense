"""Identifiers shared by Qdrant, Neo4j and the backend.

Every ID is derived from content, so loading the same judgment twice
overwrites the earlier load instead of duplicating it.
"""

from __future__ import annotations

import re
import uuid

# Fixed namespace: changing it changes every point ID, so leave it alone.
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "chunks.legal-lens")

_JUDGE_NOISE = re.compile(
    r"\b(hon'?ble|justice|mr|mrs|ms|dr|cji|c\.j\.i|chief justice of india|chief justice|jj?)\b\.?",
    re.IGNORECASE,
)


def slugify(text: str) -> str:
    """Lowercase, ASCII letters and digits, single underscores."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def point_id(chunk_id: str) -> str:
    """Qdrant accepts only unsigned integers or UUIDs as point IDs.

    The readable chunk_id stays in the payload; this is its stable UUID.
    """
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


def normalize_section(section: str) -> str:
    """'304-A' -> '304A', '156 (3)' -> '156(3)', '120b' -> '120B'."""
    s = re.sub(r"\s+", "", section)
    m = re.match(r"^(\d+)-?([A-Za-z]{0,2})((?:\([0-9A-Za-z]+\))*)$", s)
    if not m:
        return s
    number, suffix, subs = m.groups()
    return f"{number}{suffix.upper()}{subs}"


def base_section(section: str) -> str:
    """Section without its sub-sections: '106(1)' -> '106'."""
    return re.sub(r"\(.*$", "", normalize_section(section))


def statute_id(code: str, section: str) -> str:
    """One canonical ID per provision, e.g. 'BNS:106(1)' or 'IPC:304A'."""
    return f"{code}:{normalize_section(section)}"


def citation_key(citation: str) -> str:
    """'(2005) 6 SCC 1' -> '2005-6-scc-1'; 'AIR 2005 SC 3180' -> 'air-2005-sc-3180'."""
    return re.sub(r"[^a-z0-9]+", "-", citation.lower()).strip("-")


def judge_id(name: str) -> str:
    """'Justice R.C. Lahoti' and 'R. C. LAHOTI, CJI' both give 'r_c_lahoti'."""
    cleaned = _JUDGE_NOISE.sub(" ", name)
    return slugify(cleaned)
