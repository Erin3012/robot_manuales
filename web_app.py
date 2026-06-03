from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
import zipfile
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from selenium.common.exceptions import WebDriverException

from main import (
    LOGIN_URL,
    collect_pending_case_rows,
    consult_case,
    consult_history_only,
    emit_log,
    extract_rows_from_table,
    extract_tables_from_html_source,
    extract_popup_frame_src,
    open_cartola_bco_estado_popup_from_litigantes,
    create_driver,
    fetch_session_resource_text,
    download_session_resource,
    login_to_sitfa,
    resolve_litigantes_popup_url,
    snapshot_window_urls,
    wait_for_window_content,
    wait_for_window_update,
    wait_for_ready,
    normalize_header_name,
    normalize_litigantes_value,
)


app = FastAPI(title="SITFA Web", version="0.2.0")
SESSION_COOKIE_NAME = "sitfa_session_id"


SESSION_STORE: dict[str, "WebState"] = {}
SESSION_STORE_LOCK = threading.Lock()


class LoginPayload(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    headless: bool = True


class ConsultPayload(BaseModel):
    rit: str = Field(min_length=1)


class HistoryDetailPayload(BaseModel):
    rit: str = Field(min_length=1)


class SelectLitigantePayload(BaseModel):
    litigante: dict[str, Any] = Field(default_factory=dict)


class CartolaConsultPayload(BaseModel):
    account: str = ""
    start_date: str = ""
    end_date: str = ""


class OpenPdfPayload(BaseModel):
    pdf_url: str = Field(min_length=1)
    prefix: str = "pdf"
    pdf_title: str = ""


class ClosePdfPayload(BaseModel):
    pdf_id: str = ""


@dataclass
class PdfArtifact:
    path: Path
    display_name: str


@dataclass
class CartolaExcelArtifact:
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
    selected_litigante: dict[str, Any] | None = None
    pdf_files: dict[str, PdfArtifact] = field(default_factory=dict)
    cached_pdf_files: dict[str, Path] = field(default_factory=dict)
    current_pdf_id: str = ""
    cartola_excel_files: dict[str, CartolaExcelArtifact] = field(default_factory=dict)
    current_cartola_excel_id: str = ""
    cartola_window_handle: str = ""
    cartola_popup_url: str = ""
    cartola_data: dict[str, Any] = field(default_factory=dict)

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        self.logs = self.logs[-300:]

    def clear_pdf(self, pdf_id: str | None = None) -> None:
        target_id = (pdf_id or self.current_pdf_id).strip()
        if not target_id:
            return

        artifact = self.pdf_files.pop(target_id, None)
        if artifact is not None:
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass

        if self.current_pdf_id == target_id:
            self.current_pdf_id = next(reversed(self.pdf_files), "")

    def clear_all_pdfs(self) -> None:
        for pdf_id in list(self.pdf_files.keys()):
            artifact = self.pdf_files.pop(pdf_id, None)
            if artifact is not None:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.current_pdf_id = ""

    def clear_cartola_excel(self, excel_id: str | None = None) -> None:
        target_id = (excel_id or self.current_cartola_excel_id).strip()
        if not target_id:
            return

        artifact = self.cartola_excel_files.pop(target_id, None)
        if artifact is not None:
            try:
                artifact.path.unlink(missing_ok=True)
            except OSError:
                pass

        if self.current_cartola_excel_id == target_id:
            self.current_cartola_excel_id = next(reversed(self.cartola_excel_files), "")

    def clear_all_cartola_excels(self) -> None:
        for excel_id in list(self.cartola_excel_files.keys()):
            artifact = self.cartola_excel_files.pop(excel_id, None)
            if artifact is not None:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.current_cartola_excel_id = ""

    def reset_cartola(self) -> None:
        self.clear_all_cartola_excels()
        self.cartola_window_handle = ""
        self.cartola_popup_url = ""
        self.cartola_data = {}

    def reset_session(self) -> None:
        self.clear_all_pdfs()
        self.reset_cartola()
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
        self.selected_litigante = None

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
        self.clear_all_pdfs()
        self.reset_cartola()
        self.cached_pdf_files.clear()


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


def _column_name(index: int) -> str:
    name = ""
    n = index
    while n >= 0:
        n, remainder = divmod(n, 26)
        name = chr(65 + remainder) + name
        n -= 1
    return name


def build_cartola_xlsx_bytes(rows: list[dict[str, Any]], title: str = "Cartola Banco Estado") -> bytes:
    headers = ["Fecha", "Tipo movimiento", "Monto"]
    sheet_rows: list[list[str]] = [headers]
    for row in rows:
        sheet_rows.append([str(row.get(header, "") or "") for header in headers])

    rows_xml: list[str] = []
    for row_index, row_values in enumerate(sheet_rows, start=1):
        cells_xml: list[str] = []
        for col_index, value in enumerate(row_values):
            cell_ref = f"{_column_name(col_index)}{row_index}"
            cells_xml.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(value)}</t></is></c>'
            )
        rows_xml.append(f'<row r="{row_index}">{"".join(cells_xml)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        '</worksheet>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{xml_escape(title)}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", root_rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def build_cartola_xls_bytes(
    rows: list[dict[str, Any]],
    title: str = "Banco Estado",
    account: str = "",
    rut: str = "",
    start_date: str = "",
    end_date: str = "",
) -> bytes:
    summary_parts = []
    if account:
        summary_parts.append(f"LAV: {account}")
    if rut:
        summary_parts.append(f"RUT: {rut}")
    period_text = ""
    if start_date or end_date:
        period_text = f"Movimientos desde {start_date or '-'} hasta {end_date or '-'}"

    row_html: list[str] = []
    for row in rows:
        fecha = xml_escape(str(row.get("Fecha", "") or ""))
        movimiento = xml_escape(str(row.get("Tipo movimiento", "") or ""))
        monto = xml_escape(str(row.get("Monto", "") or ""))
        row_html.append(
            "<tr>"
            f"<td>{fecha}</td>"
            f"<td>{movimiento}</td>"
            f"<td style=\"mso-number-format:'\\#\\,\\#\\#0'; text-align:right;\">{monto}</td>"
            "</tr>"
        )

    summary_html = "".join(f"<div>{xml_escape(part)}</div>" for part in summary_parts)
    if period_text:
        summary_html += f"<div>{xml_escape(period_text)}</div>"

    html = (
        "<html>"
        "<head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />'
        f"<title>{xml_escape(title)}</title>"
        "</head>"
        "<body>"
        f"<h3>{xml_escape(title)}</h3>"
        f"{summary_html}"
        "<table border='1'>"
        "<tr><th>Fecha</th><th>Tipo movimiento</th><th>Monto</th></tr>"
        f"{''.join(row_html)}"
        "</table>"
        "</body>"
        "</html>"
    )
    return html.encode("utf-8")


def is_cartola_debug_enabled() -> bool:
    return os.getenv("SITFA_DEBUG_CARTOLA", "").strip() == "1"


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def collect_cartola_debug_snapshot(driver: Any) -> dict[str, Any]:
    try:
        return driver.execute_script(
            """
            function frameInfo(frame, index) {
                var info = {
                    index: index + 1,
                    tag: frame.tagName || '',
                    id: frame.id || '',
                    name: frame.name || '',
                    src: frame.getAttribute('src') || '',
                    accessible: false,
                    title: '',
                    html: '',
                    html_len: 0,
                    error: ''
                };
                try {
                    var doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                    if (doc) {
                        var html = doc.documentElement ? doc.documentElement.outerHTML : (doc.body ? doc.body.outerHTML : '');
                        info.accessible = true;
                        info.title = doc.title || '';
                        info.html = html || '';
                        info.html_len = info.html.length;
                    }
                } catch (err) {
                    info.error = String(err || '');
                }
                return info;
            }

            var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(
                Array.prototype.slice.call(document.getElementsByTagName('iframe'))
            );
            var html = document.documentElement ? document.documentElement.outerHTML : (document.body ? document.body.outerHTML : '');
            return {
                url: String(window.location.href || ''),
                title: String(document.title || ''),
                ready_state: String(document.readyState || ''),
                html: html || '',
                html_len: (html || '').length,
                frame_count: frames.length,
                frames: frames.map(frameInfo)
            };
            """
        ) or {}
    except Exception as exc:
        return {
            "url": "",
            "title": "",
            "ready_state": "",
            "html": "",
            "html_len": 0,
            "frame_count": 0,
            "frames": [],
            "error": str(exc),
        }


def write_cartola_debug_dump(driver: Any, context: dict[str, Any] | None = None) -> Path:
    dump_dir = Path("debug_dumps") / f"cartola_{time.strftime('%Y%m%d_%H%M%S')}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    snapshot = collect_cartola_debug_snapshot(driver)
    manifest = {
        "created_at": datetime.now().isoformat(),
        "snapshot": _safe_json(snapshot),
        "context": _safe_json(context or {}),
    }

    try:
        driver.save_screenshot(str(dump_dir / "window.png"))
    except Exception as exc:
        manifest["screenshot_error"] = str(exc)

    try:
        main_html = str(snapshot.get("html") or driver.page_source or "")
    except Exception as exc:
        main_html = ""
        manifest["main_html_error"] = str(exc)
    (dump_dir / "main_document.html").write_text(main_html, encoding="utf-8", errors="replace")

    for frame in snapshot.get("frames") or []:
        frame_index = int(frame.get("index") or 0)
        frame_label = f"frame_{frame_index:02d}"
        frame_html = str(frame.get("html") or "")
        if frame_html:
            (dump_dir / f"{frame_label}.html").write_text(frame_html, encoding="utf-8", errors="replace")

    (dump_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
        errors="replace",
    )
    return dump_dir


def empty_cartola_data() -> dict[str, Any]:
    return {
        "open": False,
        "window_url": "",
        "title": "",
        "accounts": [],
        "selected_account": "",
        "start_date": "",
        "end_date": "",
        "movements": [],
        "can_consult": False,
        "can_export": False,
    }


def switch_to_cartola_window(state: WebState, driver: Any) -> str:
    handle = (state.cartola_window_handle or "").strip()
    if not handle:
        raise RuntimeError("La cartola no está abierta")

    handles = set(driver.window_handles)
    if handle not in handles:
        state.reset_cartola()
        raise RuntimeError("La ventana de cartola ya no está disponible")

    previous_handle = driver.current_window_handle
    driver.switch_to.window(handle)
    driver.switch_to.default_content()
    wait_for_ready(driver, timeout=8)
    return previous_handle


def close_existing_cartola_window(state: WebState, driver: Any, log_callback=None) -> None:
    handle = (state.cartola_window_handle or "").strip()
    if not handle:
        state.reset_cartola()
        return

    try:
        handles = set(driver.window_handles)
    except WebDriverException:
        state.reset_cartola()
        return

    if handle not in handles:
        state.reset_cartola()
        return

    try:
        current_handle = driver.current_window_handle
    except WebDriverException:
        current_handle = ""

    try:
        driver.switch_to.window(handle)
        driver.close()
        emit_log(log_callback, "Cartola: ventana anterior cerrada")
    except WebDriverException as exc:
        emit_log(log_callback, "Cartola: no se pudo cerrar la ventana anterior: " + str(exc))
    finally:
        state.reset_cartola()
        remaining_handles = []
        try:
            remaining_handles = list(driver.window_handles)
        except WebDriverException:
            remaining_handles = []
        target_handle = current_handle if current_handle in remaining_handles else (remaining_handles[-1] if remaining_handles else "")
        if target_handle:
            try:
                driver.switch_to.window(target_handle)
                driver.switch_to.default_content()
            except WebDriverException:
                pass


def read_cartola_payload(driver: Any) -> dict[str, Any]:
    form_data = _cartola_form_probe(driver)
    if not form_data.get("ok"):
        raise RuntimeError("No se pudo leer el formulario de cartola")

    html_source = driver.page_source or ""
    movements = extract_cartola_movements_from_html(html_source)
    return {
        "open": True,
        "window_url": str(form_data.get("url") or getattr(driver, "current_url", "") or ""),
        "title": str(form_data.get("title") or ""),
        "accounts": form_data.get("cuentas") or [],
        "selected_account": str(form_data.get("cuenta_actual") or ""),
        "start_date": str(form_data.get("fec_inicio") or ""),
        "end_date": str(form_data.get("fec_fin") or ""),
        "movements": movements,
        "can_consult": bool(form_data.get("tiene_consulta")),
        "can_export": bool(form_data.get("tiene_excel")),
    }


def apply_cartola_filters_and_consult(
    driver: Any,
    account: str,
    start_date: str,
    end_date: str,
    log_callback=None,
) -> dict[str, Any]:
    emit_log(
        log_callback,
        "Cartola: aplicando filtros "
        + f"cuenta={account or '(actual)'} "
        + f"desde={start_date or '(sin cambio)'} "
        + f"hasta={end_date or '(sin cambio)'}",
    )
    result = driver.execute_script(
        """
        var accountValue = String(arguments[0] || '');
        var startValue = String(arguments[1] || '');
        var endValue = String(arguments[2] || '');

        function applyAndSubmit(doc) {
            var form = doc.forms && doc.forms["TramitarPpalForm"];
            if (!form) {
                return null;
            }
            var select = form.elements["NRO_Cuenta"];
            var start = form.elements["FEC_Inicio"];
            var end = form.elements["FEC_Fin"];
            if (select && accountValue) {
                for (var i = 0; i < select.options.length; i++) {
                    if (String(select.options[i].value || '') === accountValue) {
                        select.selectedIndex = i;
                        break;
                    }
                }
            }
            if (start && startValue) {
                start.removeAttribute('readonly');
                start.value = startValue;
            }
            if (end && endValue) {
                end.removeAttribute('readonly');
                end.value = endValue;
            }

            var button = Array.prototype.slice.call(form.querySelectorAll('input[type="submit"], input[type="button"], button')).find(function (el) {
                return String(el.value || el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Cons. movimientos';
            });
            if (button) {
                if (typeof button.click === 'function') {
                    button.click();
                } else {
                    button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                }
                return { ok: true, mode: 'button' };
            }
            if (form && typeof form.submit === 'function') {
                form.submit();
                return { ok: true, mode: 'submit' };
            }
            return { ok: false, mode: 'not_found' };
        }

        var applied = applyAndSubmit(document);
        if (applied) {
            return applied;
        }

        var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(Array.prototype.slice.call(document.getElementsByTagName('iframe')));
        for (var i = 0; i < frames.length; i++) {
            try {
                var doc = frames[i].contentDocument || (frames[i].contentWindow && frames[i].contentWindow.document);
                var frameApplied = doc ? applyAndSubmit(doc) : null;
                if (frameApplied) {
                    frameApplied.scope = 'frame:' + (i + 1);
                    return frameApplied;
                }
            } catch (_err) {}
        }
        return { ok: false, mode: 'form_not_found' };
        """,
        account,
        start_date,
        end_date,
    ) or {}
    emit_log(log_callback, "Cartola: resultado consultar=" + json.dumps(_safe_json(result), ensure_ascii=False))
    if not result.get("ok"):
        raise RuntimeError("No se pudo ejecutar Cons. movimientos")

    time.sleep(0.8)
    wait_for_ready(driver, timeout=8)
    return read_cartola_payload(driver)


def _cartola_form_probe(driver: Any) -> dict[str, Any]:
    return driver.execute_script(
        """
        function probeInDoc(doc) {
            var form = doc.forms && doc.forms["TramitarPpalForm"];
            if (!form) {
                return null;
            }
            var select = form.elements["NRO_Cuenta"];
            var start = form.elements["FEC_Inicio"];
            var end = form.elements["FEC_Fin"];
            var predet = Array.prototype.slice.call(form.querySelectorAll('input[type="submit"], input[type="button"]')).find(function (el) {
                return String(el.value || '').trim() === 'Predet.';
            });
            var consult = Array.prototype.slice.call(form.querySelectorAll('input[type="submit"], input[type="button"]')).find(function (el) {
                return String(el.value || '').trim() === 'Cons. movimientos';
            });
            var excelBtn = Array.prototype.slice.call(form.querySelectorAll('img, input, button, a')).find(function (el) {
                var text = String(el.value || el.alt || el.title || el.getAttribute('name') || '');
                return /excel/i.test(text) || /showexcel/i.test(String(el.getAttribute('onclick') || ''));
            });
            return {
                ok: true,
                cuentas: select ? Array.prototype.slice.call(select.options).map(function (opt) {
                    return { value: opt.value, text: opt.text, selected: !!opt.selected };
                }) : [],
                cuenta_actual: select ? String(select.value || '') : '',
                fec_inicio: start ? String(start.value || '') : '',
                fec_fin: end ? String(end.value || '') : '',
                tiene_predet: !!predet,
                tiene_consulta: !!consult,
                tiene_excel: !!excelBtn
            };
        }

        function frameMeta(frame, index) {
            var meta = {
                index: index + 1,
                tag: frame.tagName || '',
                id: frame.id || '',
                name: frame.name || '',
                src: frame.getAttribute('src') || '',
                accessible: false,
                error: ''
            };
            try {
                var doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                meta.accessible = !!doc;
                if (doc) {
                    meta.title = String(doc.title || '');
                    meta.html_len = String((doc.documentElement && doc.documentElement.outerHTML) || '').length;
                }
            } catch (err) {
                meta.error = String(err || '');
            }
            return meta;
        }

        var result = probeInDoc(document);
        if (result) {
            result.scope = 'document';
            result.url = String(window.location.href || '');
            result.title = String(document.title || '');
            result.ready_state = String(document.readyState || '');
            result.frame_count = document.getElementsByTagName('frame').length + document.getElementsByTagName('iframe').length;
            result.html_len = String((document.documentElement && document.documentElement.outerHTML) || '').length;
            return result;
        }

        var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(Array.prototype.slice.call(document.getElementsByTagName('iframe')));
        for (var i = 0; i < frames.length; i++) {
            try {
                var doc = frames[i].contentDocument || (frames[i].contentWindow && frames[i].contentWindow.document);
                var probed = doc ? probeInDoc(doc) : null;
                if (probed) {
                    probed.scope = 'frame:' + (i + 1);
                    probed.url = String(window.location.href || '');
                    probed.title = String(document.title || '');
                    probed.ready_state = String(document.readyState || '');
                    probed.frame_count = frames.length;
                    probed.html_len = String((document.documentElement && document.documentElement.outerHTML) || '').length;
                    probed.frames = frames.map(frameMeta);
                    return probed;
                }
            } catch (_err) {}
        }

        return {
            ok: false,
            reason: "no_form",
            url: String(window.location.href || ''),
            title: String(document.title || ''),
            ready_state: String(document.readyState || ''),
            frame_count: frames.length,
            html_len: String((document.documentElement && document.documentElement.outerHTML) || '').length,
            frames: frames.map(frameMeta)
        };
        """
    ) or {}


def extract_cartola_movements_from_html(source: str) -> list[dict[str, str]]:
    wanted_headers = ["Fecha", "Tipo movimiento", "Monto"]
    best_rows: list[dict[str, str]] = []
    for table in extract_tables_from_html_source(source):
        rows = extract_rows_from_table(table, wanted_headers)
        if len(rows) > len(best_rows):
            best_rows = rows
    return best_rows


def _resolve_cartola_popup_url(driver: Any, popup_url: str, log_callback=None) -> str:
    resolved_url = str(popup_url or "").strip()
    if not resolved_url:
        return ""

    emit_log(log_callback, f"Cartola: popup_url original -> {resolved_url}")
    try:
        content_type, popup_html = fetch_session_resource_text(driver, resolved_url)
        emit_log(
            log_callback,
            f"Cartola: html popup capturado type={content_type or 'desconocido'} len={len(popup_html or '')}",
        )
        inner_src = extract_popup_frame_src(popup_html or "")
        if inner_src:
            resolved_url = urljoin(resolved_url, inner_src)
            emit_log(log_callback, f"Cartola: frame interno resuelto -> {resolved_url}")
        else:
            emit_log(log_callback, "Cartola: popup sin frame interno, se usara la URL original")
    except Exception as exc:
        emit_log(log_callback, f"Cartola: no se pudo resolver frame interno: {exc}")

    return resolved_url


def cartola_popup_select_account_and_consult(driver: Any, log_callback=None) -> dict[str, Any]:
    emit_log(log_callback, "Cartola: leyendo formulario del popup")
    form_data = {}
    for attempt in range(6):
        form_data = _cartola_form_probe(driver)
        if form_data.get("ok"):
            emit_log(
                log_callback,
                "Cartola: formulario encontrado "
                + f"scope={form_data.get('scope', '')} "
                + f"url={form_data.get('url', '')} "
                + f"title={form_data.get('title', '')!r} "
                + f"frames={form_data.get('frame_count', 0)}",
            )
            break
        emit_log(
            log_callback,
            "Cartola: formulario no encontrado, "
            + f"reintento {attempt + 1}/6 "
            + f"url={form_data.get('url', '')} "
            + f"title={form_data.get('title', '')!r} "
            + f"frames={form_data.get('frame_count', 0)} "
            + f"html_len={form_data.get('html_len', 0)}",
        )
        frame_summaries = []
        for frame in form_data.get("frames") or []:
            frame_summaries.append(
                f"{frame.get('index')}:{frame.get('tag') or 'frame'}"
                + f" name={frame.get('name') or ''}"
                + f" id={frame.get('id') or ''}"
                + f" src={frame.get('src') or ''}"
                + f" accessible={frame.get('accessible')}"
            )
        if frame_summaries:
            emit_log(log_callback, "Cartola: frames detectados -> " + " || ".join(frame_summaries[:10]))
        try:
            frame_count = len(driver.find_elements("tag name", "frame")) + len(driver.find_elements("tag name", "iframe"))
            emit_log(
                log_callback,
                f"Cartola: url_actual={getattr(driver, 'current_url', '')} frames={frame_count}",
            )
            emit_log(log_callback, f"Cartola: html_visible_len={len(driver.page_source or '')}")
        except Exception:
            pass
        time.sleep(0.8)

    if not form_data.get("ok"):
        dump_dir = None
        if is_cartola_debug_enabled():
            dump_dir = write_cartola_debug_dump(
                driver,
                context={
                    "stage": "form_not_found",
                    "form_probe": form_data,
                },
            )
            emit_log(log_callback, f"Cartola: dump de debug generado en {dump_dir}")
        detail = (
            "No se encontro el formulario de cartola"
            + f" | url={form_data.get('url', '')}"
            + f" | frames={form_data.get('frame_count', 0)}"
        )
        if dump_dir is not None:
            detail += f" | dump={dump_dir}"
        raise RuntimeError(detail)

    cuentas = form_data.get("cuentas") or []
    emit_log(log_callback, f"Cartola: cuentas disponibles={cuentas}")
    if not cuentas:
        dump_dir = None
        if is_cartola_debug_enabled():
            dump_dir = write_cartola_debug_dump(
                driver,
                context={
                    "stage": "no_accounts",
                    "form_probe": form_data,
                },
            )
            emit_log(log_callback, f"Cartola: dump de debug generado en {dump_dir}")
        detail = "No hay cuentas disponibles en la cartola"
        if dump_dir is not None:
            detail += f" | dump={dump_dir}"
        raise RuntimeError(detail)

    selected_account = cuentas[0].get("value", "")
    emit_log(log_callback, f"Cartola: seleccionando cuenta={selected_account}")
    driver.execute_script(
        """
        var accountValue = arguments[0];
        function applyToDoc(doc) {
            var form = doc.forms && doc.forms["TramitarPpalForm"];
            if (!form) {
                return false;
            }
            var select = form.elements["NRO_Cuenta"];
            if (!select) {
                return false;
            }
            for (var i = 0; i < select.options.length; i++) {
                if (String(select.options[i].value || '') === String(accountValue || '')) {
                    select.selectedIndex = i;
                    break;
                }
            }
            if (typeof select.onchange === 'function') {
                try { select.onchange(); } catch (_err) {}
            }
            return true;
        }
        if (!applyToDoc(document)) {
            var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(Array.prototype.slice.call(document.getElementsByTagName('iframe')));
            for (var i = 0; i < frames.length; i++) {
                try {
                    var doc = frames[i].contentDocument || (frames[i].contentWindow && frames[i].contentWindow.document);
                    if (doc && applyToDoc(doc)) {
                        break;
                    }
                } catch (_err) {}
            }
        }
        """,
        selected_account,
    )

    before_urls = snapshot_window_urls(driver)
    emit_log(log_callback, "Cartola: clic en Predet.")
    predet_clicked = driver.execute_script(
        """
        function clickButton(doc, label) {
            var form = doc.forms && doc.forms["TramitarPpalForm"];
            if (!form) {
                return false;
            }
            var button = Array.prototype.slice.call(form.querySelectorAll('input[type="submit"], input[type="button"]')).find(function (el) {
                return String(el.value || '').trim() === label;
            });
            if (!button) {
                return false;
            }
            if (typeof button.click === 'function') {
                button.click();
            } else {
                button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }
            return true;
        }
        if (!clickButton(document, 'Predet.')) {
            var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(Array.prototype.slice.call(document.getElementsByTagName('iframe')));
            for (var i = 0; i < frames.length; i++) {
                try {
                    var doc = frames[i].contentDocument || (frames[i].contentWindow && frames[i].contentWindow.document);
                    if (doc && clickButton(doc, 'Predet.')) {
                        break;
                    }
                } catch (_err) {}
            }
        }
        return false;
        """
    )
    emit_log(log_callback, f"Cartola: resultado clic Predet.={bool(predet_clicked)}")
    if not predet_clicked and is_cartola_debug_enabled():
        dump_dir = write_cartola_debug_dump(
            driver,
            context={
                "stage": "predet_click_failed",
                "form_probe": form_data,
            },
        )
        emit_log(log_callback, f"Cartola: dump de debug generado en {dump_dir}")
    wait_for_window_update(driver, before_urls, timeout=8.0, verbose=False)
    wait_for_window_content(driver, timeout=8)
    wait_for_ready(driver, timeout=8)

    before_urls = snapshot_window_urls(driver)
    emit_log(log_callback, "Cartola: clic en Cons. movimientos")
    consult_clicked = driver.execute_script(
        """
        function clickButton(doc, label) {
            var form = doc.forms && doc.forms["TramitarPpalForm"];
            if (!form) {
                return false;
            }
            var button = Array.prototype.slice.call(form.querySelectorAll('input[type="submit"], input[type="button"]')).find(function (el) {
                return String(el.value || '').trim() === label;
            });
            if (!button) {
                return false;
            }
            if (typeof button.click === 'function') {
                button.click();
            } else {
                button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }
            return true;
        }
        if (!clickButton(document, 'Cons. movimientos')) {
            var frames = Array.prototype.slice.call(document.getElementsByTagName('frame')).concat(Array.prototype.slice.call(document.getElementsByTagName('iframe')));
            for (var i = 0; i < frames.length; i++) {
                try {
                    var doc = frames[i].contentDocument || (frames[i].contentWindow && frames[i].contentWindow.document);
                    if (doc && clickButton(doc, 'Cons. movimientos')) {
                        break;
                    }
                } catch (_err) {}
            }
        }
        return false;
        """
    )
    emit_log(log_callback, f"Cartola: resultado clic Cons. movimientos={bool(consult_clicked)}")
    if not consult_clicked and is_cartola_debug_enabled():
        dump_dir = write_cartola_debug_dump(
            driver,
            context={
                "stage": "consult_click_failed",
                "form_probe": form_data,
            },
        )
        emit_log(log_callback, f"Cartola: dump de debug generado en {dump_dir}")
    wait_for_window_update(driver, before_urls, timeout=8.0, verbose=False)
    wait_for_window_content(driver, timeout=8)
    wait_for_ready(driver, timeout=8)

    html_source = driver.page_source or ""
    movements = extract_cartola_movements_from_html(html_source)
    emit_log(log_callback, f"Cartola: movimientos extraidos={len(movements)}")
    if not movements and is_cartola_debug_enabled():
        dump_dir = write_cartola_debug_dump(
            driver,
            context={
                "stage": "no_movements_after_consult",
                "form_probe": form_data,
                "selected_account": selected_account,
                "predet_clicked": bool(predet_clicked),
                "consult_clicked": bool(consult_clicked),
            },
        )
        emit_log(log_callback, f"Cartola: sin movimientos, dump de debug en {dump_dir}")
    return {
        "account": selected_account,
        "movements": movements,
        "html": html_source,
        "raw_form": form_data,
    }


def prepare_cartola_excel_artifact(state: WebState, movements: list[dict[str, Any]]) -> CartolaExcelArtifact:
    cache_dir = get_session_cache_dir(state.session_id)
    account = str((state.cartola_data or {}).get("selected_account") or "")
    start_date = str((state.cartola_data or {}).get("start_date") or "")
    end_date = str((state.cartola_data or {}).get("end_date") or "")
    rut = ""
    if state.selected_litigante:
        rut = str(state.selected_litigante.get("Rut/Pasaporte") or "")
    excel_bytes = build_cartola_xls_bytes(
        movements,
        title="Banco Estado",
        account=account,
        rut=rut,
        start_date=start_date,
        end_date=end_date,
    )
    excel_id = uuid.uuid4().hex
    active_dir = cache_dir / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_path = active_dir / f"cartola_{excel_id}.xls"
    active_path.write_bytes(excel_bytes)
    base_name = (state.username or "cartola").strip() or "cartola"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base_name)
    return CartolaExcelArtifact(path=active_path, display_name=f"{safe_name}_BancoEstado.xls")


def ensure_driver(state: WebState, headless: bool) -> None:
    if state.driver is not None and state.headless == headless:
        return

    if state.driver is not None:
        try:
            state.driver.quit()
        except WebDriverException:
            pass
        state.driver = None

    browser = os.getenv("SITFA_WEB_BROWSER", os.getenv("SITFA_BROWSER", "edge")).strip().lower() or "edge"
    state.append_log(f"Creando driver con {browser.upper()} " + ("headless" if headless else "visible"))
    state.driver = create_driver(initial_url=LOGIN_URL, browser=browser, headless=headless)
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
    default_headless = os.getenv("SITFA_WEB_HEADLESS", "1").strip() != "0"
    return HTML_PAGE.replace("__DEFAULT_HEADLESS__", "true" if default_headless else "false")


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
        "selected_litigante": state.selected_litigante,
        "cartola": state.cartola_data or empty_cartola_data(),
        "current_pdf_url": "/api/pdf/" + state.current_pdf_id if state.current_pdf_id else "",
    }


@app.post("/api/login")
def api_login(payload: LoginPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        try:
            ensure_driver(state, payload.headless)
            state.append_log("Solicitud de login enviada")
            login_to_sitfa(state.driver, payload.username, payload.password, log_callback=state.append_log)
            state.username = payload.username
            state.current_rit = ""
            state.results = {}
            state.detail_rit = ""
            state.detail_rows = []
            state.detail_status = ""
            state.selected_litigante = None
            state.reset_cartola()
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
        close_existing_cartola_window(state, driver, log_callback=state.append_log)
        state.append_log("Consulta enviada para " + payload.rit)
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
        state.selected_litigante = None
        state.reset_cartola()
        return {"ok": True, "rit": payload.rit, "results": results}


@app.post("/api/select-litigante")
def api_select_litigante(payload: SelectLitigantePayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        litigante = payload.litigante or {}
        close_existing_cartola_window(state, driver, log_callback=state.append_log)
        state.selected_litigante = litigante
        label = str(litigante.get("Sujeto") or litigante.get("Nombre o Razón Social") or litigante.get("Rut/Pasaporte") or "").strip()
        state.append_log("Litigante seleccionado: " + (label or "sin dato"))
        return {"ok": True, "selected_litigante": litigante}


@app.post("/api/cartola-bco-estado")
def api_cartola_bco_estado(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        close_existing_cartola_window(state, driver, log_callback=state.append_log)
        if not state.selected_litigante:
            raise HTTPException(status_code=400, detail="Primero selecciona un litigante en la pestaña Litigantes")

        try:
            selected_label = str(
                state.selected_litigante.get("Sujeto")
                or state.selected_litigante.get("Nombre o Razón Social")
                or state.selected_litigante.get("Rut/Pasaporte")
                or ""
            ).strip()
            state.append_log("Cartola: solicitud recibida para " + (selected_label or "sin dato"))
            state.status = "Buscando litigantes..."
            state.progress_status = "Resolviendo ventana Litigantes"
            state.progress_value = 25.0

            popup_url = resolve_litigantes_popup_url(driver, log_callback=state.append_log)
            if not popup_url:
                raise HTTPException(status_code=400, detail="No se pudo resolver la ventana de Litigantes")
            state.append_log("Cartola: popup de Litigantes resuelto -> " + popup_url)
            state.status = "Seleccionando litigante..."
            state.progress_status = "Seleccionando litigante"
            state.progress_value = 50.0

            popup_url, reason = open_cartola_bco_estado_popup_from_litigantes(
                driver,
                selected_litigante=state.selected_litigante,
                litigantes_popup_url=popup_url,
                log_callback=state.append_log,
            )
        except Exception as exc:
            state.append_log("Cartola: " + str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not popup_url:
            detail = "No se pudo abrir Cartola bco.estado"
            if reason:
                detail += " (" + reason + ")"
            raise HTTPException(status_code=400, detail=detail)

        popup_url = urljoin(driver.current_url, popup_url)
        popup_url = _resolve_cartola_popup_url(driver, popup_url, log_callback=state.append_log)
        state.append_log("Cartola: URL capturada -> " + popup_url)
        state.status = "Abriendo Cartola bco.estado..."
        state.progress_status = "Abriendo Cartola bco.estado"
        state.progress_value = 75.0

        try:
            original_handle = driver.current_window_handle
            before_handles = set(driver.window_handles)
            state.append_log(
                "Cartola: handles antes de abrir="
                + str(len(before_handles))
                + " url_actual="
                + str(getattr(driver, "current_url", ""))
            )
            driver.execute_script("window.open(arguments[0], '_blank');", popup_url)
            opened_handle = ""
            deadline = time.time() + 5.0
            while time.time() < deadline:
                current_handles = set(driver.window_handles)
                new_handles = list(current_handles - before_handles)
                if new_handles:
                    opened_handle = new_handles[0]
                    break
                time.sleep(0.1)
            try:
                state.append_log("Cartola: handles despues de abrir=" + str(len(driver.window_handles)))
            except WebDriverException:
                pass
            if opened_handle:
                try:
                    driver.switch_to.window(opened_handle)
                    state.cartola_window_handle = opened_handle
                    state.cartola_popup_url = popup_url
                    state.append_log("Cartola: ventana enfocada " + opened_handle)
                    state.append_log("Cartola: url enfocada -> " + str(getattr(driver, "current_url", "")))
                except WebDriverException as exc:
                    state.append_log("Cartola: no se pudo enfocar la ventana nueva: " + str(exc))
            else:
                state.append_log("Cartola: no apareció una ventana nueva, se navega en la pestaña actual")
                try:
                    driver.get(popup_url)
                    state.cartola_window_handle = driver.current_window_handle
                    state.cartola_popup_url = popup_url
                    state.append_log("Cartola: navegación directa realizada -> " + str(getattr(driver, "current_url", "")))
                except WebDriverException as exc:
                    raise HTTPException(status_code=400, detail="No se pudo cargar la Cartola bco.estado en la pestaña actual: " + str(exc)) from exc

            wait_for_ready(driver, timeout=8)
            try:
                frame_count = len(driver.find_elements("tag name", "frame")) + len(driver.find_elements("tag name", "iframe"))
                state.append_log("Cartola: frames visibles=" + str(frame_count))
            except WebDriverException:
                pass
            if is_cartola_debug_enabled():
                snapshot = collect_cartola_debug_snapshot(driver)
                state.append_log(
                    "Cartola: snapshot "
                    + f"title={snapshot.get('title', '')!r} "
                    + f"ready={snapshot.get('ready_state', '')} "
                    + f"html_len={snapshot.get('html_len', 0)}"
                )
            state.cartola_data = read_cartola_payload(driver)
            state.append_log(
                "Cartola: panel cargado "
                + f"cuentas={len(state.cartola_data.get('accounts') or [])} "
                + f"movimientos={len(state.cartola_data.get('movements') or [])}"
            )
            try:
                driver.switch_to.window(original_handle)
                driver.switch_to.default_content()
            except WebDriverException:
                pass
            state.append_log("Cartola: datos trasladados a la app web")
        except WebDriverException as exc:
            detail = "No se pudo abrir la ventana de Cartola bco.estado: " + str(exc)
            if is_cartola_debug_enabled():
                dump_dir = write_cartola_debug_dump(
                    driver,
                    context={
                        "stage": "endpoint_webdriver_exception",
                        "popup_url": popup_url,
                        "selected_litigante": state.selected_litigante,
                        "error": str(exc),
                    },
                )
                state.append_log(f"Cartola: dump de debug generado en {dump_dir}")
                detail += f" | dump={dump_dir}"
            raise HTTPException(status_code=400, detail=detail) from exc
        except Exception as exc:
            detail = str(exc)
            if is_cartola_debug_enabled():
                dump_dir = write_cartola_debug_dump(
                    driver,
                    context={
                        "stage": "endpoint_exception",
                        "popup_url": popup_url,
                        "selected_litigante": state.selected_litigante,
                        "error": str(exc),
                    },
                )
                state.append_log(f"Cartola: dump de debug generado en {dump_dir}")
                detail += f" | dump={dump_dir}"
            raise HTTPException(status_code=400, detail=detail) from exc

        state.status = "Cartola bco.estado abierta"
        state.progress_status = "Cartola abierta"
        state.progress_value = 100.0
        return {
            "ok": True,
            "url": popup_url,
            "selected_litigante": state.selected_litigante,
            "cartola": state.cartola_data or empty_cartola_data(),
        }


@app.post("/api/cartola-consult")
def api_cartola_consult(payload: CartolaConsultPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        previous_handle = ""
        try:
            previous_handle = switch_to_cartola_window(state, driver)
            state.status = "Consultando cartola..."
            state.progress_status = "Consultando cartola"
            state.progress_value = 80.0
            state.cartola_data = apply_cartola_filters_and_consult(
                driver,
                account=payload.account.strip(),
                start_date=payload.start_date.strip(),
                end_date=payload.end_date.strip(),
                log_callback=state.append_log,
            )
            state.append_log(
                "Cartola: consulta actualizada "
                + f"movimientos={len(state.cartola_data.get('movements') or [])}"
            )
        except Exception as exc:
            detail = str(exc)
            if is_cartola_debug_enabled():
                dump_dir = write_cartola_debug_dump(
                    driver,
                    context={
                        "stage": "cartola_consult",
                        "payload": payload.model_dump(),
                        "error": str(exc),
                    },
                )
                state.append_log(f"Cartola: dump de debug generado en {dump_dir}")
                detail += f" | dump={dump_dir}"
            raise HTTPException(status_code=400, detail=detail) from exc
        finally:
            if previous_handle:
                try:
                    driver.switch_to.window(previous_handle)
                    driver.switch_to.default_content()
                except WebDriverException:
                    pass

        state.status = "Cartola actualizada"
        state.progress_status = "Cartola actualizada"
        state.progress_value = 100.0
        return {"ok": True, "cartola": state.cartola_data}


@app.post("/api/cartola-export-xls")
def api_cartola_export_xls(request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        require_session(state)
        cartola = state.cartola_data or {}
        movements = cartola.get("movements") or []
        if not cartola.get("open"):
            raise HTTPException(status_code=400, detail="Primero abre la cartola")
        if not movements:
            raise HTTPException(status_code=400, detail="No hay movimientos para exportar")

        excel_artifact = prepare_cartola_excel_artifact(state, movements)
        excel_id = uuid.uuid4().hex
        state.cartola_excel_files[excel_id] = excel_artifact
        state.current_cartola_excel_id = excel_id
        export_url = "/api/cartola-excel/" + excel_id
        state.append_log("Cartola: archivo XLS generado -> " + export_url)
        return {"ok": True, "url": export_url, "name": excel_artifact.display_name}


@app.get("/api/cartola-excel/{excel_id}")
def api_cartola_excel(excel_id: str, request: Request, response: Response) -> Response:
    state = get_session_state(request, response)
    with state.lock:
        artifact = state.cartola_excel_files.get(excel_id)
        if artifact is None or not artifact.path.exists():
            raise HTTPException(status_code=404, detail="No existe el archivo Excel de cartola")

        content = artifact.path.read_bytes()
        filename = artifact.display_name or "Cartola_BancoEstado.xls"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        return Response(
            content=content,
            media_type="application/vnd.ms-excel",
            headers=headers,
        )


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

        try:
            artifact = prepare_pdf_artifact(state, driver, pdf_url, payload.prefix)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        custom_title = payload.pdf_title.strip()
        if custom_title:
            artifact.display_name = custom_title

        pdf_id = uuid.uuid4().hex
        state.pdf_files[pdf_id] = artifact
        state.current_pdf_id = pdf_id
        state.append_log("Documento abierto: " + artifact.display_name)
        return {"ok": True, "id": pdf_id, "url": "/api/pdf/" + pdf_id, "name": artifact.display_name}


@app.post("/api/close-pdf")
def api_close_pdf(payload: ClosePdfPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        target_id = (payload.pdf_id or state.current_pdf_id).strip()
        state.clear_pdf(target_id)
        if target_id:
            state.append_log("PDF cerrado: " + target_id)
        else:
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
    input, select, button {
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
    input, select {
      background: rgba(255, 255, 255, 0.88);
      color: var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.55);
    }
    input::placeholder { color: #8b98b6; }
    input:focus, select:focus {
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
      grid-template-columns: fit-content(760px) 360px;
      grid-template-areas:
        "main hero"
        "results results";
      gap: 16px;
      min-height: 0;
      justify-content: start;
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
      width: fit-content;
      max-width: 100%;
      justify-self: start;
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
    .pdf-sidecar-tabs {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 10px 12px 0;
      overflow: auto;
      max-height: 106px;
      background: linear-gradient(180deg, rgba(248,251,255,0.78), rgba(244,248,255,0.94));
      border-bottom: 1px solid rgba(148, 163, 184, 0.18);
    }
    .pdf-tab-wrap {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .pdf-tab {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      max-width: 100%;
      padding: 8px 10px;
      border-radius: 14px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      cursor: pointer;
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.06);
      font-weight: 700;
      text-align: left;
    }
    .pdf-tab.active {
      background: linear-gradient(135deg, var(--primary), var(--accent));
      color: #fff;
      border-color: transparent;
    }
    .pdf-tab-label {
      display: inline-block;
      max-width: 220px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pdf-tab-close {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 28px;
      height: 28px;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.28);
      background: rgba(255,255,255,0.92);
      color: #0f172a;
      cursor: pointer;
      flex: 0 0 auto;
      font-size: 18px;
      line-height: 1;
      font-weight: 900;
      box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08);
    }
    .pdf-tab-close:hover {
      background: rgba(248,250,252,1);
      transform: translateY(-1px);
    }
    .pdf-tab.active + .pdf-tab-close {
      border-color: rgba(255,255,255,0.24);
      background: rgba(255,255,255,0.14);
      color: #fff;
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
    tbody tr.selectable-row {
      cursor: pointer;
    }
    tbody tr.selected-row {
      background: rgba(45, 107, 255, 0.12);
      outline: 2px solid rgba(45, 107, 255, 0.28);
      outline-offset: -2px;
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
    .litigante-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin: 8px 0 14px;
    }
    .litigante-selected-info {
      flex: 1 1 320px;
      min-width: 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }
    .cartola-box {
      margin: 12px 0 18px;
      padding: 16px;
      border-radius: 18px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: linear-gradient(180deg, rgba(248,251,255,0.96), rgba(239,245,255,0.92));
      box-shadow: 0 16px 30px rgba(15, 23, 42, 0.08);
    }
    .cartola-header {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
    }
    .cartola-header h4 {
      margin: 0;
      font-size: 14px;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: #384158;
    }
    .cartola-meta {
      font-size: 12px;
      color: var(--muted);
      word-break: break-all;
    }
    .cartola-controls {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      align-items: end;
      margin-bottom: 12px;
    }
    .cartola-field {
      display: grid;
      gap: 6px;
      font-size: 12px;
      font-weight: 700;
      color: #384158;
    }
    .cartola-actions {
      display: flex;
      justify-content: flex-end;
      align-items: end;
      min-height: 100%;
    }
    .cartola-status {
      margin-bottom: 12px;
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
    }
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
      .cartola-controls {
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
            <div id="pdfSidecarTabs" class="pdf-sidecar-tabs"></div>
            <iframe id="pdfSidecarFrame" title="Visor PDF"></iframe>
          </aside>
        </div>
      </div>
    </div>
  </div>

  <script>
    var DEFAULT_HEADLESS = __DEFAULT_HEADLESS__;
    var state = { results: {}, logs: [], pdfTabs: [], activePdfTabId: "", pdfPanelWidth: 560, loggedIn: false, pendingRows: [], pendingStatus: "", selectedLitigante: null, cartola: { open: false, accounts: [], selected_account: "", start_date: "", end_date: "", movements: [] }, detail: { rit: "", rows: [], status: "", loading: false, error: "" } };
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

    function getActivePdfTab() {
      for (var i = 0; i < state.pdfTabs.length; i++) {
        if (state.pdfTabs[i].id === state.activePdfTabId) {
          return state.pdfTabs[i];
        }
      }
      return state.pdfTabs.length ? state.pdfTabs[state.pdfTabs.length - 1] : null;
    }

    function getPdfTitleForRow(rowData, sectionNameInner) {
      var candidates = [
        rowData && rowData.referencia,
        rowData && rowData.Referencia,
        rowData && rowData["Referencia"],
        rowData && rowData["referencia"],
        rowData && rowData.text,
        rowData && rowData.Titulo,
        rowData && rowData.titulo
      ];
      for (var i = 0; i < candidates.length; i++) {
        var value = candidates[i];
        if (value !== null && value !== undefined) {
          var text = String(value).trim();
          if (text) {
            return text;
          }
        }
      }
      return sectionNameInner ? String(sectionNameInner) : "PDF";
    }

    function rowSignature(rowData) {
      if (!rowData) {
        return "";
      }
      var keys = Object.keys(rowData).sort();
      var parts = [];
      for (var i = 0; i < keys.length; i++) {
        var key = keys[i];
        parts.push(key + ":" + String(rowData[key] === null || rowData[key] === undefined ? "" : rowData[key]));
      }
      return parts.join("||");
    }

    function formatLitiganteLabel(rowData) {
      if (!rowData) {
        return "";
      }
      var parts = [
        rowData["Sujeto"],
        rowData["Nombre o Razón Social"],
        rowData["Rut/Pasaporte"]
      ];
      var text = [];
      for (var i = 0; i < parts.length; i++) {
        var value = parts[i];
        if (value !== null && value !== undefined && String(value).trim()) {
          text.push(String(value).trim());
        }
      }
      return text.join(" | ");
    }

    function renderPdfTabs(tabsRoot) {
      if (!tabsRoot) {
        return;
      }
      tabsRoot.innerHTML = "";
      for (var i = 0; i < state.pdfTabs.length; i++) {
        (function (tab) {
          var wrap = document.createElement("div");
          wrap.className = "pdf-tab-wrap";

          var button = document.createElement("button");
          button.type = "button";
          button.className = "pdf-tab" + (tab.id === state.activePdfTabId ? " active" : "");
          button.title = tab.title || "PDF";

          var label = document.createElement("span");
          label.className = "pdf-tab-label";
          label.textContent = tab.title || "PDF";

          var close = document.createElement("button");
          close.type = "button";
          close.className = "pdf-tab-close";
          close.title = "Cerrar pestaña";
          close.textContent = "×";
          close.onclick = function (event) {
            event.stopPropagation();
            closePdfTab(tab.id);
          };

          button.appendChild(label);
          button.onclick = function () {
            activatePdfTab(tab.id);
          };
          wrap.appendChild(button);
          wrap.appendChild(close);
          tabsRoot.appendChild(wrap);
        })(state.pdfTabs[i]);
      }
    }

    function activatePdfTab(tabId) {
      state.activePdfTabId = tabId || "";
      syncPdfSidecar();
    }

    function syncPdfSidecar() {
      var resultsCard = document.querySelector(".results");
      var resultsShell = document.querySelector(".results-shell");
      var resizer = document.getElementById("pdfResizer");
      var sidecar = document.getElementById("pdfSidecar");
      var tabsRoot = document.getElementById("pdfSidecarTabs");
      var frame = document.getElementById("pdfSidecarFrame");
      var title = document.getElementById("pdfSidecarTitle");
      var activeTab = getActivePdfTab();
      var hasPdf = !!activeTab;

      if (resultsCard) {
        resultsCard.classList.toggle("has-pdf", hasPdf);
      }
      if (resultsShell) {
        resultsShell.style.setProperty("--pdf-sidecar-width", clampPdfWidth(state.pdfPanelWidth, resultsShell.clientWidth) + "px");
      }
      if (!sidecar || !frame) {
        return;
      }

      renderPdfTabs(tabsRoot);

      if (!hasPdf) {
        sidecar.classList.add("hidden");
        if (resizer) {
          resizer.classList.remove("active");
          resizer.classList.add("hidden");
        }
        frame.src = "about:blank";
        lastRenderedPdfUrl = "";
        state.activePdfTabId = "";
        state.pdfUrl = "";
        state.pdfTitle = "";
        return;
      }

      sidecar.classList.remove("hidden");
      if (resizer) {
        resizer.classList.remove("hidden");
      }
      if (title) {
        title.textContent = activeTab.title || "PDF";
      }
      state.pdfUrl = activeTab.url || "";
      state.pdfTitle = activeTab.title || "PDF";
      if (activeTab.url !== lastRenderedPdfUrl) {
        frame.src = "about:blank";
        frame.src = activeTab.url + "?v=" + new Date().getTime();
        lastRenderedPdfUrl = activeTab.url;
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

    function openPdfSidecar(url, title, pdfId) {
      if (!url) {
        return;
      }
      var tabId = pdfId || ("local-" + Date.now() + "-" + Math.random().toString(16).slice(2));
      var existing = null;
      for (var i = 0; i < state.pdfTabs.length; i++) {
        if (state.pdfTabs[i].id === tabId) {
          existing = state.pdfTabs[i];
          break;
        }
      }
      if (existing) {
        existing.url = url;
        existing.title = title || existing.title || "PDF";
      } else {
        state.pdfTabs.push({
          id: tabId,
          url: url,
          title: title || "PDF"
        });
      }
      state.activePdfTabId = tabId;
      syncPdfSidecar();
    }

    function closePdfTab(tabId) {
      var targetId = tabId || state.activePdfTabId;
      if (!targetId) {
        return;
      }
      xhrRequest("POST", "/api/close-pdf", { pdf_id: targetId }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudo cerrar el PDF");
          return;
        }
        var nextTabs = [];
        var nextActive = "";
        for (var i = 0; i < state.pdfTabs.length; i++) {
          if (state.pdfTabs[i].id !== targetId) {
            nextTabs.push(state.pdfTabs[i]);
          }
        }
        state.pdfTabs = nextTabs;
        if (state.activePdfTabId === targetId) {
          nextActive = state.pdfTabs.length ? state.pdfTabs[state.pdfTabs.length - 1].id : "";
        } else {
          nextActive = state.activePdfTabId;
        }
        state.activePdfTabId = nextActive;
        syncPdfSidecar();
        refreshStatus();
      });
    }

    function closePdfSidecar() {
      closePdfTab(state.activePdfTabId);
    }

    function buildDataTable(rows, sectionName, options) {
      options = options || {};
      var enableRitDrilldown = !!options.enableRitDrilldown;
      var onRitClick = options.onRitClick || null;
      var onRowSelect = options.onRowSelect || null;
      var selectedRowSignature = options.selectedRowSignature || "";
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
          if (sectionNameInner === "litigantes" && typeof onRowSelect === "function") {
            tr.className = "selectable-row";
            tr.title = "Seleccionar litigante";
            tr.style.cursor = "pointer";
            var currentSignature = rowSignature(rowData);
            if (selectedRowSignature && currentSignature === selectedRowSignature) {
              tr.classList.add("selected-row");
            }
            tr.onclick = function () {
              onRowSelect(rowData);
            };
          }
          tr.ondblclick = function () {
            if (!rowData.pdf_url) {
              return;
            }
            var pdfTitle = getPdfTitleForRow(rowData, sectionNameInner);
            xhrRequest("POST", "/api/open-pdf", { pdf_url: rowData.pdf_url, prefix: sectionNameInner, pdf_title: pdfTitle }, function (status, data) {
              if (status >= 200 && status < 300 && data.ok) {
                openPdfSidecar(data.url, data.name || pdfTitle || "PDF", data.id);
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
                  var pdfTitle = getPdfTitleForRow(rowData, sectionNameInner);
                  xhrRequest("POST", "/api/open-pdf", { pdf_url: rowData.pdf_url, prefix: sectionNameInner, pdf_title: pdfTitle }, function (status, data) {
                    if (status >= 200 && status < 300 && data.ok) {
                      openPdfSidecar(data.url, data.name || pdfTitle || "PDF", data.id);
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

    function submitCartolaConsult() {
      if (!state.cartola || !state.cartola.open) {
        return;
      }
      var accountEl = document.getElementById("cartolaAccount");
      var startEl = document.getElementById("cartolaStartDate");
      var endEl = document.getElementById("cartolaEndDate");
      var payload = {
        account: accountEl ? String(accountEl.value || "") : "",
        start_date: startEl ? String(startEl.value || "") : "",
        end_date: endEl ? String(endEl.value || "") : ""
      };
      setProgressState(40, "Consultando cartola...");
      xhrRequest("POST", "/api/cartola-consult", payload, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudo consultar la cartola");
          return;
        }
        state.cartola = (data && data.cartola) || state.cartola;
        setProgressState(100, "Cartola actualizada");
        renderResults(state.results);
        refreshStatus();
      });
    }

    function exportCartolaXls() {
      if (!state.cartola || !state.cartola.open) {
        return;
      }
      xhrRequest("POST", "/api/cartola-export-xls", null, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudo exportar la cartola");
          return;
        }
        if (data && data.url) {
          var downloadLink = document.createElement("a");
          downloadLink.href = data.url;
          downloadLink.download = (data && data.name) || "Cartola_BancoEstado.xls";
          downloadLink.style.display = "none";
          document.body.appendChild(downloadLink);
          downloadLink.click();
          window.setTimeout(function () {
            if (downloadLink.parentNode) {
              downloadLink.parentNode.removeChild(downloadLink);
            }
          }, 1000);
        }
        setProgressState(100, "Cartola exportada");
        refreshStatus();
      });
    }

    function renderCartolaPanel(panel) {
      if (!state.cartola || !state.cartola.open) {
        return;
      }

      var cartola = state.cartola;
      var box = document.createElement("div");
      box.className = "cartola-box";

      var header = document.createElement("div");
      header.className = "cartola-header";

      var title = document.createElement("h4");
      title.textContent = "Cartola Banco Estado";
      header.appendChild(title);

      var meta = document.createElement("div");
      meta.className = "cartola-meta";
      meta.textContent = cartola.selected_account
        ? "Cuenta seleccionada: " + String(cartola.selected_account)
        : "Cartola disponible";
      header.appendChild(meta);
      box.appendChild(header);

      var controls = document.createElement("div");
      controls.className = "cartola-controls";

      var accountField = document.createElement("label");
      accountField.className = "cartola-field";
      accountField.textContent = "Nro. Cuenta";
      var accountSelect = document.createElement("select");
      accountSelect.id = "cartolaAccount";
      var accounts = cartola.accounts || [];
      for (var i = 0; i < accounts.length; i++) {
        var option = document.createElement("option");
        option.value = accounts[i].value || "";
        option.textContent = accounts[i].text || accounts[i].value || "";
        option.selected = (accounts[i].value || "") === (cartola.selected_account || "");
        accountSelect.appendChild(option);
      }
      accountField.appendChild(accountSelect);
      controls.appendChild(accountField);

      var startField = document.createElement("label");
      startField.className = "cartola-field";
      startField.textContent = "Desde";
      var startInput = document.createElement("input");
      startInput.id = "cartolaStartDate";
      startInput.type = "text";
      startInput.placeholder = "dd/mm/aaaa";
      startInput.value = cartola.start_date || "";
      startField.appendChild(startInput);
      controls.appendChild(startField);

      var endField = document.createElement("label");
      endField.className = "cartola-field";
      endField.textContent = "Hasta";
      var endInput = document.createElement("input");
      endInput.id = "cartolaEndDate";
      endInput.type = "text";
      endInput.placeholder = "dd/mm/aaaa";
      endInput.value = cartola.end_date || "";
      endField.appendChild(endInput);
      controls.appendChild(endField);

      var actions = document.createElement("div");
      actions.className = "cartola-actions";
      var consultButton = document.createElement("button");
      consultButton.type = "button";
      consultButton.innerHTML = buttonContent("Consultar cartola", "search");
      consultButton.onclick = submitCartolaConsult;
      actions.appendChild(consultButton);
      var exportButton = document.createElement("button");
      exportButton.type = "button";
      exportButton.className = "secondary";
      exportButton.innerHTML = buttonContent("Descargar XLS", "external");
      exportButton.onclick = exportCartolaXls;
      exportButton.disabled = !(cartola.movements && cartola.movements.length);
      actions.appendChild(exportButton);
      controls.appendChild(actions);
      box.appendChild(controls);

      var status = document.createElement("div");
      status.className = "cartola-status";
      status.textContent = "Movimientos cargados: " + String((cartola.movements || []).length);
      box.appendChild(status);

      if (cartola.movements && cartola.movements.length) {
        box.appendChild(buildDataTable(cartola.movements, "cartola", {}));
      } else {
        var empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Aún no hay movimientos cargados para la cartola.";
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

        if (section === "litigantes") {
          var litiganteActions = document.createElement("div");
          litiganteActions.className = "litigante-actions";
          if (state.selectedLitigante) {
            var selectedInfo = document.createElement("div");
            selectedInfo.className = "litigante-selected-info";
            selectedInfo.textContent = "Seleccionado: " + formatLitiganteLabel(state.selectedLitigante);
            litiganteActions.appendChild(selectedInfo);

            var cartolaButton = document.createElement("button");
            cartolaButton.type = "button";
            cartolaButton.innerHTML = buttonContent("Cartola bco.estado", "external");
            cartolaButton.onclick = openSelectedLitiganteCartola;
            litiganteActions.appendChild(cartolaButton);
          } else {
            var help = document.createElement("div");
            help.className = "muted";
            help.textContent = "Selecciona un sujeto para habilitar Cartola bco.estado.";
            litiganteActions.appendChild(help);
          }
          panel.appendChild(litiganteActions);
          renderCartolaPanel(panel);
        }

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
          onRitClick: section === "pendientes" ? consultPendingRit : openConsLitHistory,
          onRowSelect: section === "litigantes" ? selectLitigante : null,
          selectedRowSignature: section === "litigantes" && state.selectedLitigante ? rowSignature(state.selectedLitigante) : ""
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
      consult();
    }

    function selectLitigante(rowData) {
      if (!rowData) {
        return;
      }
      setProgressState(10, "Marcando litigante seleccionado...");
      xhrRequest("POST", "/api/select-litigante", { litigante: rowData }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudo seleccionar el litigante");
          return;
        }
        state.selectedLitigante = (data && data.selected_litigante) || rowData;
        setProgressState(100, "Litigante seleccionado");
        renderResults(state.results);
        refreshStatus();
      });
    }

    function openSelectedLitiganteCartola() {
      if (!state.selectedLitigante) {
        return;
      }
      setProgressState(15, "Abriendo Cartola bco.estado...");
      xhrRequest("POST", "/api/cartola-bco-estado", null, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "No se pudo abrir Cartola bco.estado");
          return;
        }
        if (data && data.selected_litigante) {
          state.selectedLitigante = data.selected_litigante;
        }
        if (data && data.cartola) {
          state.cartola = data.cartola;
        }
        setProgressState(100, "Cartola cargada en la app web");
        renderResults(state.results);
        refreshStatus();
      });
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
        state.progressValue = Number(data.progress_value || 0);
        state.progressStatus = data.progress_status || "";
        state.detail = {
          rit: data.detail_rit || "",
          rows: data.detail_rows || [],
          status: data.detail_status || "",
          loading: false,
          error: ""
        };
        state.selectedLitigante = data.selected_litigante || null;
        state.cartola = data.cartola || { open: false, accounts: [], selected_account: "", start_date: "", end_date: "", movements: [] };
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
          state.pdfTabs = [];
          state.activePdfTabId = "";
          state.pdfUrl = "";
          state.pdfTitle = "";
          state.selectedLitigante = null;
          state.cartola = { open: false, accounts: [], selected_account: "", start_date: "", end_date: "", movements: [] };
          syncPdfSidecar();
          setText("loginStatus", "Sin sesion activa. Ingresa tus credenciales para continuar.");
        }

        var resultsKey = buildResultsRenderKey(state.results)
          + "|cartola:" + String((state.cartola && state.cartola.open) ? 1 : 0)
          + ":" + String((state.cartola && state.cartola.selected_account) || "")
          + ":" + String((state.cartola && state.cartola.start_date) || "")
          + ":" + String((state.cartola && state.cartola.end_date) || "")
          + ":" + String(((state.cartola && state.cartola.movements) || []).length);
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
        headless: DEFAULT_HEADLESS
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
        state.pdfTabs = [];
        state.activePdfTabId = "";
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
      setProgressState(5, "Preparando consulta...");
      startStatusPolling();
      xhrRequest("POST", "/api/consult", { rit: rit }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setProgressState(0, (data && data.detail) || "Error en consulta");
          stopStatusPolling();
          return;
        }
        setProgressState(100, "Consulta terminada");
        activeTab = "historia";
        stopStatusPolling();
        refreshStatus();
      });
    }

    function logout() {
      xhrRequest("POST", "/api/logout", null, function () {
        setText("loginStatus", "Sesion cerrada");
        state.pdfTabs = [];
        state.activePdfTabId = "";
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
      var activeTab = getActivePdfTab();
      if (activeTab && activeTab.url) {
        window.open(activeTab.url, "_blank", "noopener,noreferrer");
      }
    };
    document.getElementById("pdfResizer").addEventListener("pointerdown", beginPdfResize);
    document.addEventListener("pointermove", movePdfResize);
    document.addEventListener("pointerup", endPdfResize);
    document.addEventListener("pointercancel", endPdfResize);
    window.addEventListener("resize", function () {
      if (state.pdfTabs && state.pdfTabs.length) {
        syncPdfSidecar();
      }
    });

    refreshStatus();
  </script>
</body>
</html>
"""
