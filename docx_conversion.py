import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

LO_PROFILE_DIR = Path(os.environ.get("LO_PROFILE_DIR", "/tmp/lo_profile"))
LO_LISTENER_HOST = os.environ.get("LO_LISTENER_HOST", "127.0.0.1")
LO_LISTENER_PORT = int(os.environ.get("LO_LISTENER_PORT", "2002"))

_listener_process: subprocess.Popen | None = None
_convert_lock = threading.Lock()


def libreoffice_binary() -> str | None:
    for candidate in ("soffice", "libreoffice"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def unoconv_binary() -> str | None:
    return shutil.which("unoconv")


def listener_running() -> bool:
    return _listener_process is not None and _listener_process.poll() is None


def _port_open() -> bool:
    try:
        with socket.create_connection((LO_LISTENER_HOST, LO_LISTENER_PORT), timeout=1):
            return True
    except OSError:
        return False


def start_listener() -> bool:
    global _listener_process

    if listener_running() and _port_open():
        return True

    stop_listener()

    binary = libreoffice_binary()
    if not binary:
        return False

    LO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _listener_process = subprocess.Popen(
        [
            binary,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            f"-env:UserInstallation={LO_PROFILE_DIR.resolve().as_uri()}",
            (
                f"--accept=socket,host={LO_LISTENER_HOST},port={LO_LISTENER_PORT};"
                "urp;StarOffice.ServiceManager"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(40):
        if _listener_process.poll() is not None:
            _listener_process = None
            return False
        if _port_open():
            return True
        time.sleep(0.25)

    stop_listener()
    return False


def stop_listener() -> None:
    global _listener_process
    if _listener_process is None:
        return
    if _listener_process.poll() is None:
        _listener_process.terminate()
        try:
            _listener_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _listener_process.kill()
    _listener_process = None


def conversion_ready() -> bool:
    return listener_running() and _port_open() and unoconv_binary() is not None


def _convert_with_unoconv(input_path: str, pdf_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            unoconv_binary(),
            "--port",
            str(LO_LISTENER_PORT),
            "-f",
            "pdf",
            "-o",
            pdf_path,
            input_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _convert_with_spawn(input_path: str, output_dir: str, pdf_path: str) -> subprocess.CompletedProcess[str]:
    binary = libreoffice_binary()
    if not binary:
        raise RuntimeError("LibreOffice is not installed.")

    LO_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            binary,
            f"-env:UserInstallation={LO_PROFILE_DIR.resolve().as_uri()}",
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            input_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def convert_docx_to_pdf(input_path: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    pdf_path = os.path.join(output_dir, f"{base_name}.pdf")

    with _convert_lock:
        if not conversion_ready() and not start_listener():
            result = _convert_with_spawn(input_path, output_dir, pdf_path)
        else:
            result = _convert_with_unoconv(input_path, pdf_path)
            if result.returncode != 0:
                stop_listener()
                if not start_listener():
                    result = _convert_with_spawn(input_path, output_dir, pdf_path)
                else:
                    result = _convert_with_unoconv(input_path, pdf_path)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Unknown LibreOffice error.").strip()
        raise RuntimeError(f"DOCX to PDF conversion failed: {detail}")

    if not os.path.isfile(pdf_path):
        raise RuntimeError("LibreOffice completed but generated no PDF file.")

    return pdf_path
