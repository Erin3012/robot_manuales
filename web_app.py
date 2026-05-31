from __future__ import annotations

import atexit
import json
import threading
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from selenium.common.exceptions import WebDriverException

from main import LOGIN_URL, consult_case, create_driver, download_pdf_with_session, login_to_sitfa


app = FastAPI(title="SITFA Web", version="0.1.0")


class LoginPayload(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    headless: bool = True


class ConsultPayload(BaseModel):
    rit: str = Field(min_length=1)


class OpenPdfPayload(BaseModel):
    pdf_url: str = Field(min_length=1)
    prefix: str = "pdf"


@dataclass
class WebState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    driver: Any | None = None
    username: str = ""
    headless: bool = True
    logged_in_at: str = ""
    current_rit: str = ""
    status: str = "Listo"
    logs: list[str] = field(default_factory=list)
    results: dict[str, list[dict]] = field(default_factory=dict)
    pdf_files: dict[str, Path] = field(default_factory=dict)
    current_pdf_id: str = ""

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        self.logs = self.logs[-500:]

    def clear_pdf(self) -> None:
        if self.current_pdf_id:
            path = self.pdf_files.pop(self.current_pdf_id, None)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.current_pdf_id = ""

    def shutdown(self) -> None:
        with self.lock:
            self.clear_pdf()
            if self.driver is not None:
                try:
                    self.driver.quit()
                except WebDriverException:
                    pass
                self.driver = None


STATE = WebState()


def ensure_driver(headless: bool) -> None:
    if STATE.driver is not None and STATE.headless == headless:
        return

    if STATE.driver is not None:
        try:
            STATE.driver.quit()
        except WebDriverException:
            pass
        STATE.driver = None

    STATE.append_log(f"Creando driver en modo {'headless' if headless else 'visible'} con Edge")
    STATE.driver = create_driver(initial_url=LOGIN_URL, browser="edge", headless=headless)
    STATE.headless = headless


def require_driver() -> Any:
    if STATE.driver is None:
        raise HTTPException(status_code=400, detail="No hay una sesion activa")
    return STATE.driver


def render_rows(section_name: str, rows: list[dict]) -> str:
    if not rows:
        return f"<p class='empty'>Sin datos para {section_name}.</p>"

    headers = [key for key in rows[0].keys() if key != "pdf_url"]
    header_html = "".join(f"<th>{escape_html(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = []
        for header in headers:
            value = row.get(header, "")
            cells.append(f"<td>{escape_html(str(value))}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(header_html, "".join(body_rows))


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PAGE


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "logged_in": STATE.driver is not None,
        "username": STATE.username,
        "current_rit": STATE.current_rit,
        "status": STATE.status,
        "headless": STATE.headless,
        "logs": STATE.logs[-200:],
        "results": STATE.results,
        "current_pdf_url": f"/api/pdf/{STATE.current_pdf_id}" if STATE.current_pdf_id else "",
    }


@app.post("/api/login")
def api_login(payload: LoginPayload) -> dict[str, Any]:
    with STATE.lock:
        ensure_driver(payload.headless)
        STATE.append_log("Solicitud de login enviada")
        login_to_sitfa(STATE.driver, payload.username, payload.password, log_callback=STATE.append_log)
        STATE.username = payload.username
        STATE.logged_in_at = datetime.now().isoformat(timespec="seconds")
        STATE.status = "Sesion iniciada"
        return {"ok": True, "username": STATE.username, "headless": STATE.headless}


@app.post("/api/consult")
def api_consult(payload: ConsultPayload) -> dict[str, Any]:
    with STATE.lock:
        driver = require_driver()
        STATE.append_log(f"Solicitud de consulta enviada para {payload.rit}")
        STATE.clear_pdf()
        results = consult_case(
            driver,
            payload.rit,
            log_callback=STATE.append_log,
            section_callback=lambda section, rows: STATE.results.__setitem__(section, rows),
        )
        STATE.results = results
        STATE.current_rit = payload.rit
        STATE.status = f"Consulta terminada para {payload.rit}"
        return {"ok": True, "rit": payload.rit, "results": results}


@app.post("/api/open-pdf")
def api_open_pdf(payload: OpenPdfPayload) -> dict[str, Any]:
    with STATE.lock:
        driver = require_driver()
        pdf_url = payload.pdf_url.strip()
        if not pdf_url:
            raise HTTPException(status_code=400, detail="La fila no tiene PDF asociado")

        STATE.clear_pdf()
        pdf_id = uuid.uuid4().hex
        output_path = Path(tempfile.gettempdir()) / f"sitfa_{payload.prefix}_{pdf_id}.pdf"
        downloaded_path = download_pdf_with_session(driver, pdf_url, output_path)
        STATE.pdf_files[pdf_id] = downloaded_path
        STATE.current_pdf_id = pdf_id
        STATE.append_log(f"PDF abierto: {downloaded_path.name}")
        return {"ok": True, "pdf_id": pdf_id, "url": f"/api/pdf/{pdf_id}", "name": downloaded_path.name}


@app.get("/api/pdf/{pdf_id}")
def api_pdf(pdf_id: str):
    path = STATE.pdf_files.get(pdf_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="PDF no encontrado")
    return FileResponse(str(path), media_type="application/pdf", filename=path.name)


@app.post("/api/logout")
def api_logout() -> dict[str, Any]:
    with STATE.lock:
        STATE.shutdown()
        STATE.username = ""
        STATE.current_rit = ""
        STATE.status = "Listo"
        STATE.logs.clear()
        STATE.results = {}
        return {"ok": True}


@app.on_event("shutdown")
def on_shutdown() -> None:
    STATE.shutdown()


HTML_PAGE = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SITFA Web</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --panel: #ffffff;
      --text: #182033;
      --muted: #5c6b82;
      --accent: #184e9e;
      --accent-2: #2a72d6;
      --border: #d9e1ee;
      --shadow: 0 16px 36px rgba(14, 25, 45, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Segoe UI, Arial, sans-serif;
      color: var(--text);
      background: linear-gradient(180deg, #eef3fb, #f8fafc 42%, #f5f7fb 100%);
    }
    .page {
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 16px;
      padding: 16px;
      min-height: 100vh;
    }
    .hero, .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }
    .hero {
      padding: 18px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    .hero h1 { margin: 0; font-size: 20px; }
    .hero p { margin: 4px 0 0; color: var(--muted); }
    .grid {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      min-height: 0;
    }
    .card {
      padding: 16px;
      min-width: 0;
      min-height: 0;
    }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; font-size: 13px; color: var(--muted); }
    input, button {
      font: inherit;
      border-radius: 10px;
      border: 1px solid var(--border);
      padding: 10px 12px;
    }
    button {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button:hover { filter: brightness(0.98); }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .status {
      padding: 10px 12px;
      background: #f0f5ff;
      border: 1px solid #d7e3fb;
      border-radius: 10px;
      color: #173a73;
      font-size: 13px;
    }
    .results {
      display: grid;
      grid-template-columns: 1fr 380px;
      gap: 16px;
      min-height: 0;
    }
    .tabs, .viewer, .logs {
      min-height: 0;
      overflow: auto;
    }
    .section {
      margin-bottom: 16px;
    }
    .section h3 {
      margin: 0 0 8px;
      font-size: 15px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      font-size: 12px;
    }
    th, td {
      border: 1px solid var(--border);
      padding: 8px 10px;
      vertical-align: top;
      text-align: left;
    }
    th {
      background: #edf3fd;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    .tabs button {
      margin-right: 8px;
      margin-bottom: 8px;
    }
    .logs {
      white-space: pre-wrap;
      font-family: Consolas, monospace;
      font-size: 12px;
      background: #0f172a;
      color: #d9e5ff;
      padding: 12px;
      border-radius: 12px;
      max-height: 220px;
      overflow: auto;
    }
    iframe {
      width: 100%;
      height: 100%;
      min-height: 720px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #fff;
    }
    .empty {
      color: var(--muted);
      padding: 12px 0;
    }
    .small { font-size: 12px; color: var(--muted); }
    .pdf-link { color: var(--accent-2); text-decoration: none; font-weight: 600; }
  </style>
</head>
<body>
  <div class="page">
    <div class="hero">
      <div>
        <h1>SITFA Web</h1>
        <p>Backend Python reutilizando el motor actual. Consultas, tablas y visor PDF en una sola pantalla.</p>
      </div>
      <div class="row">
        <button class="secondary" id="refreshBtn">Actualizar</button>
        <button class="secondary" id="logoutBtn">Cerrar sesion</button>
      </div>
    </div>

    <div class="grid">
      <div class="card stack">
        <div>
          <h3>Login</h3>
          <div class="stack">
            <label>Usuario <input id="username" autocomplete="username" /></label>
            <label>Clave <input id="password" type="password" autocomplete="current-password" /></label>
            <label><span>Modo no visible por defecto</span>
              <input id="headless" type="checkbox" checked />
            </label>
            <button id="loginBtn">Ingresar</button>
          </div>
        </div>
        <div>
          <h3>Consulta</h3>
          <div class="stack">
            <label>RIT <input id="rit" placeholder="Z-1-2026" /></label>
            <button id="consultBtn">Consultar</button>
          </div>
        </div>
        <div>
          <h3>Estado</h3>
          <div id="status" class="status">Listo.</div>
          <div class="small" id="meta"></div>
        </div>
        <div>
          <h3>Logs</h3>
          <div id="logs" class="logs"></div>
        </div>
      </div>

      <div class="card results">
        <div class="tabs" id="results"></div>
        <div class="viewer">
          <iframe id="pdfFrame" title="Visor PDF"></iframe>
        </div>
      </div>
    </div>
  </div>

  <script>
    const state = { results: {}, pdfUrl: "", logs: [] };
    const sections = ["historia", "liquidacion", "escritos", "litigantes", "cons_lit"];
    const sectionTitles = {
      historia: "Historia",
      liquidacion: "Liquidacion",
      escritos: "Esc. por Resolv.",
      litigantes: "Litigantes",
      cons_lit: "Cons. Lit."
    };

    function esc(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function setStatus(text) {
      document.getElementById("status").textContent = text;
    }

    function setMeta(text) {
      document.getElementById("meta").textContent = text;
    }

    function renderLogs(lines) {
      document.getElementById("logs").textContent = (lines || []).join("\n");
    }

    function renderResults(results) {
      const root = document.getElementById("results");
      root.innerHTML = "";
      sections.forEach(section => {
        const rows = results?.[section] || [];
        const sectionEl = document.createElement("div");
        sectionEl.className = "section";
        const title = document.createElement("h3");
        title.textContent = `${sectionTitles[section]} (${rows.length})`;
        sectionEl.appendChild(title);

        if (!rows.length) {
          const empty = document.createElement("div");
          empty.className = "empty";
          empty.textContent = "Sin datos.";
          sectionEl.appendChild(empty);
          root.appendChild(sectionEl);
          return;
        }

        const table = document.createElement("table");
        const headers = Object.keys(rows[0]);
        const thead = document.createElement("thead");
        thead.innerHTML = "<tr>" + headers.map(h => `<th>${esc(h)}</th>`).join("") + "</tr>";
        const tbody = document.createElement("tbody");
        rows.forEach(row => {
          const tr = document.createElement("tr");
          tr.addEventListener("dblclick", async () => {
            if (!row.pdf_url) {
              return;
            }
            const resp = await fetch("/api/open-pdf", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({ pdf_url: row.pdf_url, prefix: section })
            });
            const data = await resp.json();
            if (data.ok) {
              document.getElementById("pdfFrame").src = data.url;
              await refreshStatus();
            }
          });
          headers.forEach(header => {
            const td = document.createElement("td");
            td.textContent = row[header] ?? "";
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(thead);
        table.appendChild(tbody);
        sectionEl.appendChild(table);
        root.appendChild(sectionEl);
      });
    }

    async function refreshStatus() {
      const resp = await fetch("/api/status");
      const data = await resp.json();
      state.results = data.results || {};
      state.logs = data.logs || [];
      state.pdfUrl = data.current_pdf_url || "";
      setStatus(data.status || "Listo");
      setMeta(`${data.logged_in ? "Sesion activa" : "Sin sesion"}${data.username ? " | " + data.username : ""}${data.current_rit ? " | " + data.current_rit : ""}${data.headless ? " | headless" : ""}`);
      renderLogs(state.logs);
      renderResults(state.results);
      if (state.pdfUrl) {
        document.getElementById("pdfFrame").src = state.pdfUrl;
      }
    }

    async function login() {
      const payload = {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
        headless: document.getElementById("headless").checked
      };
      setStatus("Iniciando sesion...");
      const resp = await fetch("/api/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const data = await resp.json();
      if (!resp.ok) {
        setStatus(data.detail || "Error de login");
        return;
      }
      setStatus("Sesion iniciada");
      await refreshStatus();
    }

    async function consult() {
      const rit = document.getElementById("rit").value;
      setStatus("Consultando...");
      const resp = await fetch("/api/consult", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ rit })
      });
      const data = await resp.json();
      if (!resp.ok) {
        setStatus(data.detail || "Error en consulta");
        return;
      }
      setStatus("Consulta terminada");
      await refreshStatus();
    }

    async function logout() {
      await fetch("/api/logout", { method: "POST" });
      setStatus("Sesion cerrada");
      document.getElementById("pdfFrame").src = "about:blank";
      await refreshStatus();
    }

    document.getElementById("loginBtn").addEventListener("click", login);
    document.getElementById("consultBtn").addEventListener("click", consult);
    document.getElementById("logoutBtn").addEventListener("click", logout);
    document.getElementById("refreshBtn").addEventListener("click", refreshStatus);

    refreshStatus();
  </script>
</body>
</html>
"""
