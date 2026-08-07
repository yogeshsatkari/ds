import json
from pathlib import Path

import gemini_service

from data_to_docx.validate import SAMPLE_PATH, SCHEMA_PATH, load_sample, load_schema, validate_context

PROMPT_HEADER = """You are a clinical documentation assistant.

Extract discharge summary data from the clinical context below.

Use the SAMPLE JSON as the target shape, tone, and field usage guide.
Use the JSON SCHEMA for required fields, types, and boolean section flags.

Rules:
- Do not invent facts. Use only what is in the clinical context.
- Match SAMPLE style: UPPERCASE clinical text, array item format, discharge_note length.
- Never use "NOT MENTIONED" or "N/A" — use "" when a field is unknown.

DOCTORS:
- treating_doctor = primary treating/consulting specialist managing the case (from care plan, "c/s/b", "consultation with", specialist review). Include specialty in parentheses if given, e.g. DR. SORABH GUPTA.
- discharge_signatory = senior doctor on the right column; usually the SAME person as treating_doctor unless another signatory is explicitly named.
- signing_doctors = left-column doctors only (residents, ward doctors, shifting doctors). Do NOT include treating_doctor or discharge_signatory here.
- unit_incharge = unit in-charge if documented; otherwise "".

INVESTIGATION TABLE (investigation_dates + investigation_rows):
- Set include_investigation_table=true whenever the context has recorded vitals or lab values tied to dates.
- Row types include BOTH lab parameters (Albumin, A/G Ratio, etc.) AND nursing vitals (GCS, BP, HR, RR, Temp, SPO2, Pulse Rate, RBS, VAS).
- Each row: name = parameter label; values = one string per date column (use "" if missing on that date).
- Each investigation_rows[].values array MUST have exactly the same length as investigation_dates.
- Extract from assessment blocks, examination sections, and lab reports with dates.
- Do NOT leave dated vitals/lab numbers in clinical_course — they belong only in the table.
- Do NOT put "investigations advised/ordered" lists (CBO, LFT, KFT, etc.) in the table unless actual results with dates exist.
- If no dated results exist at all, set include_investigation_table=false and use [].

clinical_course:
- Admission story, treatment, consultations, medications, care plan, examination summary.
- Do NOT include: GCS/HR/BP/RR/Temp/SPO2/RBS values, lab numeric results, or "investigations advised" lists.

investigation_reports:
- Set include_investigation_reports=true for narrative test write-ups (ultrasound, nerve conduction, MRI).
- If none, set false and use [].

discharge_note:
- One short discharge status sentence only (see SAMPLE).

VITALS TABLE EXAMPLE (when context has assessment vitals, not lab rows):
{
  "include_investigation_table": true,
  "investigation_dates": ["28/07/26", "02/07/26"],
  "investigation_rows": [
    {"name": "GCS", "values": ["E4V5M6", "E4V5M6"]},
    {"name": "HR", "values": ["82", ""]},
    {"name": "RR", "values": ["22", "22"]},
    {"name": "BP", "values": ["156/100", "120/80"]},
    {"name": "RBS", "values": ["109 mg/dl", ""]},
    {"name": "SPO2", "values": ["97%", "97"]},
    {"name": "Temp", "values": ["Afebrile", "Afebrile"]},
    {"name": "Pulse Rate", "values": ["", "92"]}
  ]
}

SAMPLE JSON:
"""

PROMPT_FOOTER = """

JSON SCHEMA:
{schema}

CONSOLIDATED PATIENT CLINICAL CONTEXT:
"""


def normalize_context(context: dict) -> dict:
    if not context.get("include_investigation_table"):
        context["investigation_dates"] = []
        context["investigation_rows"] = []
    if not context.get("include_investigation_reports"):
        context["investigation_reports"] = []

    for field in ("unit_incharge", "treating_doctor", "discharge_signatory", "ayushman_code"):
        value = str(context.get(field, "")).strip()
        if value.upper() in {"NOT MENTIONED", "N/A", "NA", "NOT AVAILABLE"}:
            context[field] = ""

    treating = context.get("treating_doctor", "").strip()
    signatory = context.get("discharge_signatory", "").strip()
    if treating and not signatory:
        context["discharge_signatory"] = treating
    elif signatory and not treating:
        context["treating_doctor"] = signatory

    primary = context.get("treating_doctor", "").strip().upper()
    if primary:
        context["signing_doctors"] = [
            doctor
            for doctor in context.get("signing_doctors", [])
            if doctor.strip().upper() != primary
        ]

    return context


def build_prompt(clinical_text: str, sample: dict, schema: dict) -> str:
    sample_block = json.dumps(sample, indent=2, ensure_ascii=False)
    schema_block = json.dumps(schema, indent=2, ensure_ascii=False)
    return (
        PROMPT_HEADER
        + sample_block
        + PROMPT_FOOTER.format(schema=schema_block)
        + clinical_text
    )


def extract_context_from_markdown(
    clinical_text: str,
    *,
    sample: dict | None = None,
    schema: dict | None = None,
) -> dict:
    clinical_text = clinical_text.strip()
    if not clinical_text:
        raise ValueError("Clinical context markdown is empty.")

    sample = sample or load_sample()
    schema = schema or load_schema()
    prompt = build_prompt(clinical_text, sample, schema)
    context = normalize_context(gemini_service.generate_json(prompt, schema))
    validate_context(context)
    return context


def extract_context_json(
    context_md_path: Path,
    *,
    sample_path: Path = SAMPLE_PATH,
    output_path: Path | None = None,
) -> Path:
    if not context_md_path.is_file():
        raise FileNotFoundError(f"Missing clinical context: {context_md_path}")

    sample = load_sample(sample_path)
    context = extract_context_from_markdown(
        context_md_path.read_text(encoding="utf-8"),
        sample=sample,
    )

    if output_path is None:
        output_path = context_md_path.with_suffix(".json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return output_path
