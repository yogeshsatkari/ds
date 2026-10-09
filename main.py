from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
import subprocess
import shutil
import os
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import gemini_service
import r2_storage
from discharge_types.standard_md_to_docx.render import parse_patient_metadata_from_md
from pipeline.extraction_pipeline import run_extraction_pipeline


load_dotenv()

app = FastAPI()

_cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Patient-Id"],
)


def parse_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.") from exc


def require_r2() -> None:
    if not r2_storage.r2_configured():
        raise HTTPException(status_code=503, detail="Object storage is not configured.")


def require_gemini() -> None:
    if not gemini_service.gemini_configured():
        raise HTTPException(status_code=503, detail="Gemini is not configured.")


def cleanup_paths(*paths: str) -> None:
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            pass


def libreoffice_binary() -> str:
    for candidate in ("soffice", "libreoffice"):
        path = shutil.which(candidate)
        if path:
            return path
    raise HTTPException(status_code=503, detail="LibreOffice is not installed.")


def docx_filename(patient_name: str) -> str:
    patient_slug = patient_name.strip() or "discharge-summary"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in patient_slug)
    return f"{safe_name}.docx"


def convert_docx_to_pdf(input_path: str, output_dir: str, profile_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)

    command = [
        libreoffice_binary(),
        f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        input_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Unknown LibreOffice error.").strip()
        raise HTTPException(status_code=500, detail=f"DOCX to PDF conversion failed: {detail}")

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    pdf_path = os.path.join(output_dir, f"{base_name}.pdf")
    if not os.path.isfile(pdf_path):
        raise HTTPException(status_code=500, detail="LibreOffice completed but generated no PDF file.")

    return pdf_path


@app.get("/", response_class=HTMLResponse)
def root():
    return (
        "<h1>Discharge Summary API</h1>"
        "<ul>"
        "<li><code>POST /extract</code> — upload images, returns filled discharge summary DOCX</li>"
        "<li><code>GET /users/{user_id}/patients</code> — list past patients for a user</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/context.md</code> — fetch stored raw OCR markdown</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/discharge-summary.md</code> — fetch stored standard markdown</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/context.json</code> — fetch stored context JSON</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/discharge-summary.docx</code> — fetch stored discharge summary DOCX</li>"
        "<li><code>POST /convert/docx-to-pdf</code> — convert DOCX to PDF</li>"
        "</ul>"
    )


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "discharge_summary_api",
        "r2_configured": r2_storage.r2_configured(),
        "gemini_configured": gemini_service.gemini_configured(),
    }


@app.get("/users/{user_id}/patients")
def list_patients(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    patients = r2_storage.list_user_patients(client, user_id)
    return {"user_id": user_id, "patients": patients}


@app.get("/extractions/{user_id}/{patient_id}/context.md")
def get_extraction(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    key = r2_storage.extraction_key(user_id, patient_id)
    try:
        markdown = r2_storage.get_text(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Extraction not found.") from exc

    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


@app.get("/extractions/{user_id}/{patient_id}/discharge-summary.md")
def get_discharge_summary_md(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    key = r2_storage.discharge_summary_md_key(user_id, patient_id)
    try:
        markdown = r2_storage.get_text(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Discharge summary markdown not found.") from exc

    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


@app.get("/extractions/{user_id}/{patient_id}/context.json")
def get_extraction_context(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    key = r2_storage.extraction_json_key(user_id, patient_id)
    try:
        context = r2_storage.get_json(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Context JSON not found.") from exc

    return context


@app.get("/extractions/{user_id}/{patient_id}/metadata.json")
def get_extraction_metadata(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    meta_key = r2_storage.patient_metadata_key(user_id, patient_id)
    try:
        meta = r2_storage.get_json(client, meta_key)
        return meta
    except FileNotFoundError:
        # Fallback to get_patient_metadata for older records
        item = r2_storage.build_patient_list_item(client, user_id, patient_id)
        if not item:
            raise HTTPException(status_code=404, detail="Metadata not found.")
        return item


@app.get("/extractions/{user_id}/{patient_id}/discharge-summary.docx")
def get_discharge_summary_docx(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    docx_key = r2_storage.discharge_summary_docx_key(user_id, patient_id)
    meta_key = r2_storage.patient_metadata_key(user_id, patient_id)
    json_key = r2_storage.extraction_json_key(user_id, patient_id)
    summary_md_key = r2_storage.discharge_summary_md_key(user_id, patient_id)

    try:
        docx_bytes, _ = r2_storage.get_bytes(client, docx_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Discharge summary DOCX not found.") from exc

    filename = "discharge-summary.docx"
    # 1. Try reading patient_name from metadata.json
    try:
        meta = r2_storage.get_json(client, meta_key)
        if meta.get("patient_name"):
            filename = docx_filename(meta["patient_name"])
    except FileNotFoundError:
        # 2. Try reading patient_name from context.json
        try:
            context = r2_storage.get_json(client, json_key)
            if context.get("patient_name"):
                filename = docx_filename(context["patient_name"])
        except FileNotFoundError:
            # 3. Fallback to parsing from discharge-summary.md
            try:
                summary_text = r2_storage.get_text(client, summary_md_key)
                parsed_meta = parse_patient_metadata_from_md(summary_text)
                if parsed_meta.get("patient_name"):
                    filename = docx_filename(parsed_meta["patient_name"])
            except FileNotFoundError:
                pass

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/extract")
async def extract_images(
    user_id: str = Form(...),
    files: list[UploadFile] = File(...),
    patient_id: Optional[str] = Form(None),
    discharge_type: str = Form("standard"),
):
    require_r2()
    require_gemini()
    user_id = parse_uuid(user_id, "user_id")

    if not files:
        raise HTTPException(status_code=400, detail="At least one image is required.")

    if patient_id:
        patient_id = parse_uuid(patient_id, "patient_id")
    else:
        patient_id = str(uuid.uuid4())

    image_items: list[tuple[str, bytes]] = []

    try:
        for index, upload in enumerate(files, start=1):
            filename = upload.filename or f"page-{index:03d}.jpg"
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty image file: {filename}")
            image_items.append((filename, content))

        result = run_extraction_pipeline(image_items, discharge_type=discharge_type)

        client = r2_storage.r2_client()
        md_key = r2_storage.extraction_key(user_id, patient_id)
        docx_key = r2_storage.discharge_summary_docx_key(user_id, patient_id)
        meta_key = r2_storage.patient_metadata_key(user_id, patient_id)

        # 1. Store raw OCR markdown context
        r2_storage.put_text(client, md_key, result.context_md)

        # 2. Store flow-specific artifacts
        if result.discharge_type == "custom" and result.context_json:
            json_key = r2_storage.extraction_json_key(user_id, patient_id)
            r2_storage.put_json(client, json_key, result.context_json)
        elif result.discharge_type == "standard" and result.summary_md:
            summary_md_key = r2_storage.discharge_summary_md_key(user_id, patient_id)
            r2_storage.put_text(client, summary_md_key, result.summary_md)

        # 3. Store unified metadata.json for fast dashboard indexing
        if result.metadata:
            r2_storage.put_json(client, meta_key, result.metadata)

        # 4. Store generated DOCX
        r2_storage.put_bytes(
            client,
            docx_key,
            result.docx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        filename = docx_filename(result.patient_name)
        return Response(
            content=result.docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Patient-Id": patient_id,
            },
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc



@app.post("/convert/docx-to-pdf")
async def convert_docx_to_pdf_endpoint(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Uploaded document must be a DOCX file.")

    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.docx"
    output_dir = f"dir_{job_id}"
    profile_dir = f"lo_profile_{job_id}"

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        pdf_path = convert_docx_to_pdf(input_path, output_dir, profile_dir)
        with open(pdf_path, "rb") as pdf_file:
            pdf_bytes = pdf_file.read()

        output_name = f"{os.path.splitext(file.filename)[0]}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        cleanup_paths(input_path, output_dir, profile_dir)
