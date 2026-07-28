from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import subprocess
import shutil
import os
import uuid
import mimetypes
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

R2_BUCKET = os.environ.get("R2_BUCKET")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT")


def r2_configured() -> bool:
    return bool(
        os.environ.get("R2_BUCKET")
        and os.environ.get("R2_ENDPOINT")
        and os.environ.get("ACCESS_KEY_ID")
        and os.environ.get("SECRET_ACCESS_KEY")
    )


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def job_prefix(job_id: str) -> str:
    return f"jobs/{job_id}"


def upload_job_dir(client, job_id: str, job_dir: str) -> str:
    prefix = job_prefix(job_id)
    for root, _, files in os.walk(job_dir):
        for name in files:
            local_path = os.path.join(root, name)
            rel = os.path.relpath(local_path, job_dir).replace("\\", "/")
            key = f"{prefix}/{rel}"
            extra: dict = {}
            content_type, _ = mimetypes.guess_type(local_path)
            if content_type:
                extra["ContentType"] = content_type
            if extra:
                client.upload_file(local_path, R2_BUCKET, key, ExtraArgs=extra)
            else:
                client.upload_file(local_path, R2_BUCKET, key)
    return f"{prefix}/index.html"


def public_index_url(job_id: str) -> Optional[str]:
    if not R2_ENDPOINT:
        return None
    return f"{R2_ENDPOINT}/{job_prefix(job_id)}/index.html"


def parse_job_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid job_id.") from exc


@app.get("/", response_class=HTMLResponse)
def root():
    return (
        "<h1>FastAPI pdf2htmlEX Service Online</h1>"
        "<p>Send a POST request to <code>/convert</code> with a PDF file attachment.</p>"
        "<p>View a conversion at <code>/view/{job_id}</code>.</p>"
    )


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "pdf_converter",
        "r2_configured": r2_configured(),
    }


def cleanup_job(input_path: str, job_dir: str):
    if os.path.exists(input_path):
        try:
            os.remove(input_path)
        except OSError:
            pass

    if os.path.exists(job_dir):
        try:
            shutil.rmtree(job_dir)
        except OSError:
            pass


@app.get("/view/{job_id}")
def view_job_redirect(job_id: str):
    job_id = parse_job_id(job_id)
    return RedirectResponse(url=f"/view/{job_id}/index.html", status_code=307)


@app.get("/view/{job_id}/{asset_path:path}")
def view_job(job_id: str, asset_path: str):
    if not r2_configured():
        raise HTTPException(status_code=503, detail="Object storage is not configured.")

    job_id = parse_job_id(job_id)
    if ".." in asset_path or asset_path.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="Invalid asset path.")

    key = f"{job_prefix(job_id)}/{asset_path}"
    client = r2_client()
    try:
        obj = client.get_object(Bucket=R2_BUCKET, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="Asset not found.") from exc
        raise HTTPException(status_code=502, detail="Failed to load asset from storage.") from exc

    body = obj["Body"].read()
    content_type = obj.get("ContentType") or mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    return Response(content=body, media_type=content_type)


@app.post("/convert")
async def convert_pdf(file: UploadFile = File(...)):
    if not r2_configured():
        raise HTTPException(status_code=503, detail="Object storage is not configured.")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded document must be a PDF format profile.")

    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.pdf"
    job_dir = f"dir_{job_id}"

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        os.makedirs(job_dir, exist_ok=True)

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
            job_dir,
            input_path,
            "index.html",
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            cleanup_job(input_path, job_dir)
            raise HTTPException(status_code=500, detail=f"Conversion processing fault: {result.stderr}")

        if not os.listdir(job_dir):
            cleanup_job(input_path, job_dir)
            raise HTTPException(status_code=500, detail="pdf2htmlEX completed but generated no files.")

        client = r2_client()
        index_key = upload_job_dir(client, job_id, job_dir)

        cleanup_job(input_path, job_dir)

        return {
            "job_id": job_id,
            "index_key": index_key,
            "view_url": f"/view/{job_id}/index.html",
            "index_url": public_index_url(job_id),
        }

    except HTTPException:
        raise
    except Exception as e:
        cleanup_job(input_path, job_dir)
        raise HTTPException(status_code=500, detail=str(e)) from e
