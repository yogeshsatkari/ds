import asyncio
import io
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
from typing import Optional
import uuid
import zipfile

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from context_to_discharge.render import parse_patient_metadata_from_md
from context_to_discharge.template_parser import parse_template_to_markdown
import gemini_service
from pipeline.extraction_pipeline import run_extraction_pipeline, ExtractionPipelineError
import r2_storage


class DoctorMetadataRequest(BaseModel):
    phone: str
    pin: str


ALLOWED_TEMPLATE_EXTENSIONS = {
    ".docx",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tiff",
    ".tif",
    ".bmp",
}


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
    expose_headers=[
        "X-Patient-Id",
        "X-Total-Duration-Seconds",
        "X-Total-Cost-INR",
        "X-Total-Tokens",
    ],
)


def parse_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        safe = "".join(c for c in value if c.isalnum() or c in "-_")
        if len(safe) >= 4 and safe == value:
            return safe
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
        "<li><code>GET /users/{user_id}/metadata</code> — fetch doctor profile metadata (phone, pin, timestamps)</li>"
        "<li><code>POST /users/{user_id}/metadata</code> — save/sync doctor profile metadata (phone, pin)</li>"
        "<li><code>GET /users/{user_id}/template</code> — fetch doctor profile template metadata</li>"
        "<li><code>POST /users/{user_id}/template</code> — upload and pre-parse doctor profile template</li>"
        "<li><code>DELETE /users/{user_id}/template</code> — remove doctor profile template</li>"
        "<li><code>GET /users/{user_id}/template/file</code> — download doctor profile template file(s)</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/context.md</code> — fetch stored raw OCR markdown</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/discharge-summary.md</code> — fetch stored standard markdown</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/template.md</code> — fetch stored template markdown (if custom template provided)</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/context.json</code> — fetch stored context JSON (legacy)</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/metadata.json</code> — fetch stored patient metadata</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/metrics.json</code> — fetch performance & AI cost metrics</li>"
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


@app.get("/dashboard/patients")
def list_dashboard_patients():
    require_r2()
    client = r2_storage.r2_client()
    patients = r2_storage.list_all_patients(client)
    return {"patients": patients}


@app.get("/users/{user_id}/patients")
def list_patients(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    patients = r2_storage.list_user_patients(client, user_id)
    return {"user_id": user_id, "patients": patients}


@app.get("/users/{user_id}/template")
def get_user_template(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    meta = r2_storage.get_user_template_meta(client, user_id)
    if not meta:
        return {"mode": "standard"}
    return meta


@app.post("/users/{user_id}/template")
async def save_user_template(
    user_id: str,
    template_files: list[UploadFile] = File(...),
):
    require_r2()
    require_gemini()
    user_id = parse_uuid(user_id, "user_id")

    if not template_files:
        raise HTTPException(status_code=400, detail="At least one template file is required.")

    template_items: list[tuple[str, bytes]] = []
    primary_filename = ""

    for index, upload in enumerate(template_files, start=1):
        filename = (upload.filename or f"template-{index:03d}").strip()
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_TEMPLATE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format '{ext}' in '{filename}'. Allowed formats: .docx, .pdf, or images (.jpg, .jpeg, .png, .webp, .tiff).",
            )
        content = await upload.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"Empty uploaded file: {filename}")
        template_items.append((filename, content))
        if not primary_filename:
            primary_filename = filename

    if not template_items:
        raise HTTPException(status_code=400, detail="No readable template content found in upload.")

    # Pre-parse the template to Markdown structure across user profile
    try:
        template_md, _ = parse_template_to_markdown(template_items)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to parse template: {exc}",
        ) from exc

    client = r2_storage.r2_client()
    r2_storage.put_user_template(
        client=client,
        user_id=user_id,
        template_items=template_items,
        filename=primary_filename,
        template_md=template_md,
    )

    return {
        "status": "ok",
        "mode": "custom",
        "filename": primary_filename,
        "file_count": len(template_items),
    }


@app.delete("/users/{user_id}/template")
def remove_user_template(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    r2_storage.delete_user_template(client, user_id)
    return {"status": "ok", "mode": "standard"}


@app.get("/users/{user_id}/template/file")
def download_user_template_file(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    meta = r2_storage.get_user_template_meta(client, user_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Template not found.")

    files = r2_storage.get_user_template_files(client, user_id)
    if not files:
        raise HTTPException(status_code=404, detail="Template files not found.")

    if len(files) == 1:
        fname, fbytes = files[0]
        content_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        return Response(
            content=fbytes,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # Multi-file template: package into a zip archive
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, fbytes in files:
            zf.writestr(fname, fbytes)
    base_name = os.path.splitext(meta.get("filename") or "template")[0]
    zip_filename = f"{base_name}_template.zip"
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@app.post("/users/{user_id}/metadata")
async def update_doctor_metadata(user_id: str, req: DoctorMetadataRequest):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    clean_phone = "".join(c for c in req.phone if c.isdigit())[-10:] if req.phone else ""
    clean_pin = req.pin.strip() if req.pin else ""

    client = r2_storage.r2_client()
    data = r2_storage.save_user_metadata(client, user_id, clean_phone, clean_pin)
    return {"status": "ok", "metadata": data}


@app.get("/users/{user_id}/metadata")
async def fetch_doctor_metadata(user_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    client = r2_storage.r2_client()
    data = r2_storage.get_user_metadata(client, user_id)
    if not data:
        raise HTTPException(status_code=404, detail="Doctor metadata not found.")
    return data


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


@app.get("/extractions/{user_id}/{patient_id}/template.md")
def get_template_md(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    key = r2_storage.template_md_key(user_id, patient_id)
    try:
        markdown = r2_storage.get_text(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Template markdown not found.") from exc

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


@app.get("/extractions/{user_id}/{patient_id}/metrics.json")
def get_extraction_metrics(user_id: str, patient_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")

    client = r2_storage.r2_client()
    metrics_key = r2_storage.extraction_metrics_key(user_id, patient_id)
    try:
        metrics = r2_storage.get_json(client, metrics_key)
        return metrics
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Metrics not found.") from exc


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
                if not parsed_meta.get("patient_name"):
                    try:
                        context_text = r2_storage.get_text(client, r2_storage.extraction_key(user_id, patient_id))
                        parsed_meta = parse_patient_metadata_from_md(summary_text, fallback_context_md=context_text)
                    except Exception:
                        pass
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
    template_files: list[UploadFile] = File(default=[]),
    patient_id: Optional[str] = Form(None),
    discharge_type: Optional[str] = Form(None),
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
            filename = upload.filename or f"upload-{index:03d}"
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty uploaded file: {filename}")

            if filename.lower().endswith(".pdf"):
                pdf_pages = gemini_service.pdf_to_page_images(content, base_filename=filename)
                if not pdf_pages:
                    raise HTTPException(status_code=400, detail=f"PDF '{filename}' contains no readable pages.")
                image_items.extend(pdf_pages)
            else:
                image_items.append((filename, content))

        if not image_items:
            raise HTTPException(status_code=400, detail="No readable pages or images found in upload.")

        # Ingest doctor-provided template files (if any) or load persistent profile template
        template_items: list[tuple[str, bytes]] = []
        preparsed_template_md: Optional[str] = None
        template_name: Optional[str] = None
        is_one_off_template: bool = False
        client = r2_storage.r2_client()

        if template_files:
            for index, upload in enumerate(template_files, start=1):
                t_filename = upload.filename or f"template-{index:03d}"
                t_content = await upload.read()
                if t_content:
                    template_items.append((t_filename, t_content))
            effective_discharge_type = "custom"
            is_one_off_template = True
            if template_items:
                template_name = template_items[0][0]
        elif discharge_type and discharge_type.strip().lower() in ("standard", "force_standard"):
            # Doctor explicitly requested institutional standard format
            effective_discharge_type = "standard"
            template_name = "Institutional Standard"
        else:
            # Check if doctor has a stored custom template in R2
            profile_meta = r2_storage.get_user_template_meta(client, user_id)
            if profile_meta and profile_meta.get("mode") == "custom":
                # Prefer pre-parsed template.md to avoid re-parsing latency and API token spend
                preparsed_template_md = r2_storage.get_user_template_md(client, user_id)
                if not preparsed_template_md:
                    template_items = r2_storage.get_user_template_files(client, user_id)
                effective_discharge_type = "custom"
                template_name = profile_meta.get("filename") or "Doctor Profile Template"
            else:
                effective_discharge_type = discharge_type or "standard"
                template_name = "Institutional Standard"

        result = await asyncio.to_thread(
            run_extraction_pipeline,
            image_items=image_items,
            template_items=template_items or None,
            discharge_type=effective_discharge_type,
            preparsed_template_md=preparsed_template_md,
            template_name=template_name,
        )

        md_key = r2_storage.extraction_key(user_id, patient_id)
        docx_key = r2_storage.discharge_summary_docx_key(user_id, patient_id)
        meta_key = r2_storage.patient_metadata_key(user_id, patient_id)
        summary_md_key = r2_storage.discharge_summary_md_key(user_id, patient_id)

        # 1. Store raw OCR markdown context
        r2_storage.put_text(client, md_key, result.context_md)

        # 2. Store synthesized discharge summary Markdown
        if result.summary_md:
            r2_storage.put_text(client, summary_md_key, result.summary_md)

        # 3. Store template Markdown if a custom template was used
        if result.template_md:
            template_key = r2_storage.template_md_key(user_id, patient_id)
            r2_storage.put_text(client, template_key, result.template_md)

        # 3b. For one-off templates uploaded specifically for this patient, also preserve raw file(s)
        if is_one_off_template and template_items:
            for item_filename, item_bytes in template_items:
                safe_name = os.path.basename(item_filename).strip() or "template_file"
                one_off_key = f"{r2_storage.extraction_prefix(user_id, patient_id)}/template_files/{safe_name}"
                mime, _ = mimetypes.guess_type(safe_name)
                r2_storage.put_bytes(client, one_off_key, item_bytes, content_type=mime or "application/octet-stream")

        # 4. Store unified metadata.json for fast dashboard indexing
        if result.metadata:
            r2_storage.put_json(client, meta_key, result.metadata)

        # 5. Store generated DOCX
        r2_storage.put_bytes(
            client,
            docx_key,
            result.docx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        # 6. Store AI performance and cost metrics
        if result.metrics:
            metrics_key = r2_storage.extraction_metrics_key(user_id, patient_id)
            r2_storage.put_json(client, metrics_key, result.metrics)

        filename = docx_filename(result.patient_name)
        summary_meta = result.metrics.get("summary", {}) if result.metrics else {}
        return Response(
            content=result.docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Patient-Id": patient_id,
                "X-Total-Duration-Seconds": str(summary_meta.get("total_duration_seconds", "")),
                "X-Total-Cost-INR": str(summary_meta.get("total_cost_inr", "")),
                "X-Total-Tokens": str(summary_meta.get("total_tokens", "")),
            },
        )
    except ExtractionPipelineError as exc:
        client = r2_storage.r2_client()
        if exc.partial_context_md:
            md_key = r2_storage.extraction_key(user_id, patient_id)
            r2_storage.put_text(client, md_key, exc.partial_context_md)
        if exc.metrics:
            metrics_key = r2_storage.extraction_metrics_key(user_id, patient_id)
            r2_storage.put_json(client, metrics_key, exc.metrics)
        raise HTTPException(
            status_code=500,
            detail=f"{exc}. Partial token spend recorded in metrics.json (Patient ID: {patient_id})",
            headers={"X-Patient-Id": patient_id},
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
