from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import subprocess
import shutil
import os
import uuid
import mimetypes
from typing import Optional

from botocore.exceptions import ClientError
from dotenv import load_dotenv

import gemini_service
import r2_storage

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
)


def parse_uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.") from exc


def public_url(request: Request, path: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


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


def convert_pdf_to_html(input_path: str, output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    command = [
        "pdf2htmlEX",
        "--zoom",
        "1.3",
        "--embed-css",
        "0",
        "--embed-font",
        "0",
        "--embed-image",
        "0",
        "--embed-javascript",
        "0",
        "--dest-dir",
        output_dir,
        input_path,
        "index.html",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Conversion processing fault: {result.stderr}")
    if not os.listdir(output_dir):
        raise HTTPException(status_code=500, detail="pdf2htmlEX completed but generated no files.")


def r2_response(client, key: str, asset_path: str) -> Response:
    try:
        body, content_type = r2_storage.get_bytes(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Asset not found.") from exc
    except ClientError as exc:
        raise HTTPException(status_code=502, detail="Failed to load asset from storage.") from exc

    media_type = content_type or mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    return Response(content=body, media_type=media_type)


@app.get("/", response_class=HTMLResponse)
def root():
    return (
        "<h1>Discharge Summary API</h1>"
        "<ul>"
        "<li><code>POST /templates</code> — upload template PDF</li>"
        "<li><code>POST /extract</code> — upload images, extract clinical context</li>"
        "<li><code>POST /summarize</code> — generate discharge summary HTML</li>"
        "<li><code>POST /convert</code> — legacy PDF to HTML demo</li>"
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


@app.get("/view/{user_id}/template/{template_id}")
def view_template_redirect(user_id: str, template_id: str):
    user_id = parse_uuid(user_id, "user_id")
    template_id = parse_uuid(template_id, "template_id")
    return RedirectResponse(
        url=f"/view/{user_id}/template/{template_id}/index.html",
        status_code=307,
    )


@app.get("/view/{user_id}/template/{template_id}/{asset_path:path}")
def view_template(user_id: str, template_id: str, asset_path: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    template_id = parse_uuid(template_id, "template_id")
    if ".." in asset_path or asset_path.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="Invalid asset path.")

    key = r2_storage.template_asset_key(user_id, template_id, asset_path)
    return r2_response(r2_storage.r2_client(), key, asset_path)


@app.get("/view/{user_id}/summary/{patient_id}/{summary_id}")
def view_summary_redirect(user_id: str, patient_id: str, summary_id: str):
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")
    summary_id = parse_uuid(summary_id, "summary_id")
    return RedirectResponse(
        url=f"/view/{user_id}/summary/{patient_id}/{summary_id}/index.html",
        status_code=307,
    )


@app.get("/view/{user_id}/summary/{patient_id}/{summary_id}/{asset_path:path}")
def view_summary(user_id: str, patient_id: str, summary_id: str, asset_path: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")
    summary_id = parse_uuid(summary_id, "summary_id")
    if ".." in asset_path or asset_path.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="Invalid asset path.")

    key = r2_storage.summary_asset_key(user_id, patient_id, summary_id, asset_path)
    return r2_response(r2_storage.r2_client(), key, asset_path)


@app.get("/view/legacy/{job_id}")
def view_legacy_job_redirect(job_id: str):
    job_id = parse_uuid(job_id, "job_id")
    return RedirectResponse(url=f"/view/legacy/{job_id}/index.html", status_code=307)


@app.get("/view/legacy/{job_id}/{asset_path:path}")
def view_legacy_job(job_id: str, asset_path: str):
    require_r2()
    job_id = parse_uuid(job_id, "job_id")
    if ".." in asset_path or asset_path.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="Invalid asset path.")

    key = f"{r2_storage.legacy_job_prefix(job_id)}/{asset_path}"
    return r2_response(r2_storage.r2_client(), key, asset_path)


@app.post("/templates")
async def upload_template(
    request: Request,
    user_id: str = Form(...),
    file: UploadFile = File(...),
):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Template must be a PDF file.")

    template_id = str(uuid.uuid4())
    input_path = f"tmp_template_{template_id}.pdf"
    job_dir = f"dir_template_{template_id}"

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        convert_pdf_to_html(input_path, job_dir)

        client = r2_storage.r2_client()
        template_prefix = r2_storage.template_prefix(user_id, template_id)
        r2_storage.upload_dir(client, template_prefix, job_dir)

        return {
            "template_id": template_id,
            "view_url": public_url(
                request,
                f"/view/{user_id}/template/{template_id}/index.html",
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        cleanup_paths(input_path, job_dir)


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

    extraction_id = str(uuid.uuid4())
    image_items: list[tuple[str, bytes]] = []

    try:
        for index, upload in enumerate(files, start=1):
            filename = upload.filename or f"page-{index:03d}.jpg"
            gemini_service.validate_image_filename(filename)
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty image file: {filename}")
            image_items.append((filename, content))

        consolidated_context = gemini_service.run_extraction(image_items)

        client = r2_storage.r2_client()
        context_key = r2_storage.extraction_key(user_id, patient_id, extraction_id)
        r2_storage.put_text(client, context_key, consolidated_context, "text/markdown; charset=utf-8")

        return {
            "patient_id": patient_id,
            "extraction_id": extraction_id,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/summarize")
async def summarize_discharge(
    request: Request,
    user_id: str = Form(...),
    patient_id: str = Form(...),
    extraction_id: str = Form(...),
    template_id: str = Form(...),
):
    require_r2()
    require_gemini()

    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")
    extraction_id = parse_uuid(extraction_id, "extraction_id")
    template_id = parse_uuid(template_id, "template_id")

    summary_id = str(uuid.uuid4())
    client = r2_storage.r2_client()

    try:
        context_key = r2_storage.extraction_key(user_id, patient_id, extraction_id)
        template_html_key = r2_storage.template_asset_key(user_id, template_id, "index.html")

        clinical_context = r2_storage.get_text(client, context_key)
        template_html = r2_storage.get_text(client, template_html_key)
        filled_html = gemini_service.generate_discharge_summary(clinical_context, template_html)

        summary_prefix = r2_storage.summary_prefix(user_id, patient_id, summary_id)
        template_prefix = r2_storage.template_prefix(user_id, template_id)

        r2_storage.put_text(
            client,
            r2_storage.summary_asset_key(user_id, patient_id, summary_id, "index.html"),
            filled_html,
            "text/html; charset=utf-8",
        )
        r2_storage.copy_prefix(
            client,
            template_prefix,
            summary_prefix,
            exclude_names={"index.html"},
        )

        return {
            "summary_id": summary_id,
            "view_url": public_url(
                request,
                f"/view/{user_id}/summary/{patient_id}/{summary_id}/index.html",
            ),
        }
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Extraction or template not found.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/convert")
async def convert_pdf(request: Request, file: UploadFile = File(...)):
    require_r2()

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded document must be a PDF format profile.")

    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.pdf"
    job_dir = f"dir_{job_id}"

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        convert_pdf_to_html(input_path, job_dir)

        client = r2_storage.r2_client()
        r2_storage.upload_dir(client, r2_storage.legacy_job_prefix(job_id), job_dir)

        return {
            "job_id": job_id,
            "view_url": public_url(request, f"/view/legacy/{job_id}/index.html"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        cleanup_paths(input_path, job_dir)
