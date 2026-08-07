from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
import asyncio
import shutil
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

import docx_conversion
import gemini_service
import r2_storage

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    docx_conversion.start_listener()
    yield
    docx_conversion.stop_listener()


app = FastAPI(lifespan=lifespan)

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


@app.get("/", response_class=HTMLResponse)
def root():
    return (
        "<h1>Discharge Summary API</h1>"
        "<ul>"
        "<li><code>POST /extract</code> — upload images, extract clinical context as markdown</li>"
        "<li><code>GET /extractions/{user_id}/{patient_id}/{extraction_id}</code> — fetch stored extraction</li>"
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
        "libreoffice_listener": docx_conversion.conversion_ready(),
    }


@app.get("/extractions/{user_id}/{patient_id}/{extraction_id}")
def get_extraction(user_id: str, patient_id: str, extraction_id: str):
    require_r2()
    user_id = parse_uuid(user_id, "user_id")
    patient_id = parse_uuid(patient_id, "patient_id")
    extraction_id = parse_uuid(extraction_id, "extraction_id")

    client = r2_storage.r2_client()
    key = r2_storage.extraction_key(user_id, patient_id, extraction_id)
    try:
        markdown = r2_storage.get_text(client, key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Extraction not found.") from exc

    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


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
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty image file: {filename}")
            image_items.append((filename, content))

        consolidated_context = gemini_service.run_extraction(image_items)

        client = r2_storage.r2_client()
        context_key = r2_storage.extraction_key(user_id, patient_id, extraction_id)
        r2_storage.put_text(client, context_key, consolidated_context)

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


@app.post("/convert/docx-to-pdf")
async def convert_docx_to_pdf_endpoint(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Uploaded document must be a DOCX file.")

    if not docx_conversion.libreoffice_binary():
        raise HTTPException(status_code=503, detail="LibreOffice is not installed.")

    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.docx"
    output_dir = f"dir_{job_id}"

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        pdf_path = await asyncio.to_thread(docx_conversion.convert_docx_to_pdf, input_path, output_dir)
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
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        cleanup_paths(input_path, output_dir)
