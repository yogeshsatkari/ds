from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
import subprocess
import shutil
import os
import uuid

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def root():
    return "<h1>FastAPI pdf2htmlEX Service Online</h1><p>Send a POST request to <code>/convert</code> with a PDF file attachment.</p>"

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "pdf_converter"}

def cleanup_job(input_path: str, job_dir: str, archive_path: str):
    """Safely removes temporary files and job directories after transmission completes."""
    if os.path.exists(input_path):
        try: os.remove(input_path)
        except Exception: pass
        
    if os.path.exists(job_dir):
        try: shutil.rmtree(job_dir)
        except Exception: pass
        
    if os.path.exists(archive_path):
        try: os.remove(archive_path)
        except Exception: pass

@app.post("/convert")
async def convert_pdf(file: UploadFile = File(...)):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Uploaded document must be a PDF format profile.")

    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.pdf"
    job_dir = f"dir_{job_id}"
    archive_path = f"assets_{job_id}.zip"

    try:
        # 1. Save incoming PDF file
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 2. Create an isolated directory for split output files
        os.makedirs(job_dir, exist_ok=True)

        # 3. Execute pdf2htmlEX with external asset embedding flags disabled
        command = [
            "pdf2htmlEX",
            "--zoom", "1.3",
            "--embed-css", "0",
            "--embed-font", "0",
            "--embed-image", "0",
            "--embed-javascript", "0",
            "--dest-dir", job_dir,
            input_path,
            "index.html"  # Standardizes the primary file name inside the zip
        ]
        
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            cleanup_job(input_path, job_dir, archive_path)
            raise HTTPException(status_code=500, detail=f"Conversion processing fault: {result.stderr}")

        # 4. Check if files were created in the destination directory
        if not os.listdir(job_dir):
            cleanup_job(input_path, job_dir, archive_path)
            raise HTTPException(status_code=500, detail="pdf2htmlEX completed but generated no files.")

        # 5. Compress the output directory into a single zip archive
        # shutil.make_archive automatically adds the '.zip' extension to the target path
        shutil.make_archive(archive_path.replace('.zip', ''), 'zip', job_dir)

        # 6. Stream zip to the client and trigger background cleanup
        return FileResponse(
            path=archive_path, 
            media_type="application/zip", 
            filename="converted_assets.zip",
            background=BackgroundTask(cleanup_job, input_path, job_dir, archive_path)
        )

    except Exception as e:
        cleanup_job(input_path, job_dir, archive_path)
        raise HTTPException(status_code=500, detail=str(e))
