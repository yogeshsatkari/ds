import os
import time
from google import genai
from google.genai import types

import gemini_service

STANDARD_DISCHARGE_PROMPT = """You are an expert clinical documentation specialist and medical transcriptionist.

Your task is to transform the provided raw clinical patient context (from admission, daily rounds, doctor notes, vitals charts, emergency assessment, and treatment records) into a comprehensive, professional, and well-structured PATIENT DISCHARGE SUMMARY in Markdown.

FORMATTING AND STRUCTURE GUIDELINES:

1. TOP HEADER (Direct titles, no extra 'Hospital Name:' key prefix):
   - Level 1 Heading: Hospital Name with accreditation (e.g. `# ALL INDIA INSTITUTE OF MEDICAL SCIENCES, RISHIKESH` or `# KAILASH HOSPITAL, DEHRADUN (NABH ACCREDITED)`)
   - Level 2 Heading: Department / Specialty and document type (e.g. `## Department of Neurology / General Medicine — DISCHARGE SUMMARY`)

2. PATIENT DEMOGRAPHICS TABLE (4-column Markdown table directly below header):
   | Patient Name | <Name in Title Case / ALL CAPS> | Age / Sex | <Age> / <Sex> |
   | UHID | <UHID Number> | Ward / Bed | <Ward / Bed details> |
   | Date of Admission | <Admission Date & Time> | Date of Discharge | <Discharge Date or '[to be filled]'> |
   | Consulting Doctor | <Primary Specialist Doctor> | Attending / Shifting Doctor | <Attending or Ward Doctor> |
   (Include other relevant fields like MRD No, Relation, Address, or Referred From if present in the context).

3. DIVIDER:
   Use `---` directly after the demographics table.

4. CLINICAL SECTIONS (Use Level 3 Headings `###`):
   - `### Diagnosis:` Bulleted list of primary diagnosis and comorbidities / secondary diagnoses.
   - `### History & Chief Complaints:` Presentation complaints, duration, severity, pain score (VAS).
   - `### On Admission (General & Systemic Examination):` Airway, baseline vitals, GCS, systemic findings (Respiratory, CVS, P/A, CNS).
   - `### Recorded Vitals & Clinical Monitoring:` (Include a Markdown table if dated vitals/labs exist across multiple checkpoints/dates).
   - `### Clinical Course & Hospital Management:` Detailed narrative of inpatient stay, consultations, emergency treatment, medications administered, clinical progress, and response to therapy.
   - `### Discharge Medications & Advice:` Markdown table containing columns: `| Sr. No. | Medicine Name | Dose | Route | Frequency | Instructions |`.
   - `### Lifestyle Advice & Preventive Care:` Diet, activity, trigger avoidance, and care instructions.
   - `### Follow-up & Red Flags:` OPD review schedule, planned consults, and emergency warning signs when to seek immediate medical attention.

CRITICAL RULES:
- Do NOT hallucinate or invent clinical facts; synthesize all documented facts with 100% clinical precision.
- Do NOT output conversational filler, preamble, or markdown code block fences (like ```markdown). Output raw Markdown text directly.
"""


def extract_discharge_summary_md_from_markdown(clinical_text: str, max_retries: int = 4) -> str:
    clinical_text = clinical_text.strip()
    if not clinical_text:
        raise ValueError("Clinical context markdown is empty.")

    client = gemini_service.gemini_client()
    prompt = f"{STANDARD_DISCHARGE_PROMPT}\n\nCONSOLIDATED PATIENT CLINICAL CONTEXT:\n{clinical_text}"

    last_error: BaseException = RuntimeError("Gemini standard discharge summary generation failed.")

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=gemini_service.model_name(),
                contents=[prompt],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                ),
            )
            result = (response.text or "").strip()
            # Strip code block wrappers if any were returned
            if result.startswith("```markdown"):
                result = result[len("```markdown"):].strip()
            elif result.startswith("```"):
                result = result[len("```"):].strip()
            if result.endswith("```"):
                result = result[:-3].strip()

            if not result:
                raise RuntimeError("Gemini returned empty discharge summary markdown.")
            return result
        except Exception as exc:
            last_error = exc
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "getaddrinfo" in err:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error
