from __future__ import annotations

import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from selenium.common.exceptions import WebDriverException

from main import LOGIN_URL, consult_case, consult_history_only, create_driver, download_pdf_with_session, login_to_sitfa


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


class OpenPdfPayload(BaseModel):
    pdf_url: str = Field(min_length=1)
    prefix: str = "pdf"


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
    logs: list[str] = field(default_factory=list)
    results: dict[str, list[dict]] = field(default_factory=dict)
    detail_rit: str = ""
    detail_rows: list[dict] = field(default_factory=list)
    detail_status: str = ""
    pdf_files: dict[str, Path] = field(default_factory=dict)
    current_pdf_id: str = ""

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        self.logs = self.logs[-300:]

    def clear_pdf(self) -> None:
        if self.current_pdf_id:
            path = self.pdf_files.pop(self.current_pdf_id, None)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.current_pdf_id = ""

    def reset_session(self) -> None:
        self.clear_pdf()
        self.authenticated = False
        self.username = ""
        self.current_rit = ""
        self.status = "Listo"
        self.results = {}
        self.detail_rit = ""
        self.detail_rows = []
        self.detail_status = ""

    def shutdown(self) -> None:
        with self.lock:
            self.reset_session()
            if self.driver is not None:
                try:
                    self.driver.quit()
                except WebDriverException:
                    pass
                self.driver = None


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
        "headless": state.headless,
        "logs": state.logs[-200:],
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
            ensure_driver(state, payload.headless)
            state.append_log("Solicitud de login enviada")
            login_to_sitfa(state.driver, payload.username, payload.password, log_callback=state.append_log)
            state.username = payload.username
            state.current_rit = ""
            state.status = "Sesion iniciada"
            state.results = {}
            state.detail_rit = ""
            state.detail_rows = []
            state.detail_status = ""
            state.authenticated = True
            return {"ok": True, "username": state.username, "headless": state.headless}
        except Exception as exc:
            close_state(state.session_id)
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/consult")
def api_consult(payload: ConsultPayload, request: Request, response: Response) -> dict[str, Any]:
    state = get_session_state(request, response)
    with state.lock:
        driver = require_session(state)
        state.append_log("Consulta enviada para " + payload.rit)
        state.clear_pdf()
        results = consult_case(
            driver,
            payload.rit,
            log_callback=state.append_log,
            section_callback=lambda section, rows: state.results.__setitem__(section, rows),
        )
        state.results = results
        state.current_rit = payload.rit
        state.status = "Consulta terminada para " + payload.rit
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
        pdf_id = uuid.uuid4().hex
        output_path = Path(tempfile.gettempdir()) / ("sitfa_" + payload.prefix + "_" + pdf_id + ".pdf")
        downloaded_path = download_pdf_with_session(driver, pdf_url, output_path)
        state.pdf_files[pdf_id] = downloaded_path
        state.current_pdf_id = pdf_id
        state.append_log("PDF abierto: " + downloaded_path.name)
        return {"ok": True, "url": "/api/pdf/" + pdf_id, "name": downloaded_path.name}


@app.get("/api/pdf/{pdf_id}")
def api_pdf(pdf_id: str, request: Request, response: Response):
    state = get_session_state(request, response)
    path = state.pdf_files.get(pdf_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="PDF no encontrado")
    return Response(
        content=path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="' + path.name + '"'},
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
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Segoe UI, Arial, sans-serif;
      background: #eef3fb;
      color: #182033;
    }
    .shell {
      min-height: 100vh;
      padding: 16px;
    }
    .hidden { display: none; }
    .panel {
      background: #fff;
      border: 1px solid #d9e1ee;
      border-radius: 14px;
      box-shadow: 0 12px 30px rgba(14, 25, 45, 0.08);
    }
    .login-wrap {
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 16px;
    }
    .login-card {
      width: 100%;
      max-width: 420px;
      padding: 24px;
    }
    .title {
      margin: 0 0 8px;
      font-size: 24px;
    }
    .muted { color: #5c6b82; }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; font-size: 13px; color: #5c6b82; }
    input, button {
      font: inherit;
      border-radius: 10px;
      border: 1px solid #d9e1ee;
      padding: 10px 12px;
    }
    button {
      background: #184e9e;
      color: #fff;
      border-color: #184e9e;
      cursor: pointer;
    }
    button.secondary { background: #fff; color: #184e9e; }
    .status {
      padding: 10px 12px;
      background: #f0f5ff;
      border: 1px solid #d7e3fb;
      border-radius: 10px;
      color: #173a73;
      font-size: 13px;
    }
    .hero {
      padding: 18px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 16px;
    }
    .hero h1 { margin: 0; font-size: 20px; }
    .hero p { margin: 4px 0 0; color: #5c6b82; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .grid {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      min-height: 0;
    }
    .card {
      padding: 16px;
      min-width: 0;
    }
    .results {
      display: grid;
      grid-template-rows: auto auto;
      gap: 16px;
      min-height: 0;
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
      background: #fff;
      color: #184e9e;
    }
    .tab-buttons button.active {
      background: #184e9e;
      color: #fff;
    }
    .tab-panels {
      min-width: 0;
      min-height: 0;
      overflow: auto;
    }
    .tab-panel {
      display: none;
    }
    .tab-panel.active {
      display: block;
    }
    .viewer {
      min-width: 0;
    }
    .viewer iframe {
      width: 100%;
      min-height: 620px;
      border: 1px solid #d9e1ee;
      border-radius: 12px;
      background: #fff;
    }
    .logs {
      white-space: pre-wrap;
      font-family: Consolas, monospace;
      font-size: 12px;
      background: #0f172a;
      color: #d9e5ff;
      padding: 12px;
      border-radius: 12px;
      min-height: 220px;
      overflow: auto;
    }
    .section { margin-bottom: 16px; }
    .section h3 { margin: 0 0 8px; font-size: 15px; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      font-size: 12px;
    }
    th, td {
      border: 1px solid #d9e1ee;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #edf3fd;
      position: sticky;
      top: 0;
    }
    .pdf-action {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border-radius: 8px;
      border: 1px solid #d9e1ee;
      background: #f8fbff;
      color: #184e9e;
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
    }
    .pdf-action:hover { background: #eaf2ff; }
    .rit-link {
      padding: 0;
      border: 0;
      background: transparent;
      color: #184e9e;
      cursor: pointer;
      font-weight: 600;
      text-decoration: underline;
    }
    .detail-box {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid #d9e1ee;
    }
    .detail-box h4 {
      margin: 0 0 8px;
      font-size: 14px;
    }
    .detail-status {
      color: #5c6b82;
      font-size: 12px;
      margin-bottom: 8px;
    }
    .empty { color: #5c6b82; padding: 12px 0; }
    .small { font-size: 12px; color: #5c6b82; }
  </style>
</head>
<body>
  <div id="loginView" class="login-wrap">
    <div class="panel login-card">
      <h1 class="title">SITFA Web</h1>
      <p class="muted">Inicia sesion para consultar causas y revisar documentos desde Chrome.</p>
      <div class="stack">
        <label>Usuario <input id="username" autocomplete="username"></label>
        <label>Clave <input id="password" type="password" autocomplete="current-password"></label>
        <label><span>Modo no visible por defecto</span><input id="headless" type="checkbox" checked></label>
        <button type="button" id="loginBtn">Ingresar</button>
        <div id="loginStatus" class="status">Listo para iniciar sesion.</div>
      </div>
    </div>
  </div>

  <div id="appView" class="shell hidden">
    <div class="panel hero">
      <div>
        <h1>SITFA Web</h1>
        <p>Consultas, tablas y visor PDF en una sola pantalla.</p>
      </div>
      <div class="row">
        <button type="button" class="secondary" id="refreshBtn">Actualizar</button>
        <button type="button" class="secondary" id="logoutBtn">Cerrar sesion</button>
      </div>
    </div>

    <div class="grid">
      <div class="panel card">
        <div class="stack">
          <div>
            <h3>Consulta</h3>
            <div class="stack">
              <label>RIT <input id="rit" placeholder="Z-1-2026"></label>
              <button type="button" id="consultBtn">Consultar</button>
            </div>
          </div>
          <div>
            <h3>Estado</h3>
            <div id="status" class="status">Listo.</div>
            <div id="meta" class="small"></div>
          </div>
        </div>
      </div>

      <div class="panel card results">
        <div class="tabs-area">
          <div id="tabButtons" class="tab-buttons"></div>
          <div id="tabPanels" class="tab-panels"></div>
        </div>
        <div class="viewer">
          <iframe id="pdfFrame" title="Visor PDF"></iframe>
        </div>
      </div>
    </div>
  </div>

  <script>
    var state = { results: {}, logs: [], pdfUrl: "", loggedIn: false, detail: { rit: "", rows: [], status: "", loading: false, error: "" } };
    var activeTab = "historia";
    var sectionTitles = {
      historia: "Historia",
      liquidacion: "Liquidacion",
      escritos: "Esc. por Resolv.",
      litigantes: "Litigantes",
      cons_lit: "Cons. Lit.",
      logs: "Logs"
    };
    var sections = ["historia", "liquidacion", "escritos", "litigantes", "cons_lit", "logs"];

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

    function setVisible(id, visible) {
      var el = document.getElementById(id);
      if (el) {
        el.className = visible ? "shell" : "login-wrap";
        if (!visible) {
          el.style.display = "grid";
        }
      }
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
                var viewer = document.getElementById("pdfFrame");
                viewer.src = "about:blank";
                viewer.src = data.url + "?v=" + new Date().getTime();
                refreshStatus();
              } else {
                setText("status", (data && data.detail) || "No se pudo abrir el PDF");
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
                btn.innerHTML = "&#128196;";
                btn.onclick = function () {
                  xhrRequest("POST", "/api/open-pdf", { pdf_url: rowData.pdf_url, prefix: sectionNameInner }, function (status, data) {
                    if (status >= 200 && status < 300 && data.ok) {
                      var viewer = document.getElementById("pdfFrame");
                      viewer.src = "about:blank";
                      viewer.src = data.url + "?v=" + new Date().getTime();
                    } else {
                      setText("status", (data && data.detail) || "No se pudo abrir el PDF");
                    }
                  });
                };
                td.appendChild(btn);
              }
            } else if (enableRitDrilldown && sectionNameInner === "cons_lit" && headers[c] === "RIT" && rowData[headers[c]]) {
              var ritBtn = document.createElement("button");
              ritBtn.type = "button";
              ritBtn.className = "rit-link";
              ritBtn.textContent = String(rowData[headers[c]]);
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
      return table;
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

      if (!activeTab) {
        activeTab = "historia";
      }

      for (var i = 0; i < sections.length; i++) {
        var section = sections[i];
        var tabButton = document.createElement("button");
        tabButton.type = "button";
        tabButton.textContent = sectionTitles[section];
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
          enableRitDrilldown: section === "cons_lit",
          onRitClick: openConsLitHistory
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

    function refreshStatus() {
      xhrRequest("GET", "/api/status", null, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setText("loginStatus", (data && data.detail) || "No se pudo consultar el estado");
          return;
        }

        state.loggedIn = !!data.logged_in;
        state.results = data.results || {};
        state.logs = data.logs || [];
        state.pdfUrl = data.current_pdf_url || "";
        state.detail = {
          rit: data.detail_rit || "",
          rows: data.detail_rows || [],
          status: data.detail_status || "",
          loading: false,
          error: ""
        };

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
          setText("status", data.status || "Listo");
          setText("meta", meta);
        } else {
          setText("loginStatus", "Sin sesion activa. Ingresa tus credenciales para continuar.");
        }

        renderLogs(state.logs);
        renderResults(state.results);
        if (state.pdfUrl) {
          document.getElementById("pdfFrame").src = state.pdfUrl + "?v=" + new Date().getTime();
        }
      });
    }

    function login() {
      var payload = {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
        headless: document.getElementById("headless").checked
      };
      setText("loginStatus", "Iniciando sesion...");
      xhrRequest("POST", "/api/login", payload, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setText("loginStatus", (data && data.detail) || "Error de login");
          return;
        }
        setText("loginStatus", "Sesion iniciada");
        refreshStatus();
      });
    }

    function consult() {
      var rit = document.getElementById("rit").value;
      setText("status", "Consultando...");
      xhrRequest("POST", "/api/consult", { rit: rit }, function (status, data) {
        if (!(status >= 200 && status < 300)) {
          setText("status", (data && data.detail) || "Error en consulta");
          return;
        }
        setText("status", "Consulta terminada");
        refreshStatus();
      });
    }

    function logout() {
      xhrRequest("POST", "/api/logout", null, function () {
        setText("loginStatus", "Sesion cerrada");
        document.getElementById("pdfFrame").src = "about:blank";
        activeTab = "historia";
        refreshStatus();
      });
    }

    document.getElementById("loginBtn").onclick = login;
    document.getElementById("consultBtn").onclick = consult;
    document.getElementById("logoutBtn").onclick = logout;
    document.getElementById("refreshBtn").onclick = refreshStatus;

    refreshStatus();
  </script>
</body>
</html>
"""
