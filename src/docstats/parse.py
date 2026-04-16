"""Smart query parser for the NPI search bar.

Parses free-text input like "dr. kim do orthopedics" into structured
fields (first_name, last_name, specialty, org) that map to NPPES API params.
Generates a ranked list of API call interpretations to try in sequence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Honorifics stripped from the start ───────────────────────────────────────
_HONORIFICS = {"dr", "doctor", "doc", "prof", "professor"}

# ── Credentials stripped from standalone tokens ──────────────────────────────
# Tokens that are UNAMBIGUOUSLY credentials (not common surnames)
_UNAMBIGUOUS_CREDENTIALS = {
    "md",
    "phd",
    "dds",
    "dvm",
    "aprn",
    "dnp",
    "facp",
    "facs",
    "facog",
    "faad",
    "mph",
    "ms",
    "rn",
    "np",
    "pa",
}
# Tokens that are credentials but also common names — keep as name by default,
# only treat as credential if the name interpretation returns no results.
_AMBIGUOUS_CREDENTIALS = {"do", "pa-c", "cnp"}

# ── Org signals: any of these words → treat whole input as organization ───────
_ORG_SIGNALS = {
    "hospital",
    "clinic",
    "medical center",
    "health system",
    "urgent care",
    "institute",
    "associates",
    "group",
    "llc",
    "inc",
    "corporation",
    "foundation",
    "center",
    "services",
    "kaiser",
    "sutter",
    "ucsf",
    "stanford",
    "cedars",
    "kaiser permanente",
}

# ── Specialty keyword → NUCC taxonomy display name ───────────────────────────
# Maps common terms (and plurals/adjective forms) to the string used in
# NPPES taxonomy_description queries.
_SPECIALTY_MAP: list[tuple[str, str]] = [
    # Multi-word first (checked before single-word)
    ("internal medicine", "Internal Medicine"),
    ("family medicine", "Family Medicine"),
    ("family practice", "Family Medicine"),
    ("infectious disease", "Infectious Disease"),
    ("physical therapy", "Physical Therapy"),
    ("occupational therapy", "Occupational Therapy"),
    ("speech language", "Speech-Language Pathology"),
    ("orthopedic surgery", "Orthopedic Surgery"),
    ("general surgery", "General Surgery"),
    ("obstetrics gynecology", "Obstetrics & Gynecology"),
    ("ob gyn", "Obstetrics & Gynecology"),
    ("obgyn", "Obstetrics & Gynecology"),
    ("hematology oncology", "Hematology & Oncology"),
    ("allergy immunology", "Allergy & Immunology"),
    ("pulmonary disease", "Pulmonary Disease"),
    ("sleep medicine", "Sleep Medicine"),
    ("sports medicine", "Sports Medicine"),
    ("pain management", "Pain Management"),
    ("palliative care", "Palliative Care"),
    ("urgent care", "Emergency Medicine"),
    # Single-word
    ("cardiology", "Cardiology"),
    ("cardiologist", "Cardiology"),
    ("cardiac", "Cardiology"),
    ("neurology", "Neurology"),
    ("neurologist", "Neurology"),
    ("orthopedics", "Orthopedic Surgery"),
    ("orthopedic", "Orthopedic Surgery"),
    ("ortho", "Orthopedic Surgery"),
    ("internist", "Internal Medicine"),
    ("pediatrics", "Pediatrics"),
    ("pediatrician", "Pediatrics"),
    ("dermatology", "Dermatology"),
    ("dermatologist", "Dermatology"),
    ("psychiatry", "Psychiatry"),
    ("psychiatrist", "Psychiatry"),
    ("psych", "Psychiatry"),
    ("oncology", "Hematology & Oncology"),
    ("oncologist", "Hematology & Oncology"),
    ("endocrinology", "Endocrinology"),
    ("endocrinologist", "Endocrinology"),
    ("gastroenterology", "Gastroenterology"),
    ("gastroenterologist", "Gastroenterology"),
    ("ophthalmology", "Ophthalmology"),
    ("ophthalmologist", "Ophthalmology"),
    ("urology", "Urology"),
    ("urologist", "Urology"),
    ("pulmonology", "Pulmonology"),
    ("pulmonologist", "Pulmonology"),
    ("rheumatology", "Rheumatology"),
    ("rheumatologist", "Rheumatology"),
    ("allergist", "Allergy & Immunology"),
    ("radiology", "Radiology"),
    ("radiologist", "Radiology"),
    ("anesthesiology", "Anesthesiology"),
    ("anesthesiologist", "Anesthesiology"),
    ("surgeon", "General Surgery"),
    ("surgery", "General Surgery"),
    ("obstetrics", "Obstetrics & Gynecology"),
    ("gynecology", "Obstetrics & Gynecology"),
    ("podiatry", "Podiatry"),
    ("podiatrist", "Podiatry"),
    ("optometry", "Optometry"),
    ("optometrist", "Optometry"),
    ("chiropractic", "Chiropractic"),
    ("chiropractor", "Chiropractic"),
    ("nephrology", "Nephrology"),
    ("nephrologist", "Nephrology"),
    ("neurosurgery", "Neurological Surgery"),
    ("neurosurgeon", "Neurological Surgery"),
    ("pathology", "Pathology"),
    ("pathologist", "Pathology"),
    ("immunology", "Allergy & Immunology"),
    ("hematology", "Hematology & Oncology"),
    ("hepatology", "Gastroenterology"),
]


@dataclass
class ParseResult:
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""  # not sent to API; used for post-retrieval ranking only
    specialty: str = ""
    honorific: str = ""
    credentials: list[str] = field(default_factory=list)
    is_org: bool = False
    org_name: str = ""


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace. Keep hyphens (pa-c)."""
    return re.sub(r"[.,/#!$%^&*;:{}=_`~()]", " ", text).lower()


def _strip_honorific(tokens: list[str]) -> tuple[str, list[str]]:
    """Remove a leading honorific token and return (honorific, remaining)."""
    if not tokens:
        return "", tokens
    candidate = tokens[0]
    if candidate in _HONORIFICS:
        return candidate, tokens[1:]
    return "", tokens


def parse_query(text: str) -> ParseResult:
    """Parse a free-text provider search query into structured fields."""
    if not text.strip():
        return ParseResult()

    raw = text.strip()
    normalized = _normalize(raw)

    # ── Org detection (multi-word signals first) ─────────────────────────────
    for signal in sorted(_ORG_SIGNALS, key=len, reverse=True):
        if signal in normalized:
            return ParseResult(is_org=True, org_name=raw)

    tokens = normalized.split()

    # ── Honorific ─────────────────────────────────────────────────────────────
    honorific, tokens = _strip_honorific(tokens)

    # ── Unambiguous credentials (strip from anywhere) ─────────────────────────
    credentials: list[str] = []
    remaining: list[str] = []
    for tok in tokens:
        if tok in _UNAMBIGUOUS_CREDENTIALS:
            credentials.append(tok)
        else:
            remaining.append(tok)
    tokens = remaining

    # ── Specialty (multi-word first, then single-word) ────────────────────────
    specialty = ""
    text_joined = " ".join(tokens)
    for key, label in _SPECIALTY_MAP:
        if " " in key and key in text_joined:
            specialty = label
            text_joined = text_joined.replace(key, "").strip()
            tokens = text_joined.split()
            break
    if not specialty:
        remaining = []
        for tok in tokens:
            matched = False
            for key, label in _SPECIALTY_MAP:
                if " " not in key and tok == key:
                    specialty = label
                    matched = True
                    break
            if not matched:
                remaining.append(tok)
        tokens = remaining

    # ── Name tokens (ambiguous credentials stay as-is) ───────────────────────
    tokens = [t for t in tokens if t]  # remove empties
    first_name = ""
    last_name = ""
    middle_name = ""
    if len(tokens) == 1:
        last_name = tokens[0]
    elif len(tokens) == 2:
        first_name = tokens[0]
        last_name = tokens[1]
    elif len(tokens) >= 3:
        first_name = tokens[0]
        last_name = tokens[-1]
        middle_name = " ".join(tokens[1:-1])  # middle token(s); not sent to API

    return ParseResult(
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        specialty=specialty,
        honorific=honorific,
        credentials=credentials,
        is_org=False,
        org_name="",
    )


def _cap(s: str) -> str:
    return s.capitalize() if s else ""


def build_interpretations(result: ParseResult) -> list[dict[str, str]]:
    """Return ordered list of NPPES API kwargs dicts to try until non-empty results.

    Each dict can be unpacked directly into NPPESClient.search(**kwargs).
    """
    if result.is_org:
        return [{"organization_name": result.org_name, "enumeration_type": "NPI-2"}]

    first = _cap(result.first_name)
    last = _cap(result.last_name)
    spec = result.specialty

    interps: list[dict[str, str]] = []

    if first and last and spec:
        interps.append({"first_name": first, "last_name": last, "taxonomy_description": spec})
        interps.append(
            {
                "last_name": first,  # treat first as last (ambiguous names)
                "taxonomy_description": spec,
            }
        )
        interps.append({"first_name": first, "last_name": last})
        interps.append({"last_name": first})
    elif first and last:
        interps.append({"first_name": first, "last_name": last})
        interps.append({"last_name": last})
        interps.append({"last_name": first})
    elif last and spec:
        interps.append({"last_name": last, "taxonomy_description": spec})
        interps.append({"last_name": last})
    elif last:
        interps.append({"last_name": last})
    elif first:
        interps.append({"last_name": first})

    return interps
