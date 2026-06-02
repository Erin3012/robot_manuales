from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from selenium.common.exceptions import WebDriverException

from main import (
    LOGIN_URL,
    collect_pending_case_rows,
    consult_case,
    consult_history_only,
    create_driver,
    download_session_resource,
    login_to_sitfa,
)


app = FastAPI(title="SITFA Web", version="0.2.0")
SESSION_COOKIE_NAME = "sitfa_session_id"


SESSION_STORE: dict[str, "WebState"] = {}
SESSION_STORE_LOCK = threading.Lock()


class LoginPayload(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ConsultPayload(BaseModel):
    rit: str = Field(min_length=1)


class HistoryDetailPayload(BaseModel):
    rit: str = Field(min_length=1)


class OpenPdfPayload(BaseModel):
    pdf_url: str = Field(min_length=1)
    prefix: str = "pdf"


@dataclass
class PdfArtifact:
    path: Path
    display_name: str


@dataclass
class WebState:
    session_id: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    driver: Any | None = None
    authenticated: bool = False
    username: str = ""
    headless: bool = True
    current_rit: str = ""
    status: str = "Listo"
    progress_value: float = 0.0
    progress_status: str = ""
    logs: list[str] = field(default_factory=list)
    pending_rows: list[dict] = field(default_factory=list)
    pending_status: str = ""
    results: dict[str, list[dict]] = field(default_factory=dict)
    detail_rit: str = ""
    detail_rows: list[dict] = field(default_factory=list)
    detail_status: str = ""
    pdf_files: dict[str, PdfArtifact] = field(default_factory=dict)
    cached_pdf_files: dict[str, Path] = field(default_factory=dict)
    current_pdf_id: str = ""

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        self.logs = self.logs[-300:]

    def clear_pdf(self) -> None:
        if self.current_pdf_id:
            artifact = self.pdf_files.pop(self.current_pdf_id, None)
            if artifact is not None:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.current_pdf_id = ""

    def reset_session(self) -> None:
        self.clear_pdf()
        self.authenticated = False
        self.username = ""
        self.current_rit = ""
        self.status = "Listo"
        self.progress_value = 0.0
        self.progress_status = ""
        self.results = {}
        self.pending_rows = []
        self.pending_status = ""
        self.detail_rit = ""
        self.detail_rows = []
        self.detail_status = ""

    def shutdown(self) -> None:
        with self.lock:
            self.reset_session()
            self.cleanup_session_files()
            if self.driver is not None:
                try:
                    self.driver.quit()
                except WebDriverException:
                    pass
                self.driver = None

    def cleanup_session_files(self) -> None:
        cache_dir = get_session_cache_dir(self.session_id)
        shutil.rmtree(cache_dir, ignore_errors=True)
        self.pdf_files.clear()
        self.cached_pdf_files.clear()
        self.current_pdf_id = ""


def get_or_create_state(session_id: str) -> WebState:
    with SESSION_STORE_LOCK:
        state = SESSION_STORE.get(session_id)
        if state is None:
            state = WebState(session_id=session_id)
            SESSION_STORE[session_id] = state
        return state


def delete_state(session_id: str) -> None:
    with SESSION_STORE_LOCK:
        SESSION_STORE.pop(session_id, None)


def close_state(session_id: str) -> None:
    state = None
    with SESSION_STORE_LOCK:
        state = SESSION_STORE.get(session_id)
    if state is not None:
        state.shutdown()
    delete_state(session_id)


def get_session_cache_dir(session_id: str) -> Path:
    cache_dir = Path(tempfile.gettempdir()) / "sitfa_web_cache" / session_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _resource_suffix(resource_url: str) -> str:
    suffix = Path(urlsplit(resource_url).path).suffix.lower()
    return suffix if suffix in {".pdf", ".doc", ".docx"} else ""


def _is_word_mime(content_type: str) -> bool:
    return content_type in {
        "application/msword",
        "application/vnd.ms-word.document.macroenabled.12",
        "application/vnd.ms-word.template.macroenabled.12",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    }


def _is_pdf_mime(content_type: str) -> bool:
    return content_type == "application/pdf"


def _looks_like_docx(content: bytes) -> bool:
    if not content.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    return any(name.startswith("word/") for name in names)


def _looks_like_doc(content: bytes) -> bool:
    return content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def _is_pdf_content(content: bytes) -> bool:
    return content.startswith(b"%PDF")


def _classify_resource(resource_url: str, content_type: str, content: bytes) -> str:
    suffix = _resource_suffix(resource_url)
    if _is_pdf_mime(content_type) or suffix == ".pdf" or _is_pdf_content(content):
        return "pdf"
    if suffix in {".doc", ".docx"} or _is_word_mime(content_type):
        return "word"
    if _looks_like_docx(content) or _looks_like_doc(content):
        return "word"
    return "unknown"


def _guess_source_suffix(resource_url: str, content_type: str, content: bytes) -> str:
    suffix = _resource_suffix(resource_url)
    if suffix:
        return suffix
    if _is_pdf_mime(content_type) or _is_pdf_content(content):
        return ".pdf"
    if _looks_like_docx(content) or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    if _looks_like_doc(content) or _is_word_mime(content_type):
        return ".doc"
    return ".bin"


def _preferred_display_name(resource_url: str) -> str:
    source_name = Path(urlsplit(resource_url).path).name.strip()
    if not source_name:
        source_name = "documento"
    source_path = Path(source_name)
    return source_path.with_suffix(".pdf").name


def _find_soffice_executable() -> str | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
    ]
    program_files = [
        Path(os.environ.get("PROGRAMFILES", "")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LibreOffice" / "program" / "soffice.exe",
    ]
    candidates.extend(str(path) if path.exists() else None for path in program_files)
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def _convert_word_to_pdf(source_path: Path, output_dir: Path) -> Path:
    soffice = _find_soffice_executable()
    if not soffice:
        raise RuntimeError("LibreOffice/soffice no está disponible en este equipo")

    output_dir.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [
        soffice,
        "--headless",
        "--nologo",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(source_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        detail = stderr if stderr else f"salida {result.returncode}"
        raise RuntimeError("LibreOffice no pudo convertir el documento: " + detail)

    converted_path = output_dir / (source_path.stem + ".pdf")
    if not converted_path.exists():
        raise RuntimeError("LibreOffice terminó sin generar el PDF convertido")
    return converted_path


def prepare_pdf_artifact(state: WebState, driver: Any, resource_url: str, prefix: str) -> PdfArtifact:
    normalized_url = resource_url.strip()
    if not normalized_url:
        raise HTTPException(status_code=400, detail="La fila no tiene archivo asociado")

    cache_dir = get_session_cache_dir(state.session_id)
    source_key = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    cache_path = state.cached_pdf_files.get(source_key)
    if cache_path is not None and (not cache_path.exists() or cache_path.stat().st_size <= 0):
        state.cached_pdf_files.pop(source_key, None)
        cache_path = None

    source_path: Path | None = None
    try:
        if cache_path is None:
            source_path = cache_dir / f"{source_key}.pdf"
            content_type, content = download_session_resource(driver, normalized_url, source_path)
            resource_kind = _classify_resource(normalized_url, content_type, content)
            source_suffix = _guess_source_suffix(normalized_url, content_type, content)

            if resource_kind == "pdf":
                cache_path = cache_dir / f"{source_key}.pdf"
                if source_path != cache_path:
                    if cache_path.exists():
                        cache_path.unlink(missing_ok=True)
                    source_path.replace(cache_path)
            elif resource_kind == "word":
                if source_suffix not in {".doc", ".docx"}:
                    guessed_suffix = ".docx" if _looks_like_docx(content) else ".doc"
                    renamed_source = source_path.with_suffix(guessed_suffix)
                    source_path.replace(renamed_source)
                    source_path = renamed_source
                elif source_path.suffix not in {".doc", ".docx"}:
                    renamed_source = source_path.with_suffix(source_suffix)
                    source_path.replace(renamed_source)
                    source_path = renamed_source

                cache_path = _convert_word_to_pdf(source_path, cache_dir)
                try:
                    source_path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                try:
                    source_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise HTTPException(
                    status_code=400,
                    detail="El archivo no es un PDF ni un documento DOC/DOCX compatible",
                )

            state.cached_pdf_files[source_key] = cache_path
    except HTTPException:
        raise
    except RuntimeError as exc:
        if source_path is not None:
            try:
                source_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if source_path is not None:
            try:
                source_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    assert cache_path is not None
    active_dir = cache_dir / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_path = active_dir / f"{prefix}_{uuid.uuid4().hex}.pdf"
    shutil.copy2(cache_path, active_path)
    display_name = _preferred_display_name(normalized_url)
    return PdfArtifact(path=active_path, display_name=display_name)


def ensure_driver(state: WebState, headless: bool) -> None:
    if state.driver is not None and state.headless == headless:
        return

    if state.driver is not None:
        try:
            state.driver.quit()
        except WebDriverException:
            pass
        state.driver = None

    state.append_log("Creando driver con Edge " + ("headless" if headless else "visible"))
    state.driver = create_driver(initial_url=LOGIN_URL, browser="edge", headless=headless)
    state.headless = headless
    state.authenticated = False


def get_session_id(request: Request, response: Response) -> str:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        session_id = uuid.uuid4().hex
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return session_id


def get_session_state(request: Request, response: Response) -> WebState:
    session_id = get_session_id(request, response)
    return get_or_create_state(session_id)


def require_session(state: WebState) -> Any:
    if not state.authenticated or state.driver is None:
        raise HTTPException(status_code=400, detail="Primero debes iniciar sesion")
    return state.driver


def refresh_pending_cases(state: WebState) -> list[dict]:
    driver = require_session(state)
    rows = collect_pending_case_rows(driver, log_callback=state.append_log)
    state.pending_rows = rows
    if rows:
        state.pending_status = f"{len(rows)} causas pendientes cargadas"
    else:
        state.pending_status = "No se encontraron causas pendientes"
    return rows


def escape_html(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_table(rows: list[dict]) -> str:
    if not rows:
        return "<div class='empty'>Sin datos.</div>"
    headers = [key for key in rows[0].keys() if key != "pdf_url"]
    html = ["<table><thead><tr>"]
    for header in headers:
        html.append("<th>" + escape_html(header) + "</th>")
    html.append("</tr></thead><tbody>")
    for row in rows:
        html.append("<tr>")
        for header in headers:
            html.append("<td>" + escape_html(row.get(header, "")) + "</td>")
        html.append("</tr>")
    html.append("</tbody></table>")
    return "".join(html)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PAGE


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def status(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    return {
        "logged_in": state.authenticated,
        "username": state.username,
        "current_rit": state.current_rit,
        "status": state.status,
        "progress_value": state.progress_value,
        "progress_status": state.progress_status,
        "headless": state.headless,
        "logs": state.logs[-200:],
        "pending_rows": state.pending_rows,
        "pending_status": state.pending_status,
        "results": state.results,
        "detail_rit": state.detail_rit,
        "detail_rows": state.detail_rows,
        "detail_status": state.detail_status,
        "current_pdf_url": "/api/pdf/" + state.current_pdf_id if state.current_pdf_id else "",
    }


@app.post("/api/login")
def api_login(payload: LoginPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        try:
            ensure_driver(state, True)
            state.append_log("Solicitud de login enviada")
            login_to_sitfa(state.driver, payload.username, payload.password, log_callback=state.append_log)
            state.username = payload.username
            state.current_rit = ""
            state.results = {}
            state.detail_rit = ""
            state.detail_rows = []
            state.detail_status = ""
            state.authenticated = True
            state.pending_rows = []
            state.pending_status = "Pendientes sin cargar"
            state.status = "Sesion iniciada"
            state.progress_value = 0.0
            state.progress_status = ""
            return {
                "ok": True,
                "username": state.username,
                "headless": state.headless,
                "pending_rows": state.pending_rows,
                "pending_status": state.pending_status,
            }
        except Exception as exc:
            close_state(state.session_id)
            raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/pending-cases")
def api_pending_cases(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        try:
            rows = refresh_pending_cases(state)
            state.status = state.pending_status or "Pendientes actualizados"
            return {"ok": True, "rows": rows, "status": state.pending_status}
        except Exception as exc:
            state.pending_rows = []
            state.pending_status = "No se pudieron cargar causas pendientes"
            state.append_log("Pendientes: " + str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/consult")
def api_consult(payload: ConsultPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        state.append_log("Consulta enviada para " + payload.rit)
        state.clear_pdf()
        state.progress_value = 0.0
        state.progress_status = "Iniciando consulta"
        results = consult_case(
            driver,
            payload.rit,
            log_callback=state.append_log,
            section_callback=lambda section, rows: state.results.__setitem__(section, rows),
            progress_callback=lambda message, percent: (
                setattr(state, "progress_status", message),
                setattr(state, "progress_value", percent),
                setattr(state, "status", message),
            ),
        )
        state.results = results
        state.current_rit = payload.rit
        state.status = "Consulta terminada para " + payload.rit
        state.progress_status = "Consulta terminada"
        state.progress_value = 100.0
        state.detail_rit = ""
        state.detail_rows = []
        state.detail_status = ""
        return {"ok": True, "rit": payload.rit, "results": results}


@app.post("/api/case-history")
def api_case_history(payload: HistoryDetailPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        state.append_log("Historia secundaria solicitada para " + payload.rit)
        state.detail_rit = payload.rit
        state.detail_rows = []
        state.detail_status = "Consultando historia de " + payload.rit
        try:
            history_rows = consult_history_only(driver, payload.rit, log_callback=state.append_log)
        except Exception as exc:
            state.detail_status = "No se pudo consultar la historia de " + payload.rit
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        state.detail_rows = history_rows
        state.detail_status = "Historia cargada para " + payload.rit
        return {"ok": True, "rit": payload.rit, "history": history_rows}


@app.post("/api/open-pdf")
def api_open_pdf(payload: OpenPdfPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        pdf_url = payload.pdf_url.strip()
        if not pdf_url:
            raise HTTPException(status_code=400, detail="La fila no tiene PDF asociado")

        state.clear_pdf()
        try:
            artifact = prepare_pdf_artifact(state, driver, pdf_url, payload.prefix)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        pdf_id = uuid.uuid4().hex
        state.pdf_files[pdf_id] = artifact
        state.current_pdf_id = pdf_id
        state.append_log("Documento abierto: " + artifact.display_name)
        return {"ok": True, "url": "/api/pdf/" + pdf_id, "name": artifact.display_name}


@app.post("/api/close-pdf")
def api_close_pdf(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        state.clear_pdf()
        state.append_log("PDF cerrado")
        return {"ok": True}


@app.get("/api/pdf/{pdf_id}")
def api_pdf(pdf_id: str, request: Request, response: Response):
    state = get_session_state(request, response)
    artifact = state.pdf_files.get(pdf_id)
    if artifact is None or not artifact.path.exists():
        raise HTTPException(status_code=404, detail="PDF no encontrado")
    return Response(
        content=artifact.path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="' + artifact.display_name + '"'},
    )


@app.post("/api/logout")
def api_logout(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    close_state(state.session_id)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.on_event("shutdown")
def shutdown_event() -> None:
    with SESSION_STORE_LOCK:
        session_ids = list(SESSION_STORE.keys())
    for session_id in session_ids:
        close_state(session_id)


HTML_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SITFA Web</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

    :root {
      --bg-0: #081120;
      --bg-1: #0d1730;
      --bg-2: #14264c;
      --surface: rgba(255, 255, 255, 0.88);
      --surface-strong: rgba(255, 255, 255, 0.96);
      --surface-soft: rgba(255, 255, 255, 0.68);
      --border: rgba(255, 255, 255, 0.32);
      --border-strong: rgba(255, 255, 255, 0.52);
      --text: #0f172a;
      --muted: #5f6c86;
      --primary: #2d6bff;
      --primary-2: #67a4ff;
      --primary-3: #8dc2ff;
      --accent: #7c3aed;
      --success: #22c55e;
      --shadow-lg: 0 28px 90px rgba(2, 8, 23, 0.28);
      --shadow-md: 0 14px 40px rgba(2, 8, 23, 0.16);
      --shadow-sm: 0 10px 24px rgba(2, 8, 23, 0.08);
      --radius-xl: 26px;
      --radius-lg: 20px;
      --radius-md: 16px;
      --radius-sm: 12px;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      font-family: "Plus Jakarta Sans", Inter, "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 15% 10%, rgba(103, 164, 255, 0.28), transparent 26%),
        radial-gradient(circle at 85% 8%, rgba(124, 58, 237, 0.22), transparent 28%),
        radial-gradient(circle at 100% 100%, rgba(34, 197, 94, 0.12), transparent 24%),
      linear-gradient(140deg, var(--bg-0), var(--bg-1) 48%, var(--bg-2));
      overflow-x: hidden;
    }
    body.pdf-resizing,
    body.pdf-resizing * {
      cursor: col-resize !important;
      user-select: none !important;
    }
    body::before,
    body::after {
      content: "";
      position: fixed;
      inset: auto;
      pointer-events: none;
      z-index: 0;
      filter: blur(30px);
      opacity: 0.55;
    }
    body::before {
      top: 8%;
      left: -6%;
      width: 18rem;
      height: 18rem;
      border-radius: 50%;
      background: rgba(103, 164, 255, 0.30);
      animation: float-blob 14s ease-in-out infinite;
    }
    body::after {
      bottom: 6%;
      right: -8%;
      width: 22rem;
      height: 22rem;
      border-radius: 50%;
      background: rgba(124, 58, 237, 0.22);
      animation: float-blob 18s ease-in-out infinite reverse;
    }
    @keyframes float-blob {
      0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
      50% { transform: translate3d(10px, -18px, 0) scale(1.06); }
    }
    .shell,
    .login-wrap {
      position: relative;
      z-index: 1;
    }
    .shell {
      min-height: 100vh;
      padding: 20px;
      animation: fade-up 520ms ease both;
    }
    .hidden { display: none; }
    .panel {
      position: relative;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow-lg);
      backdrop-filter: blur(18px) saturate(160%);
      -webkit-backdrop-filter: blur(18px) saturate(160%);
      overflow: hidden;
    }
    .panel::before {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(145deg, rgba(255,255,255,0.26), rgba(255,255,255,0.04));
      pointer-events: none;
    }
    .login-wrap {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 20px;
    }
    .login-card {
      width: 100%;
      max-width: 460px;
      padding: 28px;
      transform-origin: center;
      animation: card-enter 640ms cubic-bezier(.2,.8,.2,1) both;
    }
    .login-card .title {
      color: #0f172a;
      text-shadow: none;
    }
    .login-card .muted {
      color: var(--muted);
    }
    .login-card .status {
      background: rgba(248, 250, 252, 0.78);
      color: var(--muted);
    }
    .title {
      margin: 0 0 10px;
      font-size: 28px;
      line-height: 1.1;
      letter-spacing: -0.03em;
      color: #f8fbff;
      text-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
    }
    .muted { color: rgba(230, 239, 255, 0.74); }
    .stack { display: grid; gap: 12px; }
    .stack.compact { gap: 10px; }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: rgba(15, 23, 42, 0.62);
    }
    input, button {
      font: inherit;
      border-radius: 14px;
      border: 1px solid rgba(148, 163, 184, 0.26);
      padding: 11px 14px;
      transition:
        transform 180ms ease,
        box-shadow 180ms ease,
        background 180ms ease,
        border-color 180ms ease;
    }
    input {
      background: rgba(255, 255, 255, 0.88);
      color: var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.55);
    }
    input::placeholder { color: #8b98b6; }
    input:focus {
      outline: none;
      border-color: rgba(45, 107, 255, 0.58);
      box-shadow: 0 0 0 4px rgba(45, 107, 255, 0.16);
      transform: translateY(-1px);
    }
    button {
      background: linear-gradient(135deg, var(--primary), var(--accent));
      color: #fff;
      border-color: transparent;
      cursor: pointer;
      box-shadow: 0 10px 26px rgba(45, 107, 255, 0.26);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 16px 30px rgba(45, 107, 255, 0.30);
    }
    button:active { transform: translateY(0); }
    button.secondary {
      background: rgba(255, 255, 255, 0.58);
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.28);
      box-shadow: var(--shadow-sm);
    }
    button.secondary:hover {
      background: rgba(255, 255, 255, 0.8);
      box-shadow: var(--shadow-md);
    }
    button .btn-icon,
    .chip .chip-icon {
      display: inline-flex;
      width: 14px;
      height: 14px;
      flex: 0 0 auto;
    }
    button .btn-icon svg,
    .chip .chip-icon svg {
      width: 100%;
      height: 100%;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.9;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .status {
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.68);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 14px;
      color: var(--muted);
      font-size: 13px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
    }
    .progress-wrap {
      display: grid;
      gap: 8px;
    }
    .progress-track {
      width: 100%;
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(226, 232, 240, 0.72);
      border: 1px solid rgba(148, 163, 184, 0.18);
      box-shadow: inset 0 1px 3px rgba(15, 23, 42, 0.06);
    }
    .progress-bar {
      width: 0%;
      height: 100%;
      background: linear-gradient(90deg, var(--primary), var(--primary-2), #8b5cf6);
      border-radius: inherit;
      transition: width 260ms ease;
      box-shadow: 0 0 18px rgba(45, 107, 255, 0.36);
    }
    .progress-label {
      font-size: 12px;
      color: var(--muted);
      min-height: 16px;
      letter-spacing: 0.01em;
    }
    .progress-wrap.active .progress-bar { width: var(--progress, 0%); }
    .progress-wrap.idle .progress-track { background: rgba(240, 244, 250, 0.65); }
    .hero {
      padding: 20px 22px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      min-height: 100%;
      animation: card-enter 760ms cubic-bezier(.2,.8,.2,1) both;
    }
    .hero-brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }
    .brand-mark {
      width: 44px;
      height: 44px;
      border-radius: 16px;
      display: grid;
      place-items: center;
      color: #fff;
      font-size: 18px;
      font-weight: 800;
      letter-spacing: -0.06em;
      background: linear-gradient(145deg, var(--primary), var(--accent));
      box-shadow: 0 14px 28px rgba(45, 107, 255, 0.28);
      flex: 0 0 auto;
    }
    .hero-copy {
      display: grid;
      gap: 10px;
      min-width: 0;
    }
    .hero-kicker {
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--primary);
    }
    .hero h1 {
      margin: 0;
      font-size: 26px;
      line-height: 1.05;
      letter-spacing: -0.04em;
      font-family: "Plus Jakarta Sans", Inter, "Segoe UI", Arial, sans-serif;
      font-weight: 800;
      color: #0f172a;
    }
    .hero p {
      margin: 0;
      color: var(--muted);
      max-width: 40ch;
      line-height: 1.5;
      font-size: 14px;
    }
    .hero-chips {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.72);
      color: #475569;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.01em;
      box-shadow: var(--shadow-sm);
    }
    .chip.primary {
      background: linear-gradient(135deg, rgba(45, 107, 255, 0.96), rgba(79, 134, 255, 0.94));
      color: #fff;
      border-color: transparent;
      box-shadow: 0 14px 28px rgba(45, 107, 255, 0.22);
    }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .hero-actions {
      justify-content: flex-end;
      align-items: stretch;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      grid-template-areas:
        "main hero"
        "results results";
      gap: 16px;
      min-height: 0;
    }
    .consulta-layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 16px;
      min-height: 0;
    }
    .card {
      padding: 18px;
      min-width: 0;
      animation: card-enter 680ms cubic-bezier(.2,.8,.2,1) both;
    }
    .main-card {
      grid-area: main;
      background: var(--surface-strong);
    }
    .hero-card {
      grid-area: hero;
      background: linear-gradient(145deg, rgba(255,255,255,0.88), rgba(245,248,255,0.74));
    }
    .results {
      grid-area: results;
      display: grid;
      gap: 16px;
      min-height: 0;
    }
    .results-shell {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      --pdf-sidecar-width: 560px;
      gap: 16px;
      min-width: 0;
      min-height: 0;
      align-items: start;
    }
    .results.has-pdf .results-shell {
      grid-template-columns: minmax(0, 1fr) 12px var(--pdf-sidecar-width);
    }
    .results-main {
      min-width: 0;
      min-height: 0;
    }
    .pdf-resizer {
      display: none;
      align-self: stretch;
      width: 12px;
      border-radius: 999px;
      cursor: col-resize;
      touch-action: none;
      position: relative;
      background: transparent;
    }
    .results.has-pdf .pdf-resizer {
      display: block;
    }
    .pdf-resizer::before {
      content: "";
      position: absolute;
      top: 12px;
      bottom: 12px;
      left: 50%;
      width: 2px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.32);
      transform: translateX(-50%);
      transition: background 160ms ease, box-shadow 160ms ease;
    }
    .pdf-resizer:hover::before,
    .pdf-resizer.active::before {
      background: rgba(45, 107, 255, 0.68);
      box-shadow: 0 0 0 6px rgba(45, 107, 255, 0.08);
    }
    .pdf-sidecar {
      display: none;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
      border-radius: var(--radius-lg);
      border: 1px solid rgba(148, 163, 184, 0.18);
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(244,248,255,0.9));
      box-shadow: var(--shadow-lg);
    }
    .results.has-pdf .pdf-sidecar {
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .pdf-sidecar-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.18);
      background: linear-gradient(135deg, rgba(248,251,255,0.98), rgba(235,242,255,0.92));
    }
    .pdf-sidecar-title {
      min-width: 0;
      font-weight: 800;
      color: #0f172a;
      letter-spacing: -0.02em;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pdf-sidecar-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .pdf-sidecar iframe {
      width: 100%;
      height: 100%;
      min-height: 74vh;
      border: 0;
      background: #fff;
    }
    .consulta-card,
    .estado-card {
      min-width: 0;
    }
    .consulta-card h3,
    .estado-card h3,
    .tabs-area h3,
    .detail-box h4 {
      margin: 0 0 10px;
      font-size: 14px;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: #384158;
    }
    .estado-card {
      align-self: start;
    }
    .tabs-area {
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 10px;
      min-width: 0;
      min-height: 0;
    }
    .tab-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .tab-buttons button {
      background: rgba(255, 255, 255, 0.72);
      color: var(--text);
      border: 1px solid rgba(148, 163, 184, 0.22);
      box-shadow: none;
      font-weight: 700;
      gap: 8px;
    }
    .tab-buttons button:hover { background: rgba(255,255,255,0.92); }
    .tab-buttons button.active {
      background: linear-gradient(135deg, var(--primary), var(--accent));
      color: #fff;
      box-shadow: 0 12px 28px rgba(45, 107, 255, 0.22);
    }
    .tab-panels {
      min-width: 0;
      min-height: 0;
      overflow: auto;
      padding-right: 2px;
    }
    .tab-panel {
      display: none;
      animation: fade-up 300ms ease both;
    }
    .tab-panel.active {
      display: block;
    }
    .logs {
      white-space: pre-wrap;
      font-family: Consolas, monospace;
      font-size: 12px;
      background: linear-gradient(180deg, rgba(15, 23, 42, 0.95), rgba(15, 23, 42, 0.88));
      color: #d9e5ff;
      padding: 14px;
      border-radius: 16px;
      min-height: 220px;
      overflow: auto;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.06);
    }
    .section { margin-bottom: 16px; }
    .section h3 { margin: 0 0 8px; font-size: 15px; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: rgba(255,255,255,0.92);
      font-size: 12px;
    }
    th, td {
      border: 1px solid rgba(148, 163, 184, 0.18);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: linear-gradient(180deg, rgba(240, 244, 255, 0.96), rgba(232, 240, 255, 0.92));
      position: sticky;
      top: 0;
      z-index: 1;
    }
    tbody tr {
      transition: background 150ms ease, transform 150ms ease, box-shadow 150ms ease;
    }
    tbody tr:hover {
      background: rgba(235, 243, 255, 0.92);
    }
    .table-scroll {
      overflow: visible;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    .table-scroll table {
      border: 0;
      margin: 0;
    }
    .pdf-action {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 38px;
      height: 38px;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(248, 251, 255, 0.95);
      color: var(--primary);
      cursor: pointer;
      line-height: 1;
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.08);
    }
    .pdf-action svg {
      width: 20px;
      height: 20px;
      display: block;
    }
    .pdf-action:hover {
      background: rgba(232, 242, 255, 0.98);
      transform: translateY(-1px);
    }
    .rit-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--primary);
      cursor: pointer;
      font-weight: 700;
      text-decoration: none;
    }
    .rit-link:hover {
      text-decoration: underline;
      text-underline-offset: 2px;
    }
    .rit-link .btn-icon {
      width: 12px;
      height: 12px;
    }
    .detail-box {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid rgba(148, 163, 184, 0.18);
    }
    .detail-status {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }
    .empty { color: var(--muted); padding: 12px 0; }
    .small { font-size: 12px; color: var(--muted); }
    @keyframes fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    @keyframes fade-up {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes card-enter {
      from { opacity: 0; transform: translateY(18px) scale(0.99); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes modal-pop {
      from { opacity: 0; transform: translateY(16px) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @media (max-width: 1120px) {
      .grid {
        grid-template-columns: 1fr;
        grid-template-areas:
          "main"
          "hero"
          "results";
      }
      .results.has-pdf .results-shell {
        grid-template-columns: 1fr;
      }
      .pdf-resizer {
        display: none !important;
      }
      .consulta-layout {
        grid-template-columns: 1fr;
      }
      .hero {
        align-items: flex-start;
        flex-direction: column;
      }
    }
    @media (max-width: 920px) {
      .shell {
        padding: 14px;
      }
      .consulta-layout {
        grid-template-columns: 1fr;
      }
      .pdf-modal {
        width: 100%;
        height: 92vh;
      }
    }
  </style>
</head>
<body>
  <div id="loginView" class="login-wrap">
    <div class="panel login-card">
      <h1 class="title">SITFA Web</h1>
      <p class="muted"></p>
      <div class="stack">
        <label>Usuario <input id="username" autocomplete="username"></label>
        <label>Clave <input id="password" type="password" autocomplete="current-password"></label>
        <button type="button" id="loginBtn">Ingresar</button>
        <div id="loginStatus" class="status">Listo para iniciar sesion.</div>
      </div>
    </div>
  </div>

  <div id="appView" class="shell hidden">
    <div class="grid">
      <div class="panel card main-card">
        <div class="consulta-layout">
          <div class="stack consulta-card">
            <h3>Consulta</h3>
            <div class="stack">
              <label>RIT <input id="rit" placeholder="Z-1-2026"></label>
              <button type="button" id="consultBtn">Consultar</button>
            </div>
          </div>
          <div class="estado-card">
            <h3>Estado</h3>
            <div id="statusProgress" class="progress-wrap idle">
              <div class="progress-track">
                <div class="progress-bar"></div>
              </div>
              <div id="statusText" class="progress-label">Listo.</div>
            </div>
            <div id="meta" class="small"></div>
          </div>
        </div>
      </div>

      <div class="panel hero hero-card">
        <div class="hero-copy">
          <h1>Acciones rápidas</h1>
          <p>Consulta pendientes, actualiza la vista o cierra la sesión desde aquí.</p>
        </div>
        <div class="row hero-actions">
          <button type="button" class="secondary" id="loadPendingBtn">Cargar pendientes</button>
          <button type="button" class="secondary" id="refreshBtn">Actualizar</button>
          <button type="button" class="secondary" id="logoutBtn">Cerrar sesion</button>
        </div>
      </div>

      <div class="panel card results">
        <div class="results-shell">
          <div class="results-main">
            <div class="tabs-area">
              <div id="tabButtons" class="tab-buttons"></div>
              <div id="tabPanels" class="tab-panels"></div>
            </div>
          </div>
          <div id="pdfResizer" class="pdf-resizer" aria-hidden="true"></div>
          <aside id="pdfSidecar" class="pdf-sidecar" aria-label="Visor PDF">
            <div class="pdf-sidecar-header">
              <div id="pdfSidecarTitle" class="pdf-sidecar-title">PDF</div>
              <div class="pdf-sidecar-actions">
                <button type="button" class="secondary" id="openPdfSidecarTabBtn">Abrir en pestaña</button>
                <button type="button" class="secondary" id="closePdfSidecarBtn">Cerrar PDF</button>
              </div>
            </div>
            <iframe id="pdfSidecarFrame" title="Visor PDF"></iframe>
          </aside>
        </div>
      </div>
    </div>
  </div>

  <script>
    var state = { results: {}, logs: [], pdfUrl: "", pdfTitle: "", pdfPanelWidth: 560, loggedIn: false, pendingRows: [], pendingStatus: "", detail: { rit: "", rows: [], status: "", loading: false, error: "" } };
    var activeTab = "pendientes";
    var statusPollTimer = null;
    var lastRenderedResultsKey = "";
    var lastRenderedLogsKey = "";
    var lastRenderedDetailKey = "";
    var pdfResizeState = { active: false, startX: 0, startWidth: 0 };
    var PDF_MIN_WIDTH = 420;
    var PDF_MAX_WIDTH = 760;
    var PDF_DEFAULT_WIDTH = 560;
    var sectionTitles = {
      pendientes: "Pendientes",
      historia: "Historia",
      liquidacion: "Liquidacion",
      escritos: "Esc. por Resolv.",
      litigantes: "Litigantes",
      cons_lit: "Cons. Lit.",
      logs: "Logs"
    };
    var sections = ["pendientes", "historia", "liquidacion", "escritos", "litigantes", "cons_lit", "logs"];

    function esc(value) {
      if (value === null || value === undefined) {
        value = "";
      }
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function iconSvg(name) {
      var paths = {
        login: '<path d="M10 17H6.5A2.5 2.5 0 0 1 4 14.5v-9A2.5 2.5 0 0 1 6.5 3H10"/><path d="M14 7l3 3-3 3"/><path d="M17 10H8"/>',
        search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-3.5-3.5"/>',
        inbox: '<path d="M4 7h16v10H4z"/><path d="M4 12h4l2 3h4l2-3h4"/>',
        refresh: '<path d="M20 6v5h-5"/><path d="M20 11a8 8 0 1 0 2 5.3"/><path d="M4 18v-5h5"/>',
        logout: '<path d="M14 17h2.5A2.5 2.5 0 0 0 19 14.5v-9A2.5 2.5 0 0 0 16.5 3H14"/><path d="M4 12h11"/><path d="M8 8l-4 4 4 4"/><path d="M14 17H6.5A2.5 2.5 0 0 1 4 14.5v-9A2.5 2.5 0 0 1 6.5 3H14"/>',
        pdf: '<path d="M7 2.5h8.5l4.5 4.5V22H7z" fill="#fff" stroke="#ef4444" stroke-width="1.7" stroke-linejoin="round"/><path d="M15.5 2.5V7h4.5" fill="none" stroke="#ef4444" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><rect x="9" y="10.3" width="9.5" height="7.2" rx="1.6" fill="#ef4444"/><text x="13.75" y="15.15" text-anchor="middle" font-family="Arial, sans-serif" font-size="4.4" font-weight="800" fill="#ffffff">PDF</text>',
        arrowRight: '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
        external: '<path d="M14 5h5v5"/><path d="M10 14L19 5"/><path d="M19 14v5H5V5h5"/>',
        close: '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
        history: '<path d="M4 11a8 8 0 1 1 2.3 5.7"/><path d="M4 4v7h7"/><path d="M12 7v5l3 2"/>',
        list: '<path d="M8 6h12"/><path d="M8 12h12"/><path d="M8 18h12"/><path d="M4 6h.01"/><path d="M4 12h.01"/><path d="M4 18h.01"/>'
      };
      var body = paths[name] || paths.list;
      return '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + body + '</svg>';
    }

    function buttonContent(label, iconName) {
      return '<span class="btn-icon">' + iconSvg(iconName) + '</span><span class="btn-text">' + esc(label) + '</span>';
    }

    function setButtonContent(id, label, iconName) {
      var el = document.getElementById(id);
      if (el) {
        el.innerHTML = buttonContent(label, iconName);
      }
    }

    function clampPdfWidth(value, shellWidth) {
      var available = Math.max(0, Number(shellWidth) || 0);
      var maxWidth = Math.min(PDF_MAX_WIDTH, Math.max(PDF_MIN_WIDTH, Math.floor(available * 0.55)));
      var minWidth = Math.min(PDF_MIN_WIDTH, Math.max(320, Math.floor(available * 0.35)));
      var desired = Number(value) || PDF_DEFAULT_WIDTH;
      return Math.max(minWidth, Math.min(maxWidth, desired));
    }

    function xhrRequest(method, url, payload, callback) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) {
          return;
        }
        var raw = xhr.responseText || "";
        var data = {};
        try {
          data = raw ? JSON.parse(raw) : {};
        } catch (_err) {
          data = { detail: raw || "Respuesta invalida del servidor" };
        }
        callback(xhr.status, data);
      };
      xhr.onerror = function () {
        callback(0, { detail: "No se pudo conectar con el servidor" });
      };
      xhr.send(payload ? JSON.stringify(payload) : null);
    }

    function setText(id, text) {
      var el = document.getElementById(id);
      if (el) {
        el.textContent = text;
      }
    }

    function setProgress(active, text) {
      var wrap = document.getElementById("statusProgress");
      var label = document.getElementById("statusText");
      if (wrap) {
        wrap.className = active ? "progress-wrap active" : "progress-wrap idle";
      }
      if (label && typeof text === "string") {
        label.textContent = text;
      }
    }

    function setProgressState(value, text) {
      var wrap = document.getElementById("statusProgress");
      var label = document.getElementById("statusText");
      var pct = Math.max(0, Math.min(100, Number(value) || 0));
      if (wrap) {
        wrap.className = pct > 0 ? "progress-wrap active" : "progress-wrap idle";
        wrap.style.setProperty("--progress", pct.toFixed(1) + "%");
      }
      if (label && typeof text === "string") {
        label.textContent = text;
      }
    }

    function buildResultsRenderKey(results) {
      var parts = [];
      for (var i = 0; i < sections.length; i++) {
        var section = sections[i];
        var rows = (results && results[section]) ? results[section] : [];
        parts.push(section + ":" + rows.length);
        if (rows.length) {
          var first = rows[0] || {};
          var last = rows[rows.length - 1] || {};
          parts.push(String(first.rit || first.RIT || first.text || ""));
          parts.push(String(last.rit || last.RIT || last.text || ""));
        }
      }
      return parts.join("|");
    }

    function buildLogsRenderKey(lines) {
      return String((lines || []).length) + "|" + String((lines || []).slice(-1)[0] || "");
    }

    function buildDetailRenderKey(detail) {
      if (!detail) {
        return "";
      }
      return [
        detail.rit || "",
        detail.status || "",
        detail.loading ? "1" : "0",
        detail.error || "",
        (detail.rows || []).length
      ].join("|");
    }

    function startStatusPolling() {
      if (statusPollTimer) {
        return;
      }
      statusPollTimer = window.setInterval(function () {
        refreshStatus();
      }, 500);
    }

    function stopStatusPolling() {
      if (!statusPollTimer) {
        return;
      }
      window.clearInterval(statusPollTimer);
      statusPollTimer = null;
    }

    function setVisible(id, visible) {
      var el = document.getElementById(id);
      if (el) {
        el.className = visible ? "shell" : "login-wrap";
        if (!visible) {
          el.style.display = "grid";
        }
      }
    }

    var lastRenderedPdfUrl = "";

    function syncPdfSidecar() {
      var resultsCard = document.querySelector(".results");
      var resultsShell = document.querySelector(".results-shell");
      var resizer = document.getElementById("pdfResizer");
      var sidecar = document.getElementById("pdfSidecar");
      var frame = document.getElementById("pdfSidecarFrame");
      var title = document.getElementById("pdfSidecarTitle");
      var hasPdf = !!state.pdfUrl;

      if (resultsCard) {
        resultsCard.classList.toggle("has-pdf", hasPdf);
      }
      if (resultsShell) {
        resultsShell.style.setProperty("--pdf-sidecar-width", clampPdfWidth(state.pdfPanelWidth, resultsShell.clientWidth) + "px");
      }
      if (!sidecar || !frame) {
        return;
      }

      if (!hasPdf) {
        sidecar.classList.add("hidden");
        if (resizer) {
          resizer.classList.remove("active");
          resizer.classList.add("hidden");
        }
        frame.src = "about:blank";
        lastRenderedPdfUrl = "";
        return;
      }

      sidecar.classList.remove("hidden");
      if (resizer) {
        resizer.classList.remove("hidden");
      }
      if (title) {
        title.textContent = state.pdfTitle || "PDF";
      }
      if (state.pdfUrl !== lastRenderedPdfUrl) {
        frame.src = "about:blank";
        frame.src = state.pdfUrl + "?v=" + new Date().getTime();
        lastRenderedPdfUrl = state.pdfUrl;
      }
    }

    function updatePdfPanelWidth(nextWidth) {
      var resultsShell = document.querySelector(".results-shell");
      if (!resultsShell) {
        state.pdfPanelWidth = clampPdfWidth(nextWidth, 0);
        return;
      }
      state.pdfPanelWidth = clampPdfWidth(nextWidth, resultsShell.clientWidth);
      resultsShell.style.setProperty("--pdf-sidecar-width", state.pdfPanelWidth + "px");
    }

    function beginPdfResize(event) {
      var resultsCard = document.querySelector(".results");
      var resultsShell = document.querySelector(".results-shell");
      var resizer = document.getElementById("pdfResizer");
      if (event.button !== 0 || !resultsCard || !resultsCard.classList.contains("has-pdf") || !resultsShell || window.matchMedia("(max-width: 1120px)").matches) {
        return;
      }

      pdfResizeState.active = true;
      pdfResizeState.startX = event.clientX;
      pdfResizeState.startWidth = state.pdfPanelWidth || PDF_DEFAULT_WIDTH;
      if (resizer) {
        resizer.classList.add("active");
      }
      document.body.classList.add("pdf-resizing");
      event.preventDefault();
    }

    function movePdfResize(event) {
      if (!pdfResizeState.active) {
        return;
      }
      var resultsShell = document.querySelector(".results-shell");
      if (!resultsShell) {
        return;
      }
      var rect = resultsShell.getBoundingClientRect();
      var nextWidth = rect.right - event.clientX;
      updatePdfPanelWidth(nextWidth);
      event.preventDefault();
    }

    function endPdfResize() {
      if (!pdfResizeState.active) {
        return;
      }
      pdfResizeState.active = false;
      document.body.classList.remove("pdf-resizing");
      var resizer = document.getElementById("pdfResizer");
      if (resizer) {
        resizer.classList.remove("active");
      }
    }

    function openPdfSidecar(url, title) {
      state.pdfUrl = url || "";
      state.pdfTitle = title || "PDF";
      syncPdfSidecar();
    }

    function closePdfSidecar() {
      xhrRequest("POST", "/api/close-pdf", null, function (status, data) {
        if (status >= 200 && status < 300) {
          state.pdfUrl = "";
          state.pdfTitle = "";
          syncPdfSidecar();
          refreshStatus();
        } else {
          setProgressState(0, (data && data.detail) || "No se pudo cerrar el PDF");
        }
      });
    }

    function buildDataTable(rows, sectionName, options) {
      options = options || {};
      var enableRitDrilldown = !!options.enableRitDrilldown;
      var onRitClick = options.onRitClick || null;
      var table = document.createElement("table");
      var headers = [];
      var rowKeys = Object.keys(rows[0]);
      for (var k = 0; k < rowKeys.length; k++) {
        if (rowKeys[k] !== "pdf_url") {
          headers.push(rowKeys[k]);
        }
      }
      var hasPdf = false;
      for (var hp = 0; hp < rows.length; hp++) {
        if (rows[hp] && rows[hp].pdf_url) {
          hasPdf = true;
          break;
        }
      }
      if (hasPdf) {
        headers.push("__pdf_action__");
      }

      var head = document.createElement("thead");
      var headRow = document.createElement("tr");
      for (var h = 0; h < headers.length; h++) {
        var th = document.createElement("th");
        th.textContent = headers[h] === "__pdf_action__" ? "PDF" : headers[h];
        headRow.appendChild(th);
      }
      head.appendChild(headRow);

      var body = document.createElement("tbody");
      for (var r = 0; r < rows.length; r++) {
        (function (rowData, sectionNameInner) {
          var tr = document.createElement("tr");
          tr.ondblclick = function () {
            if (!rowData.pdf_url) {
              return;
            }
            xhrRequest("POST", "/api/open-pdf", { pdf_url: rowData.pdf_url, prefix: sectionNameInner }, function (status, data) {
              if (status >= 200 && status < 300 && data.ok) {
                openPdfSidecar(data.url, data.name || "PDF");
                refreshStatus();
              } else {
                setProgressState(0, (data && data.detail) || "No se pudo abrir el PDF");
              }
            });
          };

          for (var c = 0; c < headers.length; c++) {
            var td = document.createElement("td");
            if (headers[c] === "__pdf_action__") {
              if (rowData.pdf_url) {
                var btn = document.createElement("button");
                btn.type = "button";
                btn.className = "pdf-action";
                btn.title = "Abrir PDF";
                btn.innerHTML = iconSvg("pdf");
                btn.onclick = function () {
                  xhrRequest("POST", "/api/open-pdf", { pdf_url: rowData.pdf_url, prefix: sectionNameInner }, function (status, data) {
                    if (status >= 200 && status < 300 && data.ok) {
                      openPdfSidecar(data.url, data.name || "PDF");
                    } else {
                      setProgressState(0, (data && data.detail) || "No se pudo abrir el PDF");
                    }
                  });
                };
                td.appendChild(btn);
              }
            } else if (enableRitDrilldown && headers[c] === "RIT" && rowData[headers[c]]) {
              var ritBtn = document.createElement("button");
              ritBtn.type = "button";
              ritBtn.className = "rit-link";
              ritBtn.innerHTML = '<span class="btn-icon">' + iconSvg("arrowRight") + '</span><span class="btn-text">' + esc(String(rowData[headers[c]])) + '</span>';
              ritBtn.onclick = function (ritValue) {
                return function () {
                  if (typeof onRitClick === "function") {
                    onRitClick(ritValue);
                  }
                };
              }(String(rowData[headers[c]]));
              td.appendChild(ritBtn);
            } else {
              var value = rowData[headers[c]];
              td.textContent = value === null || value === undefined ? "" : String(value);
            }
            tr.appendChild(td);
          }
          body.appendChild(tr);
        })(rows[r], sectionName);
      }

      table.appendChild(head);
      table.appendChild(body);

      var wrapper = document.createElement("div");
      wrapper.className = "table-scroll";
      wrapper.appendChild(table);
      return wrapper;
    }

    function renderConsLitDetail(panel) {
      if (!state.detail) {
        return;
      }
      var detail = state.detail;
      if (!detail.rit && !detail.rows.length && !detail.loading && !detail.error) {
        return;
      }

      var box = document.createElement("div");
      box.className = "detail-box";

      var title = document.createElement("h4");
      title.textContent = "Historia de " + (detail.rit || "");
      box.appendChild(title);

      if (detail.status || detail.loading || detail.error) {
        var status = document.createElement("div");
        status.className = "detail-status";
        status.textContent = detail.error || detail.status || (detail.loading ? "Cargando historia..." : "");
        box.appendChild(status);
      }

      if (detail.loading) {
        panel.appendChild(box);
        return;
      }

      if (detail.rows && detail.rows.length) {
        box.appendChild(buildDataTable(detail.rows, "historia_secundaria", {}));
      } else if (!detail.error) {
        var empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Sin datos.";
        box.appendChild(empty);
      }

      panel.appendChild(box);
    }

    function renderLogs(lines) {
      setText("logs", (lines || []).join("\\n"));
    }

    function renderResults(results) {
      var buttonsRoot = document.getElementById("tabButtons");
      var panelsRoot = document.getElementById("tabPanels");
      buttonsRoot.innerHTML = "";
      panelsRoot.innerHTML = "";
      var sectionIcons = {
        pendientes: "inbox",
        historia: "history",
        liquidacion: "list",
        escritos: "list",
        litigantes: "search",
        cons_lit: "search",
        logs: "external"
      };

      if (!activeTab) {
        activeTab = "historia";
      }

      for (var i = 0; i < sections.length; i++) {
        var section = sections[i];
        var tabButton = document.createElement("button");
        tabButton.type = "button";
        tabButton.innerHTML = buttonContent(sectionTitles[section], sectionIcons[section] || "list");
        if (section === activeTab) {
          tabButton.className = "active";
        }
        tabButton.onclick = (function (sectionName) {
          return function () {
            activeTab = sectionName;
            renderResults(results);
          };
        })(section);
        buttonsRoot.appendChild(tabButton);

        var panel = document.createElement("div");
        panel.className = "tab-panel" + (section === activeTab ? " active" : "");

        if (section === "logs") {
          var logsBox = document.createElement("div");
          logsBox.className = "logs";
          logsBox.textContent = (state.logs || []).join("\\n");
          panel.appendChild(logsBox);
          panelsRoot.appendChild(panel);
          continue;
        }

        var rows = (results && results[section]) ? results[section] : [];
        var title = document.createElement("h3");
        title.textContent = sectionTitles[section] + " (" + rows.length + ")";
        panel.appendChild(title);

        if (!rows.length) {
          var empty = document.createElement("div");
          empty.className = "empty";
          empty.textContent = "Sin datos.";
          panel.appendChild(empty);
          panelsRoot.appendChild(panel);
          continue;
        }

        var table = buildDataTable(rows, section, {
          enableRitDrilldown: section === "cons_lit" || section === "pendientes",
          onRitClick: section === "pendientes" ? consultPendingRit : openConsLitHistory
        });
        panel.appendChild(table);

        if (section === "cons_lit") {
          renderConsLitDetail(panel);
        }
        panelsRoot.appendChild(panel);
      }
    }

    function openConsLitHistory(rit) {
      if (!rit) {
        return;
      }
      state.detail = {
        rit: rit,
        rows: [],
        status: "Consultando historia de " + rit,
        loading: true,
        error: ""
      };
      renderResults(state.results);
      xhrRequest("POST", "/api/case-history", { rit: rit }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          state.detail = {
            rit: rit,
            rows: [],
            status: "",
            loading: false,
            error: (data && data.detail) || "No se pudo consultar la historia"
          };
          refreshStatus();
          return;
        }
        state.detail = {
          rit: data.rit || rit,
          rows: data.history || [],
          status: "Historia cargada para " + (data.rit || rit),
          loading: false,
          error: ""
        };
        refreshStatus();
      });
    }

    function consultPendingRit(rit) {
      if (!rit) {
        return;
      }
      document.getElementById("rit").value = rit;
      activeTab = "historia";
      consult();
    }

    function loadPendingCases() {
      setProgress(true, "Cargando causas pendientes...");
      xhrRequest("GET", "/api/pending-cases", null, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudieron cargar las causas pendientes");
          return;
        }
        setProgressState(100, (data && data.status) || "Causas pendientes actualizadas");
        refreshStatus();
      });
    }

    function schedulePendingCasesLoad() {
      window.setTimeout(function () {
        loadPendingCases();
      }, 0);
    }

    function refreshStatus() {
      xhrRequest("GET", "/api/status", null, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setText("loginStatus", (data && data.detail) || "No se pudo consultar el estado");
          return;
        }

        state.loggedIn = !!data.logged_in;
        state.pendingRows = data.pending_rows || [];
        state.pendingStatus = data.pending_status || "";
        state.results = data.results || {};
        state.logs = data.logs || [];
        state.pdfUrl = data.current_pdf_url || "";
        if (!state.pdfUrl) {
          state.pdfTitle = "";
        }
        state.progressValue = Number(data.progress_value || 0);
        state.progressStatus = data.progress_status || "";
        state.detail = {
          rit: data.detail_rit || "",
          rows: data.detail_rows || [],
          status: data.detail_status || "",
          loading: false,
          error: ""
        };
        state.results = Object.assign({}, state.results || {}, { pendientes: state.pendingRows });

        document.getElementById("loginView").style.display = state.loggedIn ? "none" : "grid";
        document.getElementById("appView").style.display = state.loggedIn ? "block" : "none";

        if (state.loggedIn) {
          var meta = "Sesion activa";
          if (data.username) {
            meta += " | " + data.username;
          }
          if (data.current_rit) {
            meta += " | " + data.current_rit;
          }
          if (data.headless) {
            meta += " | headless";
          }
          if (state.pendingStatus) {
            meta += " | " + state.pendingStatus;
          }
          if (state.progressValue > 0) {
            setProgressState(state.progressValue, state.progressStatus || data.status || "Listo");
          }
          setText("meta", meta);
        } else {
          setText("loginStatus", "Sin sesion activa. Ingresa tus credenciales para continuar.");
        }

        var resultsKey = buildResultsRenderKey(state.results);
        var logsKey = buildLogsRenderKey(state.logs);
        var detailKey = buildDetailRenderKey(state.detail);

        if (logsKey !== lastRenderedLogsKey) {
          renderLogs(state.logs);
          lastRenderedLogsKey = logsKey;
        }
        if (resultsKey !== lastRenderedResultsKey) {
          renderResults(state.results);
          lastRenderedResultsKey = resultsKey;
        }
        if (detailKey !== lastRenderedDetailKey) {
          if (activeTab === "cons_lit") {
            renderResults(state.results);
            lastRenderedResultsKey = resultsKey;
          }
          lastRenderedDetailKey = detailKey;
        }
        syncPdfSidecar();
      });
    }

    function login() {
      var payload = {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
        headless: true
      };
      setText("loginStatus", "Iniciando sesion...");
      setProgressState(15, "Iniciando sesion...");
      xhrRequest("POST", "/api/login", payload, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setText("loginStatus", (data && data.detail) || "Error de login");
          setProgressState(0, (data && data.detail) || "Error de login");
          stopStatusPolling();
          return;
        }
        setText("loginStatus", "Sesion iniciada");
        setProgressState(0, "Sesion iniciada");
        state.pdfUrl = "";
        state.pdfTitle = "";
        syncPdfSidecar();
        lastRenderedResultsKey = "";
        lastRenderedLogsKey = "";
        lastRenderedDetailKey = "";
        stopStatusPolling();
        refreshStatus();
        schedulePendingCasesLoad();
      });
    }

    function consult() {
      var rit = document.getElementById("rit").value;
      activeTab = "historia";
      setProgressState(5, "Preparando consulta...");
      startStatusPolling();
      xhrRequest("POST", "/api/consult", { rit: rit }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "Error en consulta");
          stopStatusPolling();
          return;
        }
        setProgressState(100, "Consulta terminada");
        stopStatusPolling();
        refreshStatus();
      });
    }

    function logout() {
      xhrRequest("POST", "/api/logout", null, function () {
        setText("loginStatus", "Sesion cerrada");
        state.pdfUrl = "";
        state.pdfTitle = "";
        syncPdfSidecar();
        setProgressState(0, "Sesion cerrada");
        lastRenderedResultsKey = "";
        lastRenderedLogsKey = "";
        lastRenderedDetailKey = "";
        stopStatusPolling();
        activeTab = "pendientes";
        refreshStatus();
      });
    }

    setButtonContent("loginBtn", "Ingresar", "login");
    setButtonContent("consultBtn", "Consultar", "search");
    setButtonContent("loadPendingBtn", "Cargar pendientes", "inbox");
    setButtonContent("refreshBtn", "Actualizar", "refresh");
    setButtonContent("logoutBtn", "Cerrar sesion", "logout");
    setButtonContent("openPdfSidecarTabBtn", "Abrir en pestaña", "external");
    setButtonContent("closePdfSidecarBtn", "Cerrar PDF", "close");

    document.getElementById("loginBtn").onclick = login;
    document.getElementById("consultBtn").onclick = consult;
    document.getElementById("loadPendingBtn").onclick = loadPendingCases;
    document.getElementById("logoutBtn").onclick = logout;
    document.getElementById("refreshBtn").onclick = refreshStatus;
    document.getElementById("closePdfSidecarBtn").onclick = function () {
      closePdfSidecar();
    };
    document.getElementById("openPdfSidecarTabBtn").onclick = function () {
      if (state.pdfUrl) {
        window.open(state.pdfUrl, "_blank", "noopener,noreferrer");
      }
    };
    document.getElementById("pdfResizer").addEventListener("pointerdown", beginPdfResize);
    document.addEventListener("pointermove", movePdfResize);
    document.addEventListener("pointerup", endPdfResize);
    document.addEventListener("pointercancel", endPdfResize);
    window.addEventListener("resize", function () {
      if (state.pdfUrl) {
        syncPdfSidecar();
      }
    });

    refreshStatus();
  </script>
</body>
</html>
"""
