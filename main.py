from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import subprocess
import shutil
import os
import uuid

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def root():
    return "<h1>FastAPI pdf2htmlEX Service Online</h1><p>Send a POST request to <code>/convert</code> with a PDF file attachment.</p>"

@app.post("/convert")
async def convert_pdf(file: UploadFile = File(...)):
    # Block invalid file types early
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Uploaded document must be a PDF format profile.")

    # Assign distinct safe transactional IDs
    job_id = str(uuid.uuid4())
    input_path = f"tmp_{job_id}.pdf"
    output_path = f"output_{job_id}.html"

    try:
        # Cache incoming web stream to file storage disk
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Execute system subprocess shell utility command execution string
        command = ["pdf2htmlEX", "--zoom", "1.3", input_path, output_path]
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Conversion processing fault: {result.stderr}")

        # Stream generated output page to user client, deleting tracking records after transmission
        return FileResponse(path=output_path, media_type="text/html", filename=f"converted.html")

    finally:
        # File management system teardown routines
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
