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
from data_to_docx.render import render_discharge_summary_from_template
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
        "<li><code>GET /extractions/{user_id}/{patient_id}</code> — fetch stored markdown</li>"
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


@app.get("/extractions/{user_id}/{patient_id}")
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


@app.get("/extractions/{user_id}/{patient_id}/discharge-summary.docx")
def get_discharge_summary_docx(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    docx_key = r2_storage.discharge_summary_docx_key(user_id, patient_id)
    json_key = r2_storage.extraction_json_key(user_id, patient_id)

    try:
        docx_bytes, _ = r2_storage.get_bytes(client, docx_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Discharge summary DOCX not found.") from exc

    filename = "discharge-summary.docx"
    try:
        context = r2_storage.get_json(client, json_key)
        filename = docx_filename(context.get("patient_name", ""))
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

        markdown, context = run_extraction_pipeline(image_items)
        docx_bytes = render_discharge_summary_from_template(context)

        client = r2_storage.r2_client()
        md_key = r2_storage.extraction_key(user_id, patient_id)
        json_key = r2_storage.extraction_json_key(user_id, patient_id)
        docx_key = r2_storage.discharge_summary_docx_key(user_id, patient_id)

        r2_storage.put_text(client, md_key, markdown)
        r2_storage.put_json(client, json_key, context)
        r2_storage.put_bytes(
            client,
            docx_key,
            docx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        filename = docx_filename(context.get("patient_name", ""))
        return Response(
            content=docx_bytes,
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
