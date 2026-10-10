import os
import time
from typing import Optional
from google import genai
from google.genai import types

import config
import gemini_service
import metrics as metrics_lib

STANDARD_DISCHARGE_PROMPT = """You are a senior clinical documentation specialist and attending physician.

Your task is to transform the provided raw clinical patient context into a high-fidelity, professional PATIENT DISCHARGE SUMMARY in clean Markdown, adopting the structural design, clinical tone, and precision of a standardized institutional hospital discharge summary.

### FLEXIBILITY & HEADER GUIDELINES:
- Adapt the headers dynamically according to the actual clinical data available in the context:
  * Do NOT create empty headers or write "None / Not Applicable" sections if no supporting documentation exists. Skip headers that lack relevant data.
  * If the patient chart contains crucial clinical domains not covered in the default template (e.g., "Operative / Procedure Notes", "Microbiology & Infectious Disease Workup", "Multidisciplinary Specialty Consultations", "Pending Investigations", or "Dietary / Dialysis Orders" etc.), add dedicated Level 3 (`###`) headers for them.

---

### CORE CLINICAL RULES & GUARDRAILS:

1. Handling Ambiguity and Gaps with `[verify]`:
   - If any clinical parameter, handwriting, dosage, lab unit, vital sign, or date is blurry, conflicting, or uncertain, append `[verify]` (e.g., `GCS E4 V? M6 [verify]`, `Urea / Creatinine 77.3 / 1.65 [verify]`).
   - If an entry cannot be read at all, write `[verify]`. Do NOT invent or guess values.
   - For discharge-specific fields not documented in the chart (e.g., exact discharge date, finalized take-home drug dosages/durations), use placeholders such as `[to be filled]`, `___ [verify]`, or `[verify - to be filled by physician]`. Never fabricate discharge prescriptions.

2. Accurate Hospital Timeline & Presentation:
   - Identify the "Date of Admission" as the EARLIEST emergency department presentation or triage registration timestamp, NOT a subsequent internal bed allocation or ward transfer ticket.
   - Distinguish the acute condition on arrival (e.g., hypoxia, altered mental status, hemodynamic instability) from the clinical trajectory and final status upon discharge.
   - All bedside notes and labs belong to the same contiguous hospitalization episode. Ensure transcribed dates remain chronologically coherent with this active admission rather than misinterpreting cursive handwriting as unrelated prior years.

3. Investigation Trends & Data Synthesis:
   - Rather than isolated numbers, organize laboratory results chronologically across dated milestones (e.g., Admission/Baseline -> Mid-Stay/Nadir/Peak -> Pre-Discharge) in a concise Markdown table.
   - Cross-reference automated flags (such as "PLT Clump?") with manual peripheral blood smear reports to differentiate genuine clinical conditions from lab artifacts.
   - For neuro-imaging (CT, MRI) and radiological studies, formulate findings strictly based on the formal Radiologist's Impression.
   - Explicitly list recommended or ordered tests whose reports are missing in the chart under a "Pending / Not Seen in File [verify]" section.

---

### DOCUMENT STRUCTURE TEMPLATE:

# <HOSPITAL NAME IN ALL CAPS, WITH ACCREDITATION IF AVAILABLE>
## <DEPARTMENT / SPECIALTY> — DISCHARGE SUMMARY

| Patient Name | <Name> | Age / Sex | <Age> / <Sex> |
| UHID | <UHID Number> | Ward / Bed | <Ward / Bed details> |
| Date of Admission | <Earliest ER/Triage Date & Time> | Date of Discharge | <Date or '[to be filled]'> |
| Consulting Doctor | <Primary Specialist/Consultant> | Referred From | <Referring Hospital / 'Self / Walk-in'> |
| Address | <Patient Address, if present> | Guardian / Attendant | <Guardian / Next of Kin, if present> |

---

### Diagnosis:
- Bulleted list of primary diagnosis, secondary diagnoses/comorbidities, and complication resolution status (e.g., acute kidney injury – resolved, thrombocytopenia – resolving).

### On Admission:
- Structured ABCDE format:
  * Airway: <Airway status>
  * Breathing: <SpO2, O2 delivery method/flow, RR, auscultation findings>
  * Circulation: <BP, Pulse, capillary refill>
  * Disability: <GCS breakdown, pupil status, meningeal signs, neurological status>
  * Exposure: <Temperature / general condition>
  * Systemic Examination: CVS, P/A, and focal systemic exam findings.

### Brief History:
- Narrative of presenting complaints, onset, duration, progression prior to hospital arrival, and relevant past medical, surgical, or medication history.

### HOSPITAL COURSE
- Chronological, cohesive clinical narrative detailing:
  * Emergency resuscitation and initial stabilization.
  * Diagnostic workup and specialty consultations (e.g., Neurology review, Infectious Disease).
  * Medical management: antimicrobial therapy, supportive interventions, and dose adjustments.
  * Clinical course evolution, unit/triage transfers, and patient improvement.

### Key Investigations
- A structured chronological table comparing major panels across dates (e.g., Hb, TLC, Platelets, Renal parameters, Electrolytes, Liver enzymes).
- Serology, microbiology, CSF analysis, and radiology findings summarized accurately.
- Pending or missing test reports clearly flagged with `[verify]`.

### Treatment Given:
- Itemized list of all inpatient medications (IV antibiotics, antiepileptics, electrolyte corrections, gastroprotective agents, IV fluids) and supportive measures administered during the stay.

### On Discharge:
- Latest recorded bedside parameters, mental status, vitals, and physical state before release.

### Follow-up & Discharge Advice:
- Discharge medications with doses and durations (use placeholders `___ [verify]` if specific take-home orders were not yet finalized by the physician).
- Outpatient specialty clinics to visit and timeframe for review.
- Precautionary advice (e.g., seizure precautions, diet, hydration, infection control).
- Clear, specific emergency red flag symptoms requiring immediate return to the hospital.

CRITICAL INSTRUCTION:
Output pure Markdown directly. Do NOT include opening or closing code fence markers (such as ```markdown or ```) and do NOT provide any introductory or concluding conversational text.
"""


def default_synthesis_thinking_budget() -> int:
    return config.GEMINI_SYNTHESIS_THINKING_BUDGET


def extract_discharge_summary_md_from_markdown(
    clinical_text: str,
    max_retries: int = 4,
    thinking_budget: Optional[int] = None,
) -> tuple[str, dict]:
    clinical_text = clinical_text.strip()
    if not clinical_text:
        raise ValueError("Clinical context markdown is empty.")

    if thinking_budget is None:
        thinking_budget = default_synthesis_thinking_budget()

    client = gemini_service.gemini_client()
    prompt = f"{STANDARD_DISCHARGE_PROMPT}\n\nCONSOLIDATED PATIENT CLINICAL CONTEXT:\n{clinical_text}"

    last_error: BaseException = RuntimeError("Gemini standard discharge summary generation failed.")
    start_time = time.time()

    for attempt in range(1, max_retries + 1):
        try:
            config = types.GenerateContentConfig(
                temperature=0.1,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                safety_settings=gemini_service.clinical_safety_settings(),
            )
            response = client.models.generate_content(
                model=gemini_service.model_name(),
                contents=[prompt],
                config=config,
            )
            duration = time.time() - start_time
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

            usage = getattr(response, "usage_metadata", None)
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
            visible_tokens = getattr(usage, "candidates_token_count", 0) or 0
            thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0

            synthesis_metrics = metrics_lib.make_successful_synthesis_metrics(
                prompt_tokens=prompt_tokens,
                visible_tokens=visible_tokens,
                thought_tokens=thought_tokens,
                duration_seconds=duration,
                output_char_count=len(result),
                attempts=attempt,
            )
            return result, synthesis_metrics
        except Exception as exc:
            last_error = exc
            err = str(exc)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "getaddrinfo" in err:
                time.sleep(6 * attempt)
            elif attempt < max_retries:
                time.sleep(3)

    raise last_error
