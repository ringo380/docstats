"""Platform-default seed data for specialty + payer rules.

The 12 specialties and 8 payers below are a thoughtful starting set for
Phase 3's rules engine. They're explicitly NOT medical advice — every
field is a coordinator-workflow heuristic: what a referral packet should
*probably* contain to minimize rejections and phone follow-ups, based on
common referral-desk practice. Org admins override via
``admin_override`` rows (Phase 6 admin UI).

``seed_platform_defaults(storage)`` is idempotent by default — it
pre-fetches the set of global rows (``organization_id IS NULL``) and
skips any ``specialty_code`` / ``payer_key`` already present. Safe to
re-run at deploy time. Partial unique indices on
``(specialty_code) WHERE organization_id IS NULL`` and
``(payer_key) WHERE organization_id IS NULL`` backstop the Python
uniqueness check at the DB layer.

When invoked with ``overwrite=True``, existing global rows are updated
in place with seed values (with ``bump_version=False`` so the
rule-engine cache isn't invalidated by a canonical restore, and with
``overwrite=True`` on the storage update so seed fields can reset a
column to ``None`` that an admin had previously filled in).

Callers from the rules engine read these rows via
``storage.list_specialty_rules(organization_id=X)`` /
``storage.list_payer_rules(organization_id=X)`` — the merge-with-
override logic lives there, not here.
"""

from __future__ import annotations

import logging
from typing import Any

from docstats.storage_base import StorageBase

logger = logging.getLogger(__name__)


# --- Specialty defaults ---
#
# NUCC taxonomy codes sourced from:
# https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40
#
# Each entry describes the fields a coordinator should gather for a referral
# *into* this specialty, not the fields a provider of this specialty would
# collect in their own practice.

SPECIALTY_DEFAULTS: list[dict[str, Any]] = [
    {
        "specialty_code": "207R00000X",
        "display_name": "Internal Medicine",
        "required_fields": {
            "fields": ["reason", "clinical_question"],
        },
        "recommended_attachments": {
            "kinds": ["lab", "note"],
            "labels": ["Recent labs (CBC, CMP)", "Recent visit notes"],
        },
        "intake_questions": {
            "prompts": [
                "Chief complaint and duration?",
                "Relevant chronic conditions?",
                "Current medications?",
            ],
        },
        "urgency_red_flags": {
            "keywords": ["sepsis", "DKA", "acute shortness of breath"],
        },
        "common_rejection_reasons": {
            "reasons": ["Missing reason for consult", "Incomplete medication list"],
        },
    },
    {
        "specialty_code": "207RC0000X",
        "display_name": "Cardiology",
        "required_fields": {
            "fields": ["reason", "clinical_question", "diagnosis_primary_icd"],
        },
        "recommended_attachments": {
            "kinds": ["lab", "imaging", "note"],
            "labels": [
                "Recent EKG",
                "Echo / cardiac imaging (if any)",
                "Lipid panel",
                "Troponin (if suspected ACS)",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Chief cardiac complaint and duration?",
                "Exertional or rest symptoms?",
                "Family history of early CAD or sudden cardiac death?",
                "Prior cardiac imaging or procedures?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "chest pain",
                "dyspnea at rest",
                "syncope",
                "new murmur",
                "hemodynamic instability",
                "ventricular arrhythmia",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing recent EKG",
                "No documented attempt at guideline-directed medical therapy",
                "Referral sent to wrong cardiology subspecialty (EP vs interventional vs general)",
            ],
        },
    },
    {
        "specialty_code": "207RE0101X",
        "display_name": "Endocrinology, Diabetes & Metabolism",
        "required_fields": {
            "fields": ["reason", "clinical_question"],
        },
        "recommended_attachments": {
            "kinds": ["lab", "note"],
            "labels": [
                "Recent A1C (past 3 months)",
                "TSH / thyroid panel",
                "Relevant labs (e.g. Vitamin D, PTH, cortisol)",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Glycemic control trend?",
                "Prior endocrinology workup?",
                "Current diabetes / thyroid medications with doses?",
            ],
        },
        "urgency_red_flags": {
            "keywords": ["DKA", "HHS", "thyroid storm", "adrenal crisis"],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing recent A1C or TSH",
                "No prior primary-care diabetes management documented",
            ],
        },
    },
    {
        "specialty_code": "207RG0100X",
        "display_name": "Gastroenterology",
        "required_fields": {
            "fields": ["reason", "clinical_question"],
        },
        "recommended_attachments": {
            "kinds": ["lab", "imaging", "note", "procedure"],
            "labels": [
                "CBC, LFTs, lipase",
                "Abdominal imaging (if any)",
                "Prior endoscopy reports",
            ],
        },
        "intake_questions": {
            "prompts": [
                "GI symptom characterization and duration?",
                "Weight loss, bleeding, or anemia?",
                "Prior endoscopy / colonoscopy results?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "GI bleed",
                "hematemesis",
                "melena",
                "biliary obstruction",
                "pancreatitis",
            ],
        },
        "common_rejection_reasons": {
            "reasons": ["Missing recent CBC/LFTs", "Unclear indication for endoscopy"],
        },
    },
    {
        "specialty_code": "2084N0400X",
        "display_name": "Neurology",
        "required_fields": {
            "fields": ["reason", "clinical_question"],
        },
        "recommended_attachments": {
            "kinds": ["imaging", "note"],
            "labels": [
                "Head / brain imaging (CT, MRI)",
                "Prior EEG / EMG (if any)",
                "Neuro exam notes",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Neurological symptom onset and progression?",
                "Prior imaging and results?",
                "Seizure / stroke history?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "acute stroke",
                "status epilepticus",
                "sudden severe headache",
                "new focal deficit",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing recent brain imaging",
                "No documented neurological exam",
            ],
        },
    },
    {
        "specialty_code": "207X00000X",
        "display_name": "Orthopedic Surgery",
        "required_fields": {
            "fields": ["reason", "clinical_question"],
        },
        "recommended_attachments": {
            "kinds": ["imaging", "note"],
            "labels": [
                "X-ray of affected area",
                "MRI (if obtained)",
                "PT notes (if attempted)",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Affected joint / anatomical area?",
                "Injury mechanism and date?",
                "Conservative measures tried (PT, NSAIDs, injections)?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "open fracture",
                "cauda equina",
                "compartment syndrome",
                "septic joint",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing X-ray",
                "No documented conservative management trial",
                "Wrong orthopedic subspecialty (spine vs sports vs joint-replacement)",
            ],
        },
    },
    {
        "specialty_code": "207N00000X",
        "display_name": "Dermatology",
        "required_fields": {"fields": ["reason"]},
        "recommended_attachments": {
            "kinds": ["imaging", "note"],
            "labels": ["Photographs of lesion(s)", "Biopsy report (if any)"],
        },
        "intake_questions": {
            "prompts": [
                "Lesion location, size, duration?",
                "Change in size, color, or symptoms?",
                "Prior biopsies or treatments?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "rapidly growing lesion",
                "suspected melanoma",
                "Stevens-Johnson",
                "DRESS syndrome",
            ],
        },
        "common_rejection_reasons": {
            "reasons": ["No photos attached", "Vague lesion description"],
        },
    },
    {
        "specialty_code": "207Y00000X",
        "display_name": "Otolaryngology (ENT)",
        "required_fields": {"fields": ["reason", "clinical_question"]},
        "recommended_attachments": {
            "kinds": ["imaging", "note"],
            "labels": ["Sinus / neck imaging (if any)", "Audiology report (if hearing-related)"],
        },
        "intake_questions": {
            "prompts": [
                "ENT complaint and duration?",
                "Hearing changes, vertigo, or tinnitus?",
                "Prior ENT procedures?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "airway compromise",
                "sudden sensorineural hearing loss",
                "epistaxis not controlled",
                "neck mass suspicious for malignancy",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing audiology for hearing complaints",
                "No imaging for suspected sinus disease",
            ],
        },
    },
    {
        "specialty_code": "2084P0800X",
        "display_name": "Psychiatry",
        "required_fields": {"fields": ["reason", "clinical_question"]},
        "recommended_attachments": {
            "kinds": ["note", "medication_list"],
            "labels": ["Recent psych notes", "Current psychiatric medication list"],
        },
        "intake_questions": {
            "prompts": [
                "Primary psychiatric concern?",
                "Safety concerns (SI / HI)?",
                "Prior hospitalizations and meds tried?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "suicidal ideation with plan",
                "homicidal ideation",
                "acute psychosis",
                "catatonia",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "No documented safety assessment",
                "Missing PCP / therapy coordination",
            ],
        },
    },
    {
        "specialty_code": "207RR0500X",
        "display_name": "Rheumatology",
        "required_fields": {"fields": ["reason", "clinical_question"]},
        "recommended_attachments": {
            "kinds": ["lab", "imaging", "note"],
            "labels": [
                "ANA, RF, anti-CCP (as indicated)",
                "ESR, CRP",
                "Joint imaging (if any)",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Joint involvement pattern (symmetric, small vs large)?",
                "Morning stiffness duration?",
                "Extra-articular symptoms (rash, eye, renal)?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "giant cell arteritis",
                "acute vasculitis",
                "pulmonary-renal syndrome",
                "scleroderma crisis",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing basic inflammatory labs (ESR / CRP)",
                "No documented symptom pattern",
            ],
        },
    },
    {
        "specialty_code": "207RH0003X",
        "display_name": "Hematology / Oncology",
        "required_fields": {
            "fields": ["reason", "clinical_question", "diagnosis_primary_icd"],
        },
        "recommended_attachments": {
            "kinds": ["lab", "imaging", "note", "procedure"],
            "labels": [
                "CBC with diff",
                "Recent imaging (CT / PET)",
                "Pathology / biopsy reports",
                "Staging workup",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Suspected or confirmed malignancy?",
                "Staging workup completed?",
                "Cytopenias or bleeding history?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "tumor lysis",
                "spinal cord compression",
                "febrile neutropenia",
                "new leukocytosis",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "Missing pathology report for suspected cancer",
                "Incomplete staging workup",
            ],
        },
    },
    {
        "specialty_code": "208VP0014X",
        "display_name": "Pain Management",
        "required_fields": {"fields": ["reason", "clinical_question"]},
        "recommended_attachments": {
            "kinds": ["imaging", "note", "medication_list"],
            "labels": [
                "Imaging of painful area",
                "Prior PT / interventional procedure notes",
                "Current pain medication list (incl. opioids)",
            ],
        },
        "intake_questions": {
            "prompts": [
                "Pain location, quality, and duration?",
                "Conservative measures tried?",
                "Current opioid dose (MME / day) if any?",
            ],
        },
        "urgency_red_flags": {
            "keywords": [
                "cauda equina",
                "red flags for malignancy",
                "rapidly progressive neurologic deficit",
            ],
        },
        "common_rejection_reasons": {
            "reasons": [
                "No imaging of painful area",
                "No documented conservative management trial",
                "Missing current medication list",
            ],
        },
    },
]


# --- Payer defaults ---
#
# Coordinator-friendly heuristics. Real-time eligibility + auth rules land
# in Phase 11 via clearinghouse APIs (Availity etc.). Until then these
# defaults drive wizard behavior.

PAYER_DEFAULTS: list[dict[str, Any]] = [
    {
        "payer_key": "Kaiser Permanente|hmo",
        "display_name": "Kaiser Permanente HMO",
        "referral_required": True,
        "auth_required_services": {
            "services": ["MRI", "CT", "PET", "out-of-network specialist"],
        },
        "auth_typical_turnaround_days": 3,
        "records_required": {
            "kinds": ["recent visit notes", "relevant imaging / labs"],
        },
        "notes": "HMO model; most specialist referrals need PCP referral + auth for imaging.",
    },
    {
        "payer_key": "Blue Cross Blue Shield|ppo",
        "display_name": "Blue Cross Blue Shield PPO",
        "referral_required": False,
        "auth_required_services": {
            "services": ["high-cost imaging (MRI / PET)", "elective surgery"],
        },
        "auth_typical_turnaround_days": 5,
        "records_required": {
            "kinds": ["recent notes", "imaging history for repeat imaging"],
        },
        "notes": "PPO model; no referral needed but auth on high-cost procedures.",
    },
    {
        "payer_key": "Aetna|hmo",
        "display_name": "Aetna HMO",
        "referral_required": True,
        "auth_required_services": {
            "services": ["specialist visits", "MRI", "CT"],
        },
        "auth_typical_turnaround_days": 5,
        "records_required": {"kinds": ["PCP referral form", "recent clinical notes"]},
        "notes": "HMO gatekeeper model; verify specialist is in-network before referral.",
    },
    {
        "payer_key": "UnitedHealthcare|ppo",
        "display_name": "UnitedHealthcare PPO",
        "referral_required": False,
        "auth_required_services": {
            "services": ["MRI", "PET", "inpatient admission (scheduled)"],
        },
        "auth_typical_turnaround_days": 5,
        "records_required": {"kinds": ["notes documenting medical necessity"]},
        "notes": "PPO; auth via UHC provider portal.",
    },
    {
        "payer_key": "Medicare|medicare",
        "display_name": "Medicare (Original)",
        "referral_required": False,
        "auth_required_services": {
            "services": ["home health", "DME over threshold"],
        },
        "auth_typical_turnaround_days": None,
        "records_required": {"kinds": ["medical necessity documentation"]},
        "notes": "Original Medicare; most specialist visits do not require prior auth.",
    },
    {
        "payer_key": "Medicaid|medicaid",
        "display_name": "Medicaid (state-managed)",
        "referral_required": True,
        "auth_required_services": {
            "services": ["specialty care", "high-cost imaging", "inpatient"],
        },
        "auth_typical_turnaround_days": 7,
        "records_required": {
            "kinds": ["PCP referral", "recent notes", "imaging"],
        },
        "notes": "Varies by state and managed-care plan; org admins should override per state rules.",
    },
    {
        "payer_key": "Cigna|hmo",
        "display_name": "Cigna HMO",
        "referral_required": True,
        "auth_required_services": {
            "services": ["specialist visits", "MRI", "CT"],
        },
        "auth_typical_turnaround_days": 5,
        "records_required": {"kinds": ["referral form", "notes"]},
        "notes": "HMO gatekeeper model.",
    },
    {
        "payer_key": "Humana|ppo",
        "display_name": "Humana PPO",
        "referral_required": False,
        "auth_required_services": {
            "services": ["MRI", "PET", "elective surgery"],
        },
        "auth_typical_turnaround_days": 5,
        "records_required": {"kinds": ["medical-necessity notes"]},
        "notes": "PPO; auth via Humana provider portal.",
    },
]


def seed_platform_defaults(
    storage: StorageBase,
    *,
    overwrite: bool = False,
) -> dict[str, int]:
    """Insert the 12 specialty rules and 8 payer rules as platform defaults.

    Safe to re-run: existing rows (matched by specialty_code / payer_key
    with ``organization_id IS NULL``) are skipped unless ``overwrite=True``.

    Returns a counts dict for the caller to log, e.g.::

        {
            "specialty_rules_created": 12,
            "specialty_rules_skipped": 0,
            "specialty_rules_overwritten": 0,
            "payer_rules_created": 8,
            "payer_rules_skipped": 0,
            "payer_rules_overwritten": 0,
        }
    """
    counts = {
        "specialty_rules_created": 0,
        "specialty_rules_skipped": 0,
        "specialty_rules_overwritten": 0,
        "payer_rules_created": 0,
        "payer_rules_skipped": 0,
        "payer_rules_overwritten": 0,
    }

    # Build a code → row index once so the overwrite path doesn't re-fetch
    # the list for every seeded entry (that was O(N²)).
    existing_specialty_by_code = {
        r.specialty_code: r for r in storage.list_specialty_rules(organization_id=None)
    }
    for row in SPECIALTY_DEFAULTS:
        code = row["specialty_code"]
        live_specialty = existing_specialty_by_code.get(code)
        if live_specialty is not None:
            if overwrite:
                # ``bump_version=False``: a canonical seed restore should NOT
                # invalidate every rule-engine cache that holds this version.
                # ``overwrite=True``: write every seed field literally so a
                # column set to None in SPECIALTY_DEFAULTS (none today, but
                # future-proof) can reset back to None.
                storage.update_specialty_rule(
                    live_specialty.id,
                    display_name=row["display_name"],
                    required_fields=row["required_fields"],
                    recommended_attachments=row["recommended_attachments"],
                    intake_questions=row["intake_questions"],
                    urgency_red_flags=row["urgency_red_flags"],
                    common_rejection_reasons=row["common_rejection_reasons"],
                    source="seed",
                    bump_version=False,
                    overwrite=True,
                )
                counts["specialty_rules_overwritten"] += 1
            else:
                counts["specialty_rules_skipped"] += 1
            continue
        storage.create_specialty_rule(
            specialty_code=code,
            display_name=row["display_name"],
            required_fields=row["required_fields"],
            recommended_attachments=row["recommended_attachments"],
            intake_questions=row["intake_questions"],
            urgency_red_flags=row["urgency_red_flags"],
            common_rejection_reasons=row["common_rejection_reasons"],
            source="seed",
        )
        counts["specialty_rules_created"] += 1

    existing_payer_by_key = {r.payer_key: r for r in storage.list_payer_rules(organization_id=None)}
    for row in PAYER_DEFAULTS:
        key = row["payer_key"]
        live_payer = existing_payer_by_key.get(key)
        if live_payer is not None:
            if overwrite:
                storage.update_payer_rule(
                    live_payer.id,
                    display_name=row["display_name"],
                    referral_required=row["referral_required"],
                    auth_required_services=row["auth_required_services"],
                    auth_typical_turnaround_days=row["auth_typical_turnaround_days"],
                    records_required=row["records_required"],
                    notes=row["notes"],
                    source="seed",
                    bump_version=False,
                    overwrite=True,
                )
                counts["payer_rules_overwritten"] += 1
            else:
                counts["payer_rules_skipped"] += 1
            continue
        storage.create_payer_rule(
            payer_key=key,
            display_name=row["display_name"],
            referral_required=row["referral_required"],
            auth_required_services=row["auth_required_services"],
            auth_typical_turnaround_days=row["auth_typical_turnaround_days"],
            records_required=row["records_required"],
            notes=row["notes"],
            source="seed",
        )
        counts["payer_rules_created"] += 1

    logger.info("seed_platform_defaults result: %s", counts)
    return counts
