import argparse
import html
import getpass
import json
import os
import re
import subprocess
import socket
import sys
import time
import uuid
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlencode
from urllib.request import Request, urlopen
import unicodedata

import fitz
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.ie.service import Service as IeService
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "http://www.familia.pjud/SITFAWEB/jsp/Login/Login.jsp"
POST_LOGIN_URL = "http://www.familia.pjud/SITFAWEB/MenuLinkAction.do?opMenu=ConsultaRolTramitar&opRol=59"
PENDING_CASES_URL = "http://www.familia.pjud/SITFAWEB/TrmPendientesViewAccion.do?TipoTramite=2"
PDF_VIEWER_HOST = "127.0.0.1"
PDF_VIEWER_PORT = 54877
PDF_VIEWER_SCRIPT = Path(__file__).with_name("pdf_viewer.py")

LogCallback = Callable[[str], None]
SectionCallback = Callable[[str, list[dict]], None]
ProgressCallback = Callable[[str, float], None]


def load_dotenv_file(path: Path | None = None) -> None:
    env_path = path or Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


load_dotenv_file()


ELEMENTS_SCRIPT = """
var cssPath = function(element) {
    if (element.id) {
        return "#" + element.id;
    }

    var parts = [];
    while (element && element.nodeType === 1 && parts.length < 5) {
        var selector = element.nodeName.toLowerCase();
        if (element.name) {
            selector += '[name="' + String(element.name).replace(/"/g, '\\\\"') + '"]';
        } else {
            var index = 1;
            var sibling = element;
            while ((sibling = sibling.previousSibling)) {
                if (sibling.nodeType === 1 && sibling.nodeName === element.nodeName) {
                    index++;
                }
            }
            selector += ":nth-of-type(" + index + ")";
        }
        parts.unshift(selector);
        element = element.parentNode;
    }

    return parts.join(" > ");
};

var textOf = function(element) {
    var text = element.innerText || element.value || element.textContent || "";
    return String(text).replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "").slice(0, 120);
};

var elements = [];
var forms = document.getElementsByTagName("form");
for (var i = 0; i < forms.length; i++) {
    var form = forms[i];
    elements.push({
        kind: "form",
        selector: cssPath(form),
        name: form.getAttribute("name") || "",
        id: form.id || "",
        method: form.getAttribute("method") || "",
        action: form.getAttribute("action") || ""
    });
}

var controls = [];
var tags = ["input", "select", "textarea", "button", "a"];
for (var tagIndex = 0; tagIndex < tags.length; tagIndex++) {
    var byTag = document.getElementsByTagName(tags[tagIndex]);
    for (var tagItem = 0; tagItem < byTag.length; tagItem++) {
        controls.push(byTag[tagItem]);
    }
}
for (var j = 0; j < controls.length; j++) {
    var element = controls[j];
    var item = {
        kind: element.tagName.toLowerCase(),
        selector: cssPath(element),
        name: element.getAttribute("name") || "",
        id: element.id || "",
        type: element.getAttribute("type") || "",
        value: element.value || "",
        text: textOf(element),
        href: element.getAttribute("href") || "",
        disabled: !!element.disabled,
        visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length)
    };

    if (element.tagName.toLowerCase() === "select") {
        item.options = [];
        for (var k = 0; k < element.options.length; k++) {
            var option = element.options[k];
            item.options.push({
                text: option.text,
                value: option.value,
                selected: option.selected
            });
        }
    }

    elements.push(item);
}

return {
    title: document.title,
    url: location.href,
    elements: elements
};
"""


CLICKABLES_SCRIPT = """
var cssPath = function(element) {
    if (element.id) {
        return "#" + element.id;
    }

    var parts = [];
    while (element && element.nodeType === 1 && parts.length < 5) {
        var selector = element.nodeName.toLowerCase();
        if (element.name) {
            selector += '[name="' + String(element.name).replace(/"/g, '\\\\"') + '"]';
        } else {
            var index = 1;
            var sibling = element;
            while ((sibling = sibling.previousSibling)) {
                if (sibling.nodeType === 1 && sibling.nodeName === element.nodeName) {
                    index++;
                }
            }
            selector += ":nth-of-type(" + index + ")";
        }
        parts.unshift(selector);
        element = element.parentNode;
    }

    return parts.join(" > ");
};

var isVisible = function(element) {
    return !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
};

var textOf = function(element) {
    var text = element.innerText ||
        element.value ||
        element.getAttribute("title") ||
        element.getAttribute("alt") ||
        element.getAttribute("aria-label") ||
        element.getAttribute("name") ||
        element.id ||
        element.getAttribute("href") ||
        "";
    return String(text).replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "").slice(0, 120);
};

var nodes = [];
var tags = ["button", "input", "a", "td", "tr", "div", "span", "img"];
for (var tagIndex = 0; tagIndex < tags.length; tagIndex++) {
    var byTag = document.getElementsByTagName(tags[tagIndex]);
    for (var tagItem = 0; tagItem < byTag.length; tagItem++) {
        nodes.push(byTag[tagItem]);
    }
}
var actions = [];
for (var i = 0; i < nodes.length; i++) {
    var target = nodes[i];
    var tag = target.tagName.toLowerCase();
    var type = (target.getAttribute("type") || "").toLowerCase();
    var role = (target.getAttribute("role") || "").toLowerCase();
    var isAction = tag === "button" ||
        tag === "a" ||
        type === "button" ||
        type === "submit" ||
        type === "image" ||
        role === "button" ||
        role === "tab" ||
        target.getAttribute("onclick");

    if (!isAction || !isVisible(target) || target.disabled) {
        continue;
    }

    actions.push({
        action_index: actions.length,
        kind: tag,
        type: type,
        role: role,
        label: textOf(target),
        name: target.getAttribute("name") || "",
        value: target.value || target.getAttribute("value") || "",
        selector: cssPath(target),
        href: target.getAttribute("href") || "",
        target: target.getAttribute("target") || "",
        onclick: target.getAttribute("onclick") || ""
    });
}

return {
    title: document.title,
    url: location.href,
    actions: actions
};
"""


CLICK_ACTION_SCRIPT = """
var actionIndex = arguments[0];

var isVisible = function(element) {
    return !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
};

var nodes = [];
var tags = ["button", "input", "a", "td", "tr", "div", "span", "img"];
for (var tagIndex = 0; tagIndex < tags.length; tagIndex++) {
    var byTag = document.getElementsByTagName(tags[tagIndex]);
    for (var tagItem = 0; tagItem < byTag.length; tagItem++) {
        nodes.push(byTag[tagItem]);
    }
}
var actions = [];
for (var i = 0; i < nodes.length; i++) {
    var target = nodes[i];
    var tag = target.tagName.toLowerCase();
    var type = (target.getAttribute("type") || "").toLowerCase();
    var role = (target.getAttribute("role") || "").toLowerCase();
    var isAction = tag === "button" ||
        tag === "a" ||
        type === "button" ||
        type === "submit" ||
        type === "image" ||
        role === "button" ||
        role === "tab" ||
        target.getAttribute("onclick");

    if (isAction && isVisible(target) && !target.disabled) {
        actions.push(target);
    }
}

var target = actions[actionIndex];
if (!target) {
    throw new Error("No existe accion clicable con indice " + actionIndex);
}

target.scrollIntoView(false);
var tagName = target.tagName.toLowerCase();
var inputType = (target.getAttribute("type") || "").toLowerCase();
var href = target.getAttribute("href") || "";
var targetFrame = target.getAttribute("target") || "";
var hasOnclick = !!target.getAttribute("onclick");

if ((tagName === "input" || tagName === "button") && inputType === "submit" && target.form) {
    if (target.name) {
        var submitter = document.createElement("input");
        submitter.type = "hidden";
        submitter.name = target.name;
        submitter.value = target.value || "";
        target.form.appendChild(submitter);
    }
    target.form.submit();
    return true;
}

if (hasOnclick && target.fireEvent) {
    target.fireEvent("onclick");
    return true;
}

if (hasOnclick && typeof target.onclick === "function") {
    var result = target.onclick.call(target);
    if (result === false) {
        return true;
    }
}

if (tagName === "a" && href && href !== "#") {
    var absoluteHref = target.href || href;
    if (targetFrame) {
        try {
            if (window.parent && window.parent.frames && window.parent.frames[targetFrame]) {
                window.parent.frames[targetFrame].location.href = absoluteHref;
                return true;
            }
            if (window.top && window.top.frames && window.top.frames[targetFrame]) {
                window.top.frames[targetFrame].location.href = absoluteHref;
                return true;
            }
        } catch (frameError) {
        }
    }

    if (typeof target.click === "function") {
        target.click();
        return true;
    }

    window.location.href = absoluteHref;
    return true;
}

if (typeof target.click === "function") {
    target.click();
    return true;
}

return false;
"""


ASYNC_CLICK_ACTION_SCRIPT = """
var actionIndex = arguments[0];

var isVisible = function(element) {
    return !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
};

var nodes = [];
var tags = ["button", "input", "a", "td", "tr", "div", "span", "img"];
for (var tagIndex = 0; tagIndex < tags.length; tagIndex++) {
    var byTag = document.getElementsByTagName(tags[tagIndex]);
    for (var tagItem = 0; tagItem < byTag.length; tagItem++) {
        nodes.push(byTag[tagItem]);
    }
}

var actions = [];
for (var i = 0; i < nodes.length; i++) {
    var target = nodes[i];
    var tag = target.tagName.toLowerCase();
    var type = (target.getAttribute("type") || "").toLowerCase();
    var role = (target.getAttribute("role") || "").toLowerCase();
    var isAction = tag === "button" ||
        tag === "a" ||
        type === "button" ||
        type === "submit" ||
        type === "image" ||
        role === "button" ||
        role === "tab" ||
        target.getAttribute("onclick");

    if (isAction && isVisible(target) && !target.disabled) {
        actions.push(target);
    }
}

var target = actions[actionIndex];
if (!target) {
    throw new Error("No existe accion clicable con indice " + actionIndex);
}

window.setTimeout(function() {
    target.scrollIntoView(false);
    if (target.fireEvent) {
        target.fireEvent("onclick");
        return;
    }
    if (typeof target.click === "function") {
        target.click();
        return;
    }
}, 0);

return true;
"""


FRAMES_INFO_SCRIPT = """
var frames = [];
var nodes = [];
var frameTags = document.getElementsByTagName("frame");
for (var i = 0; i < frameTags.length; i++) {
    nodes.push(frameTags[i]);
}
var iframeTags = document.getElementsByTagName("iframe");
for (var j = 0; j < iframeTags.length; j++) {
    nodes.push(iframeTags[j]);
}
for (var k = 0; k < nodes.length; k++) {
    frames.push({
        index: k + 1,
        name: nodes[k].getAttribute("name") || "",
        id: nodes[k].id || "",
        src: nodes[k].getAttribute("src") || ""
    });
}
return {
    url: location.href,
    title: document.title,
    frames: frames
};
"""


LITIGANTES_DOM_EXTRACTOR_SCRIPT = r"""
function normalize(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
}

function buildHeaderLookup(cells) {
    var normalized = cells.map(function(cell) { return normalize(cell).toLowerCase(); });
    var candidates = [
        ['sujeto'],
        ['rut/pasaporte', 'rut pasaporte', 'rut', 'pasaporte'],
        ['nombre o razon social', 'nombre o razón social', 'razon social', 'nombre'],
        ['fec. nacimiento', 'fec nacimiento', 'fecha nacimiento', 'nacimiento'],
        ['edad']
    ];
    var indexes = [];
    for (var i = 0; i < candidates.length; i++) {
        var found = -1;
        for (var j = 0; j < normalized.length; j++) {
            for (var k = 0; k < candidates[i].length; k++) {
                if (normalized[j].indexOf(candidates[i][k]) >= 0) {
                    found = j;
                    break;
                }
            }
            if (found >= 0) {
                break;
            }
        }
        if (found < 0) {
            return null;
        }
        indexes.push(found);
    }
    return indexes;
}

function extractRows(headerIndexes, headerNames, rows) {
    var result = [];
    for (var i = 0; i < rows.length; i++) {
        var cells = Array.from(rows[i].querySelectorAll('td, th')).map(normalize);
        if (!cells.some(function(value) { return value !== ''; })) {
            continue;
        }
        var row = {};
        for (var j = 0; j < headerIndexes.length; j++) {
            row[headerNames[headerIndexes[j]] || ('col_' + j)] = cells[headerIndexes[j]] || '';
        }
        result.push(row);
    }
    return result;
}

function analyzeDocument(doc) {
    var rows = Array.from(doc.querySelectorAll('tr'));
    for (var ri = 0; ri < rows.length; ri++) {
        var cells = Array.from(rows[ri].querySelectorAll('th, td')).map(normalize);
        if (!cells.length) {
            continue;
        }
        var indexes = buildHeaderLookup(cells);
        if (indexes) {
            var headerNames = cells;
            var dataRows = rows.slice(ri + 1);
            return {
                headers: headerNames,
                rows: extractRows(indexes, headerNames, dataRows)
            };
        }
    }
    return { headers: [], rows: [] };
}

function extractRowsBySelectItem(doc) {
    var rows = Array.from(doc.querySelectorAll('tr'));
    return rows
        .filter(function(row) {
            var onclick = String(row.getAttribute('onclick') || '');
            var clickable = onclick.indexOf('SelectItem') >= 0 || row.querySelector('[onclick*="SelectItem"]');
            return clickable && row.querySelectorAll('td').length > 0;
        })
        .map(function(row) {
            return Array.from(row.querySelectorAll('td, th')).map(normalize);
        });
}

function findInFrames(doc) {
    var result = analyzeDocument(doc);
    if (result.rows.length) {
        return result;
    }
    var fallbackRows = extractRowsBySelectItem(doc);
    if (fallbackRows.length) {
        return { headers: [], rows: fallbackRows };
    }
    var frames = Array.from(doc.querySelectorAll('iframe, frame'));
    for (var i = 0; i < frames.length; i++) {
        var frame = frames[i];
        try {
            var childDoc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (childDoc) {
                var childResult = findInFrames(childDoc);
                if (childResult.rows.length) {
                    return childResult;
                }
            }
        } catch (e) {
            // Ignore inaccessible frames.
        }
    }
    return { headers: [], rows: [] };
}

var found = findInFrames(document);
return {
    headers: found.headers,
    rows: found.rows
};
"""

LOGIN_SCRIPT = """
var data = arguments[0];
var form = document.forms["InicioAplicacionForm"];
if (!form) {
    throw new Error("No se encontro el formulario InicioAplicacionForm");
}

form.elements["username"].value = data.username;
while (form.elements["username"].value.length < 50) {
    form.elements["username"].value += " ";
}
form.elements["password"].value = data.password;
form.elements["nomequipo"].value = data.machineName;
form.elements["usuequipo"].value = data.windowsUser;
form.elements["ipequipo"].value = data.localIp;
form.elements["MAC_equipo"].value = data.macAddress;

var accept = null;
var inputs = form.getElementsByTagName("input");
for (var i = 0; i < inputs.length; i++) {
    if (inputs[i].name === "Aceptar") {
        accept = inputs[i];
        break;
    }
}
if (!accept) {
    accept = document.createElement("input");
    accept.type = "hidden";
    accept.name = "Aceptar";
    form.appendChild(accept);
}
accept.value = "Aceptar";

form.submit();
"""


FILL_LOGIN_SCRIPT = """
var data = arguments[0];
var form = document.forms["InicioAplicacionForm"];
if (!form) {
    throw new Error("No se encontro el formulario InicioAplicacionForm");
}

var username = form.elements["username"];
var password = form.elements["password"];
if (!username || !password) {
    throw new Error("No se encontraron los campos username/password");
}

username.value = data.username;
while (username.value.length < 50) {
    username.value += " ";
}
password.value = data.password;

form.elements["nomequipo"].value = data.machineName;
form.elements["usuequipo"].value = data.windowsUser;
form.elements["ipequipo"].value = data.localIp;
form.elements["MAC_equipo"].value = data.macAddress;

var accept = null;
var inputs = form.getElementsByTagName("input");
for (var i = 0; i < inputs.length; i++) {
    if (inputs[i].name === "Aceptar") {
        accept = inputs[i];
        break;
    }
}
if (!accept) {
    accept = document.createElement("input");
    accept.type = "hidden";
    accept.name = "Aceptar";
    form.appendChild(accept);
}
accept.value = "Aceptar";

return {
    usernameLength: username.value.length,
    usernameTrimmed: username.value.replace(/^\\s+|\\s+$/g, ""),
    passwordLength: password.value.length,
    machineName: form.elements["nomequipo"].value,
    windowsUser: form.elements["usuequipo"].value,
    localIp: form.elements["ipequipo"].value,
    macAddress: form.elements["MAC_equipo"].value
};
"""


SUBMIT_LOGIN_SCRIPT = """
var form = document.forms["InicioAplicacionForm"];
if (!form) {
    throw new Error("No se encontro el formulario InicioAplicacionForm");
}
form.submit();
"""


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "0"


def get_mac_address() -> str:
    mac = uuid.getnode()
    return "-".join(f"{(mac >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


def find_edge_path() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def read_credentials() -> tuple[str, str]:
    username = os.getenv("SITFA_USER") or input("Usuario SITFA: ").strip()
    password = os.getenv("SITFA_PASS")

    if password is None:
        if os.getenv("PYCHARM_HOSTED") == "1" or not sys.stdin.isatty():
            password = input("Clave SITFA: ")
        else:
            password = getpass.getpass("Clave SITFA: ")

    if not username:
        raise ValueError("Falta el usuario. Define SITFA_USER o ingresalo por consola.")
    if not password:
        raise ValueError("Falta la clave. Define SITFA_PASS o ingresala por consola.")

    return username, password


def read_case_query() -> tuple[str, str, str]:
    rit = (os.getenv("SITFA_RIT") or "").strip().upper()
    if rit:
        parts = [part.strip() for part in rit.split("-")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("SITFA_RIT debe tener formato tipo-rol-anio, por ejemplo Z-1-2026.")
        return parts[0], parts[1], parts[2]

    case_type = (os.getenv("SITFA_TIP_CAUSA") or input("Tipo causa (ej: Z): ")).strip().upper()
    case_number = (os.getenv("SITFA_ROL_CAUSA") or input("Rol causa: ")).strip()
    case_year = (os.getenv("SITFA_ERA_CAUSA") or input("Anio causa (ej: 2026): ")).strip()

    if not case_type:
        raise ValueError("Falta el tipo de causa. Define SITFA_TIP_CAUSA o ingresalo por consola.")
    if not case_number:
        raise ValueError("Falta el rol de causa. Define SITFA_ROL_CAUSA o ingresalo por consola.")
    if not case_year:
        raise ValueError("Falta el anio de causa. Define SITFA_ERA_CAUSA o ingresalo por consola.")

    return case_type, case_number, case_year


def parse_rit_value(rit: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in (rit or "").strip().upper().split("-")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("El RIT debe tener formato tipo-rol-anio, por ejemplo Z-1-2026.")
    return parts[0], parts[1], parts[2]


def parse_runtime_options() -> tuple[str, bool, bool]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--browser",
        choices=("ie", "edge"),
        default=None,
        help="Browser mode to use. Defaults to Edge.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run the browser without a visible window.",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        default=False,
        help="Force a visible browser window.",
    )
    parser.add_argument(
        "--dump-litigantes-html",
        action="store_true",
        default=False,
        help="Login, open the Litigantes popup, and dump its HTML to debug_dumps.",
    )
    args = parser.parse_args()

    browser = args.browser or os.getenv("SITFA_BROWSER", "").strip().lower()
    if not browser:
        browser = "edge"

    env_headless = os.getenv("SITFA_HEADLESS")
    default_headless = True if env_headless is None else env_headless.strip() == "1"
    headless = default_headless or args.headless
    if args.visible:
        headless = False

    return browser, headless, bool(args.dump_litigantes_html)


def create_driver(initial_url: str | None = None, browser: str = "edge", headless: bool = False):
    browser = (browser or "edge").strip().lower()
    page_load_strategy = os.getenv("SITFA_PAGE_LOAD_STRATEGY", "eager")

    if browser == "edge":
        options = EdgeOptions()
        options.page_load_strategy = page_load_strategy
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--window-size=1600,1200")

        driver_path = os.getenv("EDGEDRIVER_PATH") or os.getenv("EDGEWEBDRIVER_PATH")
        service = EdgeService(executable_path=driver_path) if driver_path else EdgeService()
        driver = webdriver.Edge(service=service, options=options)
    else:
        options = webdriver.IeOptions()
        options.page_load_strategy = page_load_strategy
        if initial_url:
            options.initial_browser_url = initial_url
        options.ignore_zoom_level = True
        options.ignore_protected_mode_settings = True
        options.ensure_clean_session = False
        options.native_events = False

        mode = os.getenv("SITFA_IE_MODE", "ie").strip().lower()
        if mode != "ie":
            edge_path = os.getenv("SITFA_EDGE_PATH") or find_edge_path()
            options.attach_to_edge_chrome = True
            if edge_path:
                options.edge_executable_path = edge_path

        driver_path = os.getenv("IEDRIVER_PATH")
        service = IeService(executable_path=driver_path) if driver_path else IeService()
        driver = webdriver.Ie(service=service, options=options)

    if initial_url:
        driver.get(initial_url)
    driver.set_page_load_timeout(int(os.getenv("SITFA_PAGE_LOAD_TIMEOUT", "20")))
    driver.set_script_timeout(int(os.getenv("SITFA_SCRIPT_TIMEOUT", "8")))
    return driver


def safe_get(driver, url: str, timeout: int = 20) -> None:
    try:
        before_url = driver.current_url
    except WebDriverException:
        before_url = ""

    try:
        driver.execute_script(
            "var targetUrl = arguments[0]; "
            "window.setTimeout(function() { window.location.href = targetUrl; }, 0); "
            "return true;",
            url,
        )
    except WebDriverException as exc:
        raise RuntimeError(f"No se pudo iniciar navegacion por JavaScript: {exc}") from exc

    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            current_url = driver.current_url
            if current_url != last_url:
                last_url = current_url
            if current_url and current_url != "about:blank" and current_url != before_url:
                return
        except WebDriverException:
            pass

def wait_for_ready(driver, timeout: int = 20) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: current.execute_script("return document.readyState") in {"interactive", "complete"}
        )
    except TimeoutException:
        pass


def wait_for_form(driver, form_name: str, timeout: int = 20) -> None:
    WebDriverWait(driver, timeout).until(
        lambda current: current.execute_script(
            "return !!document.forms[arguments[0]];",
            form_name,
        )
    )


def switch_to_frame_with_form(driver, form_name: str, timeout: int = 20) -> None:
    deadline = time.time() + timeout

    while time.time() < deadline:
        driver.switch_to.default_content()
        try:
            if driver.execute_script("return !!document.forms[arguments[0]];", form_name):
                return
        except WebDriverException:
            pass

        frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        for frame in frames:
            driver.switch_to.default_content()
            try:
                driver.switch_to.frame(frame)
                if driver.execute_script("return !!document.forms[arguments[0]];", form_name):
                    return
            except WebDriverException:
                continue

        time.sleep(0.5)

    driver.switch_to.default_content()
    raise TimeoutException(f"No encontre el formulario {form_name}.")


def wait_for_case_type_option(driver, case_type: str, timeout: int = 20) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda current: current.execute_script(
                """
                var form = document.forms["TramitarPpalForm"];
                if (!form || !form.elements["TIP_Causa"]) {
                    return false;
                }
                var options = form.elements["TIP_Causa"].options;
                for (var i = 0; i < options.length; i++) {
                    if (options[i].value === arguments[0] || options[i].text === arguments[0]) {
                        return true;
                    }
                }
                return false;
                """,
                case_type,
            )
        )
    except TimeoutException as exc:
        available_case_types = get_case_type_options(driver)
        available_summary = ", ".join(
            f"{item.get('value', '')}:{item.get('text', '')}" for item in available_case_types if item
        )
        raise RuntimeError(
            f"El tipo de causa '{case_type}' no estuvo disponible en TIP_Causa. "
            f"Opciones vistas: {available_summary or 'ninguna'}."
        ) from exc


def get_case_type_options(driver) -> list[dict[str, str]]:
    try:
        options = driver.execute_script(
            """
            var form = document.forms["TramitarPpalForm"];
            var select = form ? form.elements["TIP_Causa"] : null;
            var values = [];
            if (!select || !select.options) {
                return values;
            }
            for (var i = 0; i < select.options.length; i++) {
                values.push({
                    value: String(select.options[i].value || ""),
                    text: String(select.options[i].text || "")
                });
            }
            return values;
            """
        )
        return list(options or [])
    except WebDriverException:
        return []


def get_query_form_snapshot(driver) -> dict[str, Any]:
    try:
        snapshot = driver.execute_script(
            """
            var form = document.forms["TramitarPpalForm"];
            if (!form) {
                throw new Error("No se encontro TramitarPpalForm");
            }
            var fields = {};
            var elements = form.elements;
            for (var i = 0; i < elements.length; i++) {
                var el = elements[i];
                if (!el || !el.name || el.disabled) {
                    continue;
                }
                var tag = String(el.tagName || "").toLowerCase();
                var type = String(el.type || "").toLowerCase();
                if (type === "checkbox" || type === "radio") {
                    if (el.checked) {
                        fields[el.name] = String(el.value || "on");
                    }
                    continue;
                }
                if (tag === "select" && el.multiple) {
                    var values = [];
                    for (var j = 0; j < el.options.length; j++) {
                        if (el.options[j].selected) {
                            values.push(String(el.options[j].value || el.options[j].text || ""));
                        }
                    }
                    fields[el.name] = values;
                    continue;
                }
                fields[el.name] = String(el.value || "");
            }
            return {
                action: String(form.action || ""),
                method: String(form.method || "GET").toUpperCase(),
                fields: fields
            };
            """
        )
        return dict(snapshot or {})
    except WebDriverException as exc:
        raise RuntimeError(f"No se pudo leer el formulario de consulta: {exc}") from exc


def load_html_into_current_document(driver, html_text: str, base_url: str = "") -> None:
    content = html_text or ""
    if base_url:
        base_tag = f'<base href="{html.escape(base_url, quote=True)}">'
        lower = content.lower()
        if "<head>" in lower and "<base" not in lower:
            content = re.sub(r"(?i)<head>", "<head>" + base_tag, content, count=1)
        elif "<html" in lower and "<head>" not in lower:
            content = content.replace("<html", "<html><head>" + base_tag, 1)
        elif "<head" not in lower:
            content = base_tag + content

    driver.execute_script(
        """
        var html = arguments[0];
        document.open();
        document.write(html);
        document.close();
        """,
        content,
    )
    wait_for_ready(driver, timeout=30)


def submit_query_form_direct(driver, case_type: str, case_number: str, case_year: str) -> None:
    form_snapshot = get_query_form_snapshot(driver)
    action_url = urljoin(driver.current_url, str(form_snapshot.get("action") or ""))
    method = str(form_snapshot.get("method") or "POST").upper()
    fields = dict(form_snapshot.get("fields") or {})
    fields["TIP_Causa"] = case_type
    fields["ROL_Causa"] = case_number
    fields["ERA_Causa"] = case_year

    cookies = driver.get_cookies()
    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name") is not None
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": driver.current_url,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    if method == "GET":
        query_string = urlencode(fields, doseq=True)
        request_url = action_url + ("&" if "?" in action_url else "?") + query_string
        request = Request(request_url, headers=headers)
        post_data = None
    else:
        request = Request(action_url, data=urlencode(fields, doseq=True).encode("utf-8"), headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
        post_data = True

    with urlopen(request, timeout=60) as response:
        content = response.read()
        content_type = response.headers.get_content_type() or ""
        final_url = response.geturl()

    if "html" not in content_type.lower() and not content.lstrip().startswith(b"<"):
        raise RuntimeError(f"La consulta directa no devolvio HTML (tipo={content_type or 'desconocido'}).")

    try:
        html_text = content.decode("utf-8", errors="replace")
    except Exception:
        html_text = content.decode("latin-1", errors="replace")

    load_html_into_current_document(driver, html_text, final_url)


def find_case_type_scope(driver, case_type: str) -> dict | None:
    try:
        scope_results = inspect_window_scopes(driver)
    except WebDriverException:
        return None

    target = case_type.strip().upper()
    if not target:
        return None

    for scope in scope_results:
        html_source = (scope.get("html") or "").upper()
        if "TIP_CAUSA" not in html_source:
            continue
        if f"VALUE={target}" in html_source or f">{target}<" in html_source:
            return scope
    return None


def wait_for_query_form_inputs(driver, timeout: int = 20) -> None:
    WebDriverWait(driver, timeout).until(
        lambda current: current.execute_script(
            """
            var form = document.forms["TramitarPpalForm"];
            if (!form) {
                return false;
            }
            var tip = form.elements["TIP_Causa"];
            var rol = form.elements["ROL_Causa"];
            var era = form.elements["ERA_Causa"];
            return !!(tip && rol && era && tip.options && tip.options.length && era.options && era.options.length);
            """
        )
    )


def ensure_query_form_ready(driver, timeout: int = 15) -> str:
    try:
        switch_to_frame_with_form(driver, "TramitarPpalForm", timeout=2)
        wait_for_query_form_inputs(driver, timeout=min(timeout, 5))
        return "reused"
    except TimeoutException:
        driver.switch_to.default_content()

    safe_get(driver, POST_LOGIN_URL, timeout=timeout)
    switch_to_frame_with_form(driver, "TramitarPpalForm", timeout=timeout)
    wait_for_query_form_inputs(driver, timeout=timeout)
    return "navigated"


def extract_history_tables(driver) -> list[dict]:
    return driver.execute_script(
        """
        var normalize = function(value) {
            return String(value || "").replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "");
        };
        var tables = [];
        var tbody = document.getElementById("bodyTramitesFirmados");
        if (!tbody) {
            return tables;
        }

        var table = tbody.closest ? tbody.closest("table") : null;
        if (!table) {
            table = tbody.parentNode;
            while (table && table.tagName && table.tagName.toLowerCase() !== "table") {
                table = table.parentNode;
            }
        }
        if (!table) {
            return tables;
        }

        var headers = [];
        var thead = table.getElementsByTagName("thead")[0];
        if (thead) {
            var headerRow = thead.getElementsByTagName("tr")[0];
            if (headerRow) {
                var headerCells = headerRow.children;
                for (var h = 0; h < headerCells.length; h++) {
                    var headerTag = headerCells[h].tagName.toLowerCase();
                    if (headerTag === "td" || headerTag === "th") {
                        headers.push(normalize(headerCells[h].innerText || headerCells[h].textContent));
                    }
                }
            }
        }

        var rows = [];
        var trs = tbody.getElementsByTagName("tr");
        for (var rowIndex = 0; rowIndex < trs.length; rowIndex++) {
            var row = trs[rowIndex];
            var cells = [];
            var children = row.children;
            for (var cellIndex = 0; cellIndex < children.length; cellIndex++) {
                var child = children[cellIndex];
                var tag = child.tagName.toLowerCase();
                if (tag === "td" || tag === "th") {
                    cells.push(normalize(child.innerText || child.textContent));
                }
            }
            if (cells.length) {
                var pdfUrl = "";
                var imgs = row.getElementsByTagName("img");
                for (var j = 0; j < imgs.length; j++) {
                    var onclick = String(imgs[j].getAttribute("onclick") || "");
                    var match = onclick.match(/ShowPDFEscrito\\('([^']+)'\\)/);
                    if (!match) {
                        match = onclick.match(/ShowPDF\\('([^']+)'\\)/);
                    }
                    if (match && match[1]) {
                        pdfUrl = match[1];
                        break;
                    }
                }

                rows.push({
                    cells: cells,
                    pdf_url: pdfUrl
                });
            }
        }

        tables.push({
            id: tbody.id || "",
            class_name: table.className || "",
            headers: headers,
            rows: rows,
            row_count: rows.length
        });

        return tables;
        """
    )


def extract_liquidacion_tables(driver) -> list[dict]:
    return driver.execute_script(
        """
        var normalize = function(value) {
            return String(value || "").replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "");
        };
        var tables = [];
        var root = document.getElementById("liquidacion");
        if (!root) {
            return tables;
        }

        var candidateTables = root.getElementsByTagName("table");
        var table = null;
        for (var i = 0; i < candidateTables.length; i++) {
            var candidate = candidateTables[i];
            var className = String(candidate.className || "").toLowerCase();
            if (className.indexOf("nuevatabla") >= 0) {
                table = candidate;
                break;
            }
        }
        if (!table && candidateTables.length) {
            for (var j = 0; j < candidateTables.length; j++) {
                var possible = candidateTables[j];
                if (possible.getElementsByTagName("th").length >= 6) {
                    table = possible;
                    break;
                }
            }
        }
        if (!table) {
            return tables;
        }

        var headers = [];
        var thead = table.getElementsByTagName("thead")[0];
        if (thead) {
            var headerRow = thead.getElementsByTagName("tr")[0];
            if (headerRow) {
                var headerCells = headerRow.children;
                for (var h = 0; h < headerCells.length; h++) {
                    var headerTag = headerCells[h].tagName.toLowerCase();
                    if (headerTag === "td" || headerTag === "th") {
                        headers.push(normalize(headerCells[h].innerText || headerCells[h].textContent));
                    }
                }
            }
        }

        var rows = [];
        var tbody = table.getElementsByTagName("tbody")[0];
        if (!tbody) {
            return tables;
        }

        var trs = tbody.getElementsByTagName("tr");
        for (var rowIndex = 0; rowIndex < trs.length; rowIndex++) {
            var row = trs[rowIndex];
            var cells = [];
            var children = row.children;
            for (var cellIndex = 0; cellIndex < children.length; cellIndex++) {
                var child = children[cellIndex];
                var tag = child.tagName.toLowerCase();
                if (tag === "td" || tag === "th") {
                    cells.push(normalize(child.innerText || child.textContent));
                }
            }
            if (cells.length) {
                var pdfUrl = "";
                var imgs = row.getElementsByTagName("img");
                for (var j = 0; j < imgs.length; j++) {
                    var onclick = String(imgs[j].getAttribute("onclick") || "");
                    var match = onclick.match(/ShowPDFEscrito\\('([^']+)'\\)/);
                    if (!match) {
                        match = onclick.match(/ShowPDF\\('([^']+)'\\)/);
                    }
                    if (match && match[1]) {
                        pdfUrl = match[1];
                        break;
                    }
                }

                rows.push({
                    cells: cells,
                    pdf_url: pdfUrl
                });
            }
        }

        tables.push({
            id: table.id || "",
            class_name: table.className || "",
            headers: headers,
            rows: rows,
            row_count: rows.length
        });

        return tables;
        """
    )



def extract_escritos_resolver_tables(driver) -> list[dict]:
    return driver.execute_script(
        """
        var normalize = function(value) {
            return String(value || "").replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "");
        };
        var tables = [];
        var root = document.getElementById("escxResolver");
        if (!root) {
            return tables;
        }

        var candidateTables = root.getElementsByTagName("table");
        var table = null;
        for (var i = 0; i < candidateTables.length; i++) {
            var candidate = candidateTables[i];
            var className = String(candidate.className || "").toLowerCase();
            if (className.indexOf("nuevatabla") >= 0) {
                table = candidate;
                break;
            }
        }
        if (!table && candidateTables.length) {
            for (var j = 0; j < candidateTables.length; j++) {
                var possible = candidateTables[j];
                if (possible.getElementsByTagName("th").length >= 5) {
                    table = possible;
                    break;
                }
            }
        }
        if (!table) {
            return tables;
        }

        var headers = [];
        var thead = table.getElementsByTagName("thead")[0];
        if (thead) {
            var headerRow = thead.getElementsByTagName("tr")[0];
            if (headerRow) {
                var headerCells = headerRow.children;
                for (var h = 0; h < headerCells.length; h++) {
                    var headerTag = headerCells[h].tagName.toLowerCase();
                    if (headerTag === "td" || headerTag === "th") {
                        headers.push(normalize(headerCells[h].innerText || headerCells[h].textContent));
                    }
                }
            }
        }

        var rows = [];
        var tbody = table.getElementsByTagName("tbody")[0];
        if (!tbody) {
            return tables;
        }

        var trs = tbody.getElementsByTagName("tr");
        for (var rowIndex = 0; rowIndex < trs.length; rowIndex++) {
            var row = trs[rowIndex];
            var cells = [];
            var children = row.children;
            for (var cellIndex = 0; cellIndex < children.length; cellIndex++) {
                var child = children[cellIndex];
                var tag = child.tagName.toLowerCase();
                if (tag === "td" || tag === "th") {
                    cells.push(normalize(child.innerText || child.textContent));
                }
            }
            if (cells.length) {
                var pdfUrl = "";
                var nodes = row.getElementsByTagName("img");
                for (var j = 0; j < nodes.length; j++) {
                    var onclick = String(nodes[j].getAttribute("onclick") || "");
                    var match = onclick.match(/ShowPDFEscrito\\('([^']+)'\\)/);
                    if (!match) {
                        match = onclick.match(/ShowPDF\\('([^']+)'\\)/);
                    }
                    if (match && match[1]) {
                        pdfUrl = match[1];
                        break;
                    }
                }
                if (!pdfUrl) {
                    var buttons = row.getElementsByTagName("button");
                    for (var b = 0; b < buttons.length; b++) {
                        var buttonOnclick = String(buttons[b].getAttribute("onclick") || "");
                        var buttonMatch = buttonOnclick.match(/ShowPDFEscrito\\('([^']+)'\\)/);
                        if (!buttonMatch) {
                            buttonMatch = buttonOnclick.match(/ShowPDF\\('([^']+)'\\)/);
                        }
                        if (buttonMatch && buttonMatch[1]) {
                            pdfUrl = buttonMatch[1];
                            break;
                        }
                    }
                }

                rows.push({
                    cells: cells,
                    pdf_url: pdfUrl
                });
            }
        }

        tables.push({
            id: table.id || "",
            class_name: table.className || "",
            headers: headers,
            rows: rows,
            row_count: rows.length
        });

        return tables;
        """
    )


def collect_history_rows(driver) -> list[dict]:
    try:
        tables = extract_history_tables(driver)
    except WebDriverException:
        return []

    for table in tables:
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if not headers or not rows:
            continue

        header_lookup = {str(header).strip().lower(): index for index, header in enumerate(headers)}
        fecha_index = header_lookup.get("fecha")
        referencia_index = header_lookup.get("referencia")
        if fecha_index is None or referencia_index is None:
            continue

        history_rows = []
        for row_index, row in enumerate(rows, start=1):
            cells = row.get("cells") or []
            fecha = cells[fecha_index] if fecha_index < len(cells) else ""
            referencia = cells[referencia_index] if referencia_index < len(cells) else ""
            fecha = " ".join(str(fecha).split())
            referencia = " ".join(str(referencia).split())
            pdf_url = row.get("pdf_url") or ""
            if fecha or referencia:
                history_rows.append(
                    {
                        "index": row_index,
                        "fecha": fecha,
                        "referencia": referencia,
                        "pdf_url": pdf_url,
                    }
                )
        return history_rows

    return []


def print_history_tab(driver) -> list[dict]:
    print("")
    print("Historia")
    history_rows = collect_history_rows(driver)
    if not history_rows:
        print("  No se encontraron filas de historia.")
        return []

    for item in history_rows:
        print(f"{item['index']}. {item['fecha']} | {item['referencia']}")

    return history_rows


def collect_liquidacion_rows(driver) -> list[dict]:
    try:
        tables = extract_liquidacion_tables(driver)
    except WebDriverException:
        return []

    for table in tables:
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if not headers or not rows:
            continue

        header_lookup = {str(header).strip().lower(): index for index, header in enumerate(headers)}
        fecha_index = header_lookup.get("fecha")
        referencia_index = header_lookup.get("referencia")
        if fecha_index is None or referencia_index is None:
            continue

        liquidacion_rows = []
        for row_index, row in enumerate(rows, start=1):
            cells = row.get("cells") or []
            fecha = cells[fecha_index] if fecha_index < len(cells) else ""
            referencia = cells[referencia_index] if referencia_index < len(cells) else ""
            fecha = " ".join(str(fecha).split())
            referencia = " ".join(str(referencia).split())
            pdf_url = row.get("pdf_url") or ""
            if fecha or referencia:
                liquidacion_rows.append(
                    {
                        "index": row_index,
                        "fecha": fecha,
                        "referencia": referencia,
                        "pdf_url": pdf_url,
                    }
                )
        return liquidacion_rows

    return []


def print_liquidacion_tab(driver) -> list[dict]:
    print("")
    print("Liquidacion")
    liquidacion_rows = collect_liquidacion_rows(driver)
    if not liquidacion_rows:
        print("  No se encontraron filas de liquidacion.")
        return []

    for item in liquidacion_rows:
        print(f"{item['index']}. {item['fecha']} | {item['referencia']}")

    return liquidacion_rows


def collect_escritos_resolver_rows(driver) -> list[dict]:
    try:
        tables = extract_escritos_resolver_tables(driver)
    except WebDriverException:
        return []

    for table in tables:
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if not headers or not rows:
            continue

        header_lookup = {str(header).strip().lower(): index for index, header in enumerate(headers)}
        fecha_index = (
            header_lookup.get("fecha ing.")
            if header_lookup.get("fecha ing.") is not None
            else header_lookup.get("fecha ing")
        )
        if fecha_index is None:
            fecha_index = header_lookup.get("fecha")
        tipo_escrito_index = header_lookup.get("tipo escrito")
        if fecha_index is None or tipo_escrito_index is None:
            continue

        resolver_rows = []
        for row_index, row in enumerate(rows, start=1):
            cells = row.get("cells") or []
            fecha = cells[fecha_index] if fecha_index < len(cells) else ""
            referencia = cells[tipo_escrito_index] if tipo_escrito_index < len(cells) else ""
            fecha = " ".join(str(fecha).split())
            referencia = " ".join(str(referencia).split())
            pdf_url = row.get("pdf_url") or ""
            if fecha or referencia:
                resolver_rows.append(
                    {
                        "index": row_index,
                        "fecha": fecha,
                        "referencia": referencia,
                        "pdf_url": pdf_url,
                    }
                )
        return resolver_rows

    return []


def print_escritos_resolver_tab(driver) -> list[dict]:
    print("")
    print("Esc.por Resolv.")
    resolver_rows = collect_escritos_resolver_rows(driver)
    if not resolver_rows:
        print("  No se encontraron filas de escritos por resolver.")
        return []

    for item in resolver_rows:
        print(f"{item['index']}. {item['fecha']} | {item['referencia']}")

    return resolver_rows


def prompt_escritos_resolver_row_to_open(driver, resolver_rows: list[dict]) -> None:
    if not resolver_rows:
        return

    while True:
        choice = input("Numero de fila de escritos por resolver para abrir PDF (q para salir): ").strip().lower()
        if not choice:
            continue
        if choice == "q":
            return
        if not choice.isdigit():
            print("Numero invalido.")
            continue

        selected_index = int(choice)
        selected = next((item for item in resolver_rows if item["index"] == selected_index), None)
        if not selected:
            print("La fila no existe.")
            continue
        if not selected["pdf_url"]:
            print("Esa fila no tiene PDF asociado.")
            continue

        output_path = Path.cwd() / f"escritos_resolver_fila_{selected_index}.pdf"
        try:
            downloaded_path = download_and_open_pdf(driver, selected["pdf_url"], output_path)
        except Exception as exc:
            print(f"No se pudo descargar u abrir el PDF: {exc}")
            continue

        print(f"PDF descargado y abierto: {downloaded_path}")


def resolve_litigantes_popup_url(driver, log_callback: LogCallback | None = None) -> str:
    driver.switch_to.default_content()
    try:
        step_start = time.perf_counter()
        frames = [None] + driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        emit_log(log_callback, f"Litigantes cabecera: buscando popup en {len(frames) - 1} frame(s)")

        for frame in frames:
            driver.switch_to.default_content()
            if frame is not None:
                try:
                    driver.switch_to.frame(frame)
                except WebDriverException:
                    continue

            try:
                popup_url = driver.execute_script(
                    """
                    var capturedUrl = "";
                    if (typeof ShowPopUpCabecera !== "function") {
                        return "";
                    }
                    var originalDialog = window.showModalDialog;
                    try {
                        window.showModalDialog = function(url) {
                            capturedUrl = String(url || "");
                            return null;
                        };
                        ShowPopUpCabecera(5);
                    } finally {
                        window.showModalDialog = originalDialog;
                    }
                    return capturedUrl;
                    """
                )
            except WebDriverException:
                popup_url = ""

            popup_url = str(popup_url or "").strip()
            if popup_url:
                emit_log(log_callback, f"Litigantes cabecera: popup capturado en {time.perf_counter() - step_start:.1f}s")
                driver.switch_to.default_content()
                return popup_url

        step_start = time.perf_counter()
        actions = get_clickable_actions(driver, verbose=False)
        emit_log(log_callback, f"Litigantes cabecera: acciones leidas en {time.perf_counter() - step_start:.1f}s")

        step_start = time.perf_counter()
        action = next(
            (
                item
                for item in actions
                if "showpopupcabecera(5)" in (item.get("onclick") or "").lower()
                or ((item.get("label") or "").strip().lower() == "litigantes" and item.get("kind") == "button")
            ),
            None,
        )
        if action is None:
            action = find_action_by_label(actions, ("litigantes",))
        emit_log(log_callback, f"Litigantes cabecera: accion localizada en {time.perf_counter() - step_start:.1f}s")
        if action:
            click_action(driver, action, verbose=False, log_callback=log_callback, log_prefix="Litigantes cabecera")
            clicked = True
        else:
            clicked = False
    except WebDriverException as exc:
        raise RuntimeError(f"No se pudo invocar ShowPopUpCabecera(5): {exc}") from exc

    if not clicked:
        raise RuntimeError("No encontre el boton o funcion de Litigantes.")
    driver.switch_to.default_content()
    return ""


def fetch_litigantes_popup_html(
    driver,
    popup_url: str = "",
    log_callback: LogCallback | None = None,
) -> str:
    if popup_url:
        try:
            content_type, popup_bytes = fetch_session_resource_bytes(driver, popup_url)
            if "pdf" in (content_type or "").lower() or popup_bytes.startswith(b"%PDF"):
                try:
                    pdf_doc = fitz.open(stream=popup_bytes, filetype="pdf")
                    pdf_text = "\n".join(page.get_text("text") for page in pdf_doc)
                except Exception as exc:
                    emit_log(log_callback, f"Litigantes: error leyendo PDF directo {popup_url}: {exc}")
                    pdf_text = popup_bytes.decode("latin-1", errors="replace")
                popup_rows = extract_litigantes_rows_from_pdf_text(pdf_text)
                if popup_rows:
                    emit_log(
                        log_callback,
                        f"Litigantes: descarga directa PDF con {len(popup_rows)} filas (tipo={content_type or 'desconocido'})",
                    )
                    return pdf_text
                emit_log(
                    log_callback,
                    f"Litigantes: PDF directo sin filas, tipo={content_type or 'desconocido'}, len={len(pdf_text)}",
                )
                popup_html = pdf_text
            else:
                popup_html = popup_bytes.decode("utf-8", errors="replace")
                popup_rows = extract_litigantes_rows_from_html(popup_html)
            if popup_rows:
                emit_log(
                    log_callback,
                    f"Litigantes: descarga directa con {len(popup_rows)} filas (tipo={content_type or 'desconocido'})",
                )
                return popup_html
            emit_log(
                log_callback,
                f"Litigantes: descarga directa sin filas, tipo={content_type or 'desconocido'}, len={len(popup_html)}",
            )
            frame_src = extract_popup_frame_src(popup_html)
            if frame_src:
                try:
                    _, frame_html = fetch_session_resource_text(driver, frame_src)
                    frame_rows = extract_litigantes_rows_from_html(frame_html)
                    if frame_rows:
                        emit_log(log_callback, f"Litigantes: descarga directa frame_src con {len(frame_rows)} filas")
                        return frame_html
                    emit_log(log_callback, "Litigantes: descarga directa frame_src encontrado pero no extrajo filas")
                    popup_html = frame_html
                except Exception as exc:
                    emit_log(log_callback, f"Litigantes: error descargando frame_src {frame_src}: {exc}")

            try:
                outer_rows = extract_litigantes_rows_from_html(popup_html)
                if outer_rows:
                    emit_log(log_callback, f"Litigantes: descarga directa outerHTML con {len(outer_rows)} filas")
                    return popup_html
            except Exception:
                pass

            return popup_html
        except Exception as exc:
            emit_log(log_callback, f"Litigantes: error descargando URL {popup_url}: {exc}")

    popup_html = driver.page_source or ""
    if popup_html:
        popup_rows = extract_litigantes_rows_from_html(popup_html)
        if popup_rows:
            emit_log(log_callback, f"Litigantes: popup directo con {len(popup_rows)} filas")
            return popup_html
        emit_log(log_callback, f"Litigantes: popup directo sin filas, page_source len={len(popup_html)}")

    frame_src = extract_popup_frame_src(popup_html)
    if frame_src:
        try:
            _, frame_html = fetch_session_resource_text(driver, frame_src)
            frame_rows = extract_litigantes_rows_from_html(frame_html)
            if frame_rows:
                emit_log(log_callback, f"Litigantes: frame_src parseado con {len(frame_rows)} filas")
                return frame_html
            emit_log(log_callback, "Litigantes: frame_src encontrado pero no extrajo filas")
        except Exception as exc:
            emit_log(log_callback, f"Litigantes: error descargando frame_src {frame_src}: {exc}")

    try:
        outer_html = driver.execute_script("return document.documentElement.outerHTML || document.body.outerHTML || '';") or ""
    except WebDriverException as exc:
        outer_html = ""
        emit_log(log_callback, f"Litigantes: outerHTML error: {exc}")

    if outer_html and outer_html != popup_html:
        outer_rows = extract_litigantes_rows_from_html(outer_html)
        if outer_rows:
            emit_log(log_callback, f"Litigantes: outerHTML con {len(outer_rows)} filas")
            return outer_html
        emit_log(log_callback, f"Litigantes: outerHTML sin filas, len={len(outer_html)}")

    fallback_html = fetch_litigantes_popup_html_from_scopes(driver, log_callback=log_callback)
    if fallback_html:
        return fallback_html

    emit_log(log_callback, "Litigantes: usando popup directo como fallback")
    return popup_html


def fetch_litigantes_popup_html_from_scopes(driver, log_callback: LogCallback | None = None) -> str:
    try:
        scope_results = inspect_window_scopes(driver)
    except WebDriverException as exc:
        emit_log(log_callback, f"Litigantes: inspeccion de scopes fallo: {exc}")
        return ""

    if not scope_results:
        emit_log(log_callback, "Litigantes: no se encontraron scopes en popup")
        return ""

    for scope in scope_results:
        html_source = scope.get("html") or ""
        frame_src = scope.get("frame_src") or ""
        if not html_source and frame_src:
            try:
                _, html_source = fetch_session_resource_text(driver, frame_src)
            except Exception as exc:
                emit_log(
                    log_callback,
                    f"Litigantes: error descargando frame_src de scope {scope.get('frame_index')}: {exc}",
                )
                continue

        if not html_source:
            continue

        rows = extract_litigantes_rows_from_html(html_source)
        if rows:
            emit_log(
                log_callback,
                f"Litigantes: encontrado en scope {scope.get('frame_index')} "
                f"name={scope.get('frame_name')!r} src={frame_src or '(inline)'} filas={len(rows)}",
            )
            return html_source

    return ""


def collect_litigantes_rows(driver) -> list[dict]:
    original_handle = ""
    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        pass

    try:
        popup_url = resolve_litigantes_popup_url(driver)
        if not popup_url:
            return []
        frame_html = fetch_litigantes_popup_html(driver, popup_url)
        rows = extract_litigantes_rows_from_html(frame_html)
        if not rows:
            rows = extract_litigantes_rows_from_dom(driver)
        return rows
    finally:
        restore_primary_window(driver, original_handle)


def collect_cons_lit_rows(driver, subject_label: str = "DDO.") -> list[dict]:
    original_handle = ""
    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        pass

    try:
        popup_url = resolve_litigantes_popup_url(driver)
        if not popup_url:
            return []
        frame_html = fetch_litigantes_popup_html(driver, popup_url)
        cons_lit_url, _ = open_cons_lit_popup_from_litigantes(
            driver,
            subject_label=subject_label,
            litigantes_popup_url=popup_url,
            log_callback=None,
        )
        if not cons_lit_url:
            return []
        _, cons_lit_popup_html = fetch_session_resource_text(driver, cons_lit_url)
        cons_lit_frame_src = extract_popup_frame_src(cons_lit_popup_html)
        cons_lit_html = cons_lit_popup_html
        if cons_lit_frame_src:
            _, cons_lit_html = fetch_session_resource_text(driver, cons_lit_frame_src)
        return extract_cons_lit_rows_from_html(cons_lit_html)
    finally:
        restore_primary_window(driver, original_handle)


def collect_litigantes_and_cons_lit_rows(
    driver,
    subject_label: str = "DDO.",
    log_callback: LogCallback | None = None,
) -> tuple[list[dict], list[dict]]:
    original_handle = ""
    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        pass

    litigantes_rows: list[dict] = []
    cons_lit_rows: list[dict] = []

    try:
        step_start = time.perf_counter()
        popup_url = resolve_litigantes_popup_url(driver, log_callback=log_callback)
        emit_log(log_callback, f"Litigantes: popup de cabecera resuelto en {time.perf_counter() - step_start:.1f}s")
        if not popup_url:
            return litigantes_rows, cons_lit_rows

        step_start = time.perf_counter()
        frame_html = fetch_litigantes_popup_html(driver, popup_url, log_callback=log_callback)
        litigantes_rows = extract_litigantes_rows_from_html(frame_html)
        if not litigantes_rows:
            litigantes_rows = extract_litigantes_rows_from_dom(driver, log_callback=log_callback)
            if litigantes_rows:
                emit_log(
                    log_callback,
                    f"Litigantes: DOM extraido y {len(litigantes_rows)} filas en {time.perf_counter() - step_start:.1f}s",
                )
            else:
                emit_log(
                    log_callback,
                    f"Litigantes: HTML leido y {len(litigantes_rows)} filas extraidas en {time.perf_counter() - step_start:.1f}s",
                )
        else:
            emit_log(
                log_callback,
                f"Litigantes: HTML leido y {len(litigantes_rows)} filas extraidas en {time.perf_counter() - step_start:.1f}s",
            )

        step_start = time.perf_counter()
        cons_lit_url, cons_lit_reason = open_cons_lit_popup_from_litigantes(
            driver,
            subject_label=subject_label,
            litigantes_popup_url=popup_url,
            log_callback=log_callback,
        )
        if not cons_lit_url:
            if cons_lit_reason:
                emit_log(log_callback, f"Cons. Lit.: no disponible ({cons_lit_reason})")
            return litigantes_rows, cons_lit_rows
        emit_log(log_callback, f"Cons. Lit.: URL obtenida en {time.perf_counter() - step_start:.1f}s")

        step_start = time.perf_counter()
        cons_content_type, cons_lit_popup_html = fetch_session_resource_text(driver, cons_lit_url)
        cons_lit_frame_src = extract_popup_frame_src(cons_lit_popup_html)
        cons_lit_html = cons_lit_popup_html
        if cons_lit_frame_src:
            _, cons_lit_html = fetch_session_resource_text(driver, cons_lit_frame_src)
        cons_lit_rows = extract_cons_lit_rows_from_html(cons_lit_html)
        emit_log(
            log_callback,
            f"Cons. Lit.: HTML leido y {len(cons_lit_rows)} filas extraidas en {time.perf_counter() - step_start:.1f}s "
            f"(tipo={cons_content_type or 'desconocido'}, len={len(cons_lit_popup_html)})",
        )
        log_cons_lit_rows(log_callback, cons_lit_rows)
        return litigantes_rows, cons_lit_rows
    finally:
        restore_primary_window(driver, original_handle)


def emit_log(log_callback: LogCallback | None, message: str) -> None:
    if log_callback is not None:
        log_callback(message)


def describe_litigantes_popup(driver) -> None:
    print("")
    print("Litigantes")
    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        original_handle = ""
    try:
        popup_url = resolve_litigantes_popup_url(driver)
    except Exception as exc:
        print(f"No se pudo abrir Litigantes: {exc}")
        return

    if not popup_url:
        print("No se detecto el enlace de Litigantes.")
        return

    try:
        try:
            _, popup_html = fetch_session_resource_text(driver, popup_url)
        except Exception as exc:
            print(f"No se pudo leer Litigantes: {exc}")
            return

        frame_src = extract_popup_frame_src(popup_html)
        if frame_src:
            try:
                content_type, frame_html = fetch_session_resource_text(driver, frame_src)
            except Exception as exc:
                print(f"No se pudo descargar el contenido de Litigantes: {exc}")
                return
        else:
            frame_html = popup_html

        printed_rows = print_litigantes_table_from_html(frame_html)
        cons_lit_url, cons_lit_reason = open_cons_lit_popup_from_litigantes(
            driver,
            subject_label="DDO.",
            litigantes_popup_url=popup_url,
            log_callback=log_callback,
        )
        if cons_lit_url:
            try:
                _, cons_lit_popup_html = fetch_session_resource_text(driver, cons_lit_url)
                cons_lit_frame_src = extract_popup_frame_src(cons_lit_popup_html)
                cons_lit_html = cons_lit_popup_html
                if cons_lit_frame_src:
                    _, cons_lit_html = fetch_session_resource_text(driver, cons_lit_frame_src)
                if cons_lit_html:
                    print("")
                    print_cons_lit_table_from_html(cons_lit_html)
            except Exception as exc:
                print(f"Cons. Lit. no se pudo leer: {exc}")
        elif cons_lit_reason:
            print(f"Cons. Lit. no se abrio: {cons_lit_reason}")
        if printed_rows:
            return

        print("No se pudo extraer la informacion de Litigantes.")
    finally:
        restore_primary_window(driver, original_handle)


def export_litigantes_popup_html(driver) -> Path:
    popup_url = resolve_litigantes_popup_url(driver)
    if not popup_url:
        raise RuntimeError("No se pudo localizar el popup de Litigantes.")

    dump_dir = dump_popup_html_from_url(driver, "litigantes", popup_url)
    print(f"HTML de Litigantes guardado en: {dump_dir}")
    return dump_dir


def login_to_sitfa(driver, username: str, password: str, log_callback: LogCallback | None = None) -> None:
    start = time.perf_counter()
    emit_log(log_callback, "Login: esperando pantalla inicial")
    wait_for_ready(driver, timeout=10)
    emit_log(log_callback, "Login: enviando credenciales")
    submit_login(driver, username, password)
    wait_for_ready(driver, timeout=30)
    emit_log(log_callback, f"Login completado en {time.perf_counter() - start:.1f}s")


def consult_case(
    driver,
    rit: str,
    log_callback: LogCallback | None = None,
    section_callback: SectionCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, list[dict]]:
    total_start = time.perf_counter()
    case_type, case_number, case_year = parse_rit_value(rit)
    def emit_progress(message: str, percent: float) -> None:
        if progress_callback is not None:
            progress_callback(message, max(0.0, min(100.0, float(percent))))
    emit_log(log_callback, f"Consulta: iniciando para {rit}")
    emit_progress("Preparando busqueda", 5)

    step_start = time.perf_counter()
    emit_log(log_callback, "Consulta: preparando formulario de búsqueda")
    form_mode = ensure_query_form_ready(driver, timeout=15)
    driver.switch_to.default_content()
    emit_log(
        log_callback,
        f"Consulta: formulario listo en {time.perf_counter() - step_start:.1f}s ({'reutilizado' if form_mode == 'reused' else 'navegado'})",
    )

    step_start = time.perf_counter()
    emit_log(log_callback, "Consulta: enviando RIT")
    submit_case_query(driver, case_type, case_number, case_year)
    emit_log(log_callback, f"Consulta: causa cargada en {time.perf_counter() - step_start:.1f}s")

    step_start = time.perf_counter()
    historia = collect_history_rows(driver)
    emit_log(log_callback, f"Historia: {len(historia)} filas en {time.perf_counter() - step_start:.1f}s")
    if section_callback is not None:
        section_callback("historia", historia)
    emit_progress("Historia cargada", 52)

    step_start = time.perf_counter()
    liquidacion = collect_liquidacion_rows(driver)
    emit_log(log_callback, f"Liquidación: {len(liquidacion)} filas en {time.perf_counter() - step_start:.1f}s")
    if section_callback is not None:
        section_callback("liquidacion", liquidacion)
    emit_progress("Liquidacion cargada", 68)

    step_start = time.perf_counter()
    escritos = collect_escritos_resolver_rows(driver)
    emit_log(log_callback, f"Esc. por Resolv.: {len(escritos)} filas en {time.perf_counter() - step_start:.1f}s")
    if section_callback is not None:
        section_callback("escritos", escritos)
    emit_progress("Escritos por resolver cargados", 80)

    step_start = time.perf_counter()
    litigantes, cons_lit = collect_litigantes_and_cons_lit_rows(driver, subject_label="DDO.", log_callback=log_callback)
    emit_log(log_callback, f"Litigantes: {len(litigantes)} filas en {time.perf_counter() - step_start:.1f}s")
    emit_log(log_callback, f"Cons. Lit.: {len(cons_lit)} filas reutilizando Litigantes")
    if section_callback is not None:
        section_callback("litigantes", litigantes)
        section_callback("cons_lit", cons_lit)
    emit_progress("Litigantes y Cons. Lit. cargados", 95)

    emit_log(log_callback, f"Consulta total terminada en {time.perf_counter() - total_start:.1f}s")
    emit_progress("Consulta terminada", 100)
    return {
        "historia": historia,
        "liquidacion": liquidacion,
        "escritos": escritos,
        "litigantes": litigantes,
        "cons_lit": cons_lit,
    }


def consult_history_only(
    driver,
    rit: str,
    log_callback: LogCallback | None = None,
) -> list[dict]:
    total_start = time.perf_counter()
    case_type, case_number, case_year = parse_rit_value(rit)
    original_handle = ""
    temp_handle = ""

    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        original_handle = ""

    try:
        emit_log(log_callback, f"Historia secundaria: iniciando para {rit}")
        before_handles = set()
        try:
            before_handles = set(driver.window_handles)
        except WebDriverException:
            before_handles = set()

        driver.execute_script("window.open(arguments[0], '_blank');", POST_LOGIN_URL)
        wait_start = time.perf_counter()
        while time.perf_counter() - wait_start < 10:
            try:
                current_handles = set(driver.window_handles)
            except WebDriverException:
                current_handles = set()
            new_handles = list(current_handles - before_handles)
            if new_handles:
                temp_handle = new_handles[0]
                break
            time.sleep(0.2)

        if not temp_handle:
            raise RuntimeError("No se pudo abrir una ventana temporal para consultar la historia.")

        driver.switch_to.window(temp_handle)
        driver.switch_to.default_content()
        wait_for_ready(driver, timeout=10)
        emit_log(log_callback, "Historia secundaria: ventana temporal lista")

        step_start = time.perf_counter()
        emit_log(log_callback, "Historia secundaria: preparando formulario de búsqueda")
        form_mode = ensure_query_form_ready(driver, timeout=15)
        driver.switch_to.default_content()
        emit_log(
            log_callback,
            f"Historia secundaria: formulario listo en {time.perf_counter() - step_start:.1f}s ({'reutilizado' if form_mode == 'reused' else 'navegado'})",
        )

        step_start = time.perf_counter()
        emit_log(log_callback, "Historia secundaria: enviando RIT")
        submit_case_query(driver, case_type, case_number, case_year)
        emit_log(log_callback, f"Historia secundaria: causa cargada en {time.perf_counter() - step_start:.1f}s")

        step_start = time.perf_counter()
        historia = collect_history_rows(driver)
        emit_log(log_callback, f"Historia secundaria: {len(historia)} filas en {time.perf_counter() - step_start:.1f}s")
        emit_log(log_callback, f"Historia secundaria total terminada en {time.perf_counter() - total_start:.1f}s")
        return historia
    finally:
        if temp_handle:
            try:
                driver.switch_to.window(temp_handle)
                driver.close()
            except WebDriverException:
                pass
        if original_handle:
            try:
                driver.switch_to.window(original_handle)
                driver.switch_to.default_content()
                wait_for_ready(driver, timeout=5)
            except WebDriverException:
                pass


def download_pdf_with_session(driver, pdf_url: str, output_path: Path) -> Path:
    download_session_resource(driver, pdf_url, output_path)
    return output_path


def download_session_resource(driver, resource_url: str, output_path: Path) -> tuple[str, bytes]:
    absolute_url = urljoin(driver.current_url, resource_url)
    cookies = driver.get_cookies()
    cookie_header = "; ".join(
        f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name") is not None
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/pdf,*/*",
        "Referer": driver.current_url,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    request = Request(absolute_url, headers=headers)
    with urlopen(request, timeout=60) as response:
        content = response.read()
        content_type = response.headers.get_content_type() or ""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return content_type, content


def open_local_file(path: Path) -> bool:
    return send_pdf_to_viewer(path)


def launch_pdf_viewer() -> None:
    if not PDF_VIEWER_SCRIPT.exists():
        return

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [sys.executable, str(PDF_VIEWER_SCRIPT)],
            cwd=str(PDF_VIEWER_SCRIPT.parent),
            creationflags=creationflags,
        )
    except OSError:
        pass


def send_pdf_to_viewer(path: Path) -> bool:
    payload = json.dumps(
        {
            "path": str(path.resolve()),
            "title": path.stem,
        }
    ).encode("utf-8")

    try:
        with socket.create_connection((PDF_VIEWER_HOST, PDF_VIEWER_PORT), timeout=1.0) as conn:
            conn.sendall(payload)
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            response = conn.recv(32)
            return response.strip() == b"OK"
    except OSError:
        launch_pdf_viewer()

    try:
        time.sleep(0.8)
        with socket.create_connection((PDF_VIEWER_HOST, PDF_VIEWER_PORT), timeout=1.0) as conn:
            conn.sendall(payload)
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            response = conn.recv(32)
            return response.strip() == b"OK"
    except OSError:
        return False


def download_and_open_pdf(driver, pdf_path: str, output_path: Path) -> Path:
    downloaded_path = download_pdf_with_session(driver, pdf_path, output_path)
    viewer_handled = open_local_file(downloaded_path)
    if viewer_handled:
        try:
            downloaded_path.unlink(missing_ok=True)
        except OSError:
            pass
    return downloaded_path


def prompt_history_row_to_open(driver, history_rows: list[dict]) -> None:
    if not history_rows:
        return

    while True:
        choice = input("Numero de fila para abrir PDF (q para salir): ").strip().lower()
        if not choice:
            continue
        if choice == "q":
            return
        if not choice.isdigit():
            print("Numero invalido.")
            continue

        selected_index = int(choice)
        selected = next((item for item in history_rows if item["index"] == selected_index), None)
        if not selected:
            print("La fila no existe.")
            continue
        if not selected["pdf_url"]:
            print("Esa fila no tiene PDF asociado.")
            continue

        output_path = Path.cwd() / f"historia_fila_{selected_index}.pdf"
        try:
            downloaded_path = download_and_open_pdf(driver, selected["pdf_url"], output_path)
        except Exception as exc:
            print(f"No se pudo descargar u abrir el PDF: {exc}")
            continue

        print(f"PDF descargado y abierto: {downloaded_path}")


def prompt_liquidacion_row_to_open(driver, liquidacion_rows: list[dict]) -> None:
    if not liquidacion_rows:
        return

    while True:
        choice = input("Numero de fila de liquidacion para abrir PDF (q para salir): ").strip().lower()
        if not choice:
            continue
        if choice == "q":
            return
        if not choice.isdigit():
            print("Numero invalido.")
            continue

        selected_index = int(choice)
        selected = next((item for item in liquidacion_rows if item["index"] == selected_index), None)
        if not selected:
            print("La fila no existe.")
            continue
        if not selected["pdf_url"]:
            print("Esa fila no tiene PDF asociado.")
            continue

        output_path = Path.cwd() / f"liquidacion_fila_{selected_index}.pdf"
        try:
            downloaded_path = download_and_open_pdf(driver, selected["pdf_url"], output_path)
        except Exception as exc:
            print(f"No se pudo descargar u abrir el PDF: {exc}")
            continue

        print(f"PDF descargado y abierto: {downloaded_path}")


def prompt_section_selection(
    driver,
    history_rows: list[dict],
    liquidacion_rows: list[dict],
    escritos_resolver_rows: list[dict],
) -> None:
    while True:
        choice = input("Seccion: h=Historia, l=Liquidacion, e=Esc.por Resolv., g=Litigantes, q=Salir: ").strip().lower()
        if not choice:
            continue
        if choice == "q":
            return
        if choice == "h":
            prompt_history_row_to_open(driver, history_rows)
            continue
        if choice == "l":
            prompt_liquidacion_row_to_open(driver, liquidacion_rows)
            continue
        if choice == "e":
            prompt_escritos_resolver_row_to_open(driver, escritos_resolver_rows)
            continue
        if choice == "g":
            describe_litigantes_popup(driver)
            continue
        print("Opcion invalida.")


def submit_case_query(driver, case_type: str, case_number: str, case_year: str) -> None:
    switch_to_frame_with_form(driver, "TramitarPpalForm", timeout=20)
    target_scope = find_case_type_scope(driver, case_type)
    if target_scope and target_scope.get("frame_path"):
        frame_path = [int(part) for part in target_scope.get("frame_path") or []]
        if frame_path:
            switch_to_frame_path(driver, frame_path)

    if case_type.upper() == "M":
        submit_query_form_direct(driver, case_type, case_number, case_year)
        driver.switch_to.default_content()
        wait_for_ready(driver, timeout=30)
        return

    wait_for_case_type_option(driver, case_type, timeout=20)

    try:
        driver.execute_script(
            """
            var data = arguments[0];
            var form = document.forms["TramitarPpalForm"];
            if (!form) {
                throw new Error("No se encontro TramitarPpalForm");
            }

            var setSelect = function(select, value) {
                var found = false;
                for (var i = 0; i < select.options.length; i++) {
                    if (select.options[i].value === value || select.options[i].text === value) {
                        select.selectedIndex = i;
                        select.value = select.options[i].value;
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    throw new Error("No existe la opcion " + value + " en " + select.name);
                }
                if (select.fireEvent) {
                    select.fireEvent("onchange");
                } else if (typeof select.onchange === "function") {
                    select.onchange();
                }
            };

            setSelect(form.elements["TIP_Causa"], data.caseType);
            form.elements["ROL_Causa"].value = data.caseNumber;
            setSelect(form.elements["ERA_Causa"], data.caseYear);

            return {
                caseType: form.elements["TIP_Causa"].value,
                caseNumber: form.elements["ROL_Causa"].value,
                caseYear: form.elements["ERA_Causa"].value
            };
            """,
            {"caseType": case_type, "caseNumber": case_number, "caseYear": case_year},
        )
    except Exception as exc:
        if case_type != "M":
            available_case_types = get_case_type_options(driver)
            available_summary = ", ".join(
                f"{item.get('value', '')}:{item.get('text', '')}" for item in available_case_types if item
            )
            raise RuntimeError(
                f"No se pudo seleccionar el tipo de causa '{case_type}'. Opciones disponibles: {available_summary or 'ninguna'}."
            ) from exc

        submit_query_form_direct(driver, case_type, case_number, case_year)
        driver.switch_to.default_content()
        wait_for_ready(driver, timeout=30)
        return

    driver.execute_script(
        """
        var form = document.forms["TramitarPpalForm"];
        if (!form) {
            throw new Error("No se encontro TramitarPpalForm");
        }
        if (typeof window.ValidaCampos === "function" && window.ValidaCampos() === false) {
            throw new Error("ValidaCampos rechazo el RIT");
        }
        window.setTimeout(function() { form.submit(); }, 0);
        return true;
        """
    )
    driver.switch_to.default_content()
    wait_for_ready(driver, timeout=30)


def extract_tables_from_current_frame(driver) -> list[dict]:
    return driver.execute_script(
        """
        var tables = [];
        var normalize = function(value) {
            return String(value || "").replace(/\\s+/g, " ").replace(/^\\s+|\\s+$/g, "");
        };
        var nodes = document.getElementsByTagName("table");

        for (var tableIndex = 0; tableIndex < nodes.length; tableIndex++) {
            var table = nodes[tableIndex];
            var rows = [];
            var trs = table.getElementsByTagName("tr");

            for (var rowIndex = 0; rowIndex < trs.length; rowIndex++) {
                var cells = [];
                var isHeader = false;
                var children = trs[rowIndex].children;
                for (var cellIndex = 0; cellIndex < children.length; cellIndex++) {
                    var tag = children[cellIndex].tagName.toLowerCase();
                    if (tag === "td" || tag === "th") {
                        if (tag === "th") {
                            isHeader = true;
                        }
                        cells.push(normalize(children[cellIndex].innerText || children[cellIndex].textContent));
                    }
                }
                if (cells.length) {
                    rows.push({
                        cells: cells,
                        is_header: isHeader
                    });
                }
            }

            var maxColumns = 0;
            for (var i = 0; i < rows.length; i++) {
                if (rows[i].cells.length > maxColumns) {
                    maxColumns = rows[i].cells.length;
                }
            }

            tables.push({
                table_index: tableIndex + 1,
                id: table.id || "",
                class_name: table.className || "",
                rows: rows.length,
                columns: maxColumns,
                sample: rows.slice(0, 5)
            });
        }

        return tables;
        """
    )


def normalize_table_cell(value: object) -> str:
    return " ".join(str(value or "").split())


def format_table_rows(rows: list[list[str]], max_column_width: int = 34, max_rows: int = 30) -> list[str]:
    if not rows:
        return []

    limited_rows = rows[:max_rows]
    column_count = max(len(row) for row in limited_rows)
    widths = [0] * column_count

    for row in limited_rows:
        for column_index in range(column_count):
            cell = normalize_table_cell(row[column_index]) if column_index < len(row) else ""
            widths[column_index] = min(max(widths[column_index], len(cell)), max_column_width)

    widths = [max(width, 3) for width in widths]

    def clip(text: str, width: int) -> str:
        if len(text) <= width:
            return text
        if width <= 1:
            return text[:width]
        return text[: width - 1] + "…"

    lines = []
    separator = "-+-".join("-" * width for width in widths)
    for row_index, row in enumerate(limited_rows):
        rendered_cells = []
        for column_index in range(column_count):
            cell = normalize_table_cell(row[column_index]) if column_index < len(row) else ""
            rendered_cells.append(clip(cell, widths[column_index]).ljust(widths[column_index]))
        lines.append(" | ".join(rendered_cells).rstrip())
        if row_index == 0 and len(limited_rows) > 1:
            lines.append(separator)

    if len(rows) > max_rows:
        lines.append(f"... {len(rows) - max_rows} filas mas")

    return lines


def list_result_tables(driver) -> list[dict]:
    results = []
    driver.switch_to.default_content()

    frame_scopes = [{"frame_index": "0", "frame_name": ""}]
    frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
    for index, frame in enumerate(frames, start=1):
        frame_scopes.append(
            {
                "frame_index": str(index),
                "frame_name": frame.get_attribute("name") or frame.get_attribute("id") or "",
            }
        )

    for frame_scope in frame_scopes:
        driver.switch_to.default_content()
        if frame_scope["frame_index"] != "0":
            frame_position = int(frame_scope["frame_index"]) - 1
            try:
                driver.switch_to.frame(frames[frame_position])
            except WebDriverException as exc:
                results.append({**frame_scope, "error": str(exc), "tables": []})
                continue

        try:
            tables = extract_tables_from_current_frame(driver)
        except WebDriverException as exc:
            results.append({**frame_scope, "error": str(exc), "tables": []})
            continue

        results.append({**frame_scope, "tables": tables})

    driver.switch_to.default_content()

    print("")
    print("Tablas encontradas:")
    found_any = False
    for frame_scope in results:
        for table in frame_scope.get("tables", []):
            found_any = True
            frame_label = frame_scope["frame_name"] or frame_scope["frame_index"]
            print(
                f"  Frame {frame_label} | Tabla {table['table_index']} "
                f"id={table['id']!r} class={table['class_name']!r} "
                f"filas={table['rows']} columnas={table['columns']}"
            )
            sample_rows = [item.get("cells", []) for item in table.get("sample", [])]
            if sample_rows:
                for line in format_table_rows(sample_rows, max_column_width=24, max_rows=5):
                    print(f"    {line}")

    if not found_any:
        print("  No encontre tablas en el documento principal ni en frames directos.")

    return results


def find_action_by_label(actions: list[dict], labels: tuple[str, ...]) -> dict | None:
    normalized_labels = [label.strip().lower() for label in labels if label.strip()]
    for action in actions:
        label = (action.get("label") or "").strip().lower()
        if not label:
            continue
        if any(token in label for token in normalized_labels):
            return action
    return None


def read_frame_text(driver) -> str:
    try:
        body_elements = driver.find_elements(By.TAG_NAME, "body")
    except WebDriverException:
        return ""

    if not body_elements:
        return ""

    try:
        return (body_elements[0].text or "").strip()
    except WebDriverException:
        return ""


def html_source_to_text(source: str) -> str:
    cleaned = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", source or "")
    cleaned = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", cleaned)
    cleaned = re.sub(r"(?is)<br\\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?is)</(p|div|tr|li|table|thead|tbody|tfoot|h[1-6])>", "\n", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s+\n", "\n\n", cleaned)
    return cleaned.strip()


def read_frame_source_text(driver) -> str:
    try:
        source = driver.page_source or ""
    except WebDriverException:
        source = ""

    if not source:
        try:
            source = driver.execute_script("return document.documentElement.outerHTML || document.body.outerHTML || '';") or ""
        except WebDriverException:
            source = ""

    return html_source_to_text(source)


def extract_popup_frame_src(source: str) -> str:
    if not source:
        return ""

    match = re.search(r'<iframe[^>]*\bid\s*=\s*["\']?body["\']?[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', source, re.I)
    if not match:
        match = re.search(r'<iframe[^>]*\bname\s*=\s*["\']?body["\']?[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', source, re.I)
    if match:
        return html.unescape(match.group(1))

    # Fallback: if there is a single iframe or a known popup URL pattern, use it.
    candidates = re.findall(r'<iframe[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', source, re.I)
    if len(candidates) == 1:
        return html.unescape(candidates[0])
    for candidate in candidates:
        if "PopUpPpalB4.jsp" in candidate or "tipo_popUp" in candidate or "PopUp" in candidate:
            return html.unescape(candidate)
    return ""


def fetch_session_resource_text(driver, resource_url: str) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(prefix="sitfa_", suffix=".html", delete=False) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        content_type, content = download_session_resource(driver, resource_url, temp_path)
        try:
            if "text" in (content_type or "").lower() or content.lstrip().startswith(b"<"):
                return content_type, content.decode("utf-8", errors="replace")
            return content_type, content.decode("latin-1", errors="replace")
        except Exception:
            return content_type, str(content)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def fetch_session_resource_bytes(driver, resource_url: str) -> tuple[str, bytes]:
    with tempfile.NamedTemporaryFile(prefix="sitfa_", suffix=".bin", delete=False) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        return download_session_resource(driver, resource_url, temp_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


class TableSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self._current_table: dict | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._current_cell_tag: str | None = None
        self._current_row_kind: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "table":
            self._current_table = {
                "id": attributes.get("id", ""),
                "class_name": attributes.get("class", ""),
                "rows_data": [],
            }
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
            self._current_row_kind = "data"
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._current_cell_tag = tag
            if tag == "th":
                self._current_row_kind = "header"

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            cell_text = " ".join("".join(self._current_cell).split())
            self._current_row.append(cell_text)
            self._current_cell = None
            self._current_cell_tag = None
        elif tag == "tr" and self._current_table is not None and self._current_row is not None:
            if any(cell.strip() for cell in self._current_row):
                self._current_table["rows_data"].append(
                    {
                        "kind": self._current_row_kind or "data",
                        "cells": self._current_row,
                    }
                )
            self._current_row = None
            self._current_row_kind = None
        elif tag == "table" and self._current_table is not None:
            rows_data = self._current_table.pop("rows_data", [])
            max_columns = max((len(row["cells"]) for row in rows_data), default=0)
            self.tables.append(
                {
                    "table_index": len(self.tables) + 1,
                    "id": self._current_table.get("id", ""),
                    "class_name": self._current_table.get("class_name", ""),
                    "rows": len(rows_data),
                    "columns": max_columns,
                    "rows_data": rows_data,
                    "sample": [{"cells": row["cells"]} for row in rows_data[:5]],
                }
            )
            self._current_table = None


def extract_tables_from_html_source(source: str) -> list[dict]:
    parser = TableSourceParser()
    try:
        parser.feed(source or "")
        parser.close()
    except Exception:
        return []
    return parser.tables


def normalize_header_name(value: str) -> str:
    value = value or ""
    try:
        value = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    value = value.replace("�", "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", "", value.lower())
    value = value.replace("razonsocial", "raznsocial")
    return value


def normalize_litigantes_value(value: str) -> str:
    value = value or ""
    try:
        value = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    value = " ".join(value.replace("\xa0", " ").split())
    value = value.replace("\ufffd", "")
    value = re.sub(r"\bvar\s+[a-zA-Z_]\w*\s*=.*$", "", value).strip(" ;,|-")
    return value


def extract_litigantes_rows_from_pdf_text(source: str) -> list[dict]:
    if not source:
        return []

    lines = [normalize_litigantes_value(line) for line in source.splitlines()]
    lines = [line for line in lines if line and line != "%PDF-1.4"]
    if not any(line.lower().startswith("litigante:") for line in lines):
        return []

    records: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush_current() -> None:
        nonlocal current
        if current:
            records.append(current)
            current = {}

    for line in lines:
        normalized = line.lower()
        if normalized.startswith("datos litigante"):
            continue
        if set(line) == {"_"}:
            flush_current()
            continue
        if ":" not in line:
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        key_norm = normalize_header_name(key)

        if key_norm == normalize_header_name("Litigante"):
            if current:
                flush_current()
            current["Sujeto"] = value
            continue

        if key_norm == normalize_header_name("Nombre"):
            current["Nombre o Razón Social"] = value
        elif key_norm == normalize_header_name("RUT"):
            current["Rut/Pasaporte"] = value
        elif key_norm == normalize_header_name("Fecha de Nacimiento"):
            current["Fec. Nacimiento"] = value
        elif key_norm == normalize_header_name("Dirección"):
            current.setdefault("Direccion", value)
        elif key_norm == normalize_header_name("Correo Electrónico"):
            current.setdefault("Correo Electrónico", value)

    flush_current()

    result_rows: list[dict] = []
    today = datetime.now().date()
    for record in records:
        subject = normalize_litigantes_value(record.get("Sujeto", ""))
        rut = normalize_litigantes_value(record.get("Rut/Pasaporte", ""))
        name = normalize_litigantes_value(record.get("Nombre o Razón Social", ""))
        birth = normalize_litigantes_value(record.get("Fec. Nacimiento", ""))
        age = ""
        try:
            birth_date = datetime.strptime(birth, "%d/%m/%Y").date()
            age = str(today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day)))
        except Exception:
            age = ""

        if not rut and not name:
            continue

        result_rows.append(
            {
                "Sujeto": subject,
                "Rut/Pasaporte": rut,
                "Nombre o Razón Social": name,
                "Fec. Nacimiento": birth,
                "Edad": age,
            }
        )

    return result_rows


def extract_litigantes_rows_from_table(table: dict, wanted_headers: list[str]) -> list[dict]:
    rows_data = table.get("rows_data") or []
    if not rows_data:
        return []

    header_row_index = None
    header_cells: list[str] = []
    for index, row in enumerate(rows_data):
        cells = row.get("cells") or []
        if row.get("kind") == "header" and cells:
            header_row_index = index
            header_cells = cells
            break

    if not header_cells and rows_data:
        first_row_cells = rows_data[0].get("cells") or []
        if first_row_cells:
            header_row_index = 0
            header_cells = first_row_cells

    if not header_cells:
        return []

    normalized_headers = [normalize_header_name(cell) for cell in header_cells]
    wanted_normalized = [normalize_header_name(header) for header in wanted_headers]
    wanted_variants = {
        normalize_header_name("Nombre o Razón Social"),
        normalize_header_name("Nombre o Razon Social"),
    }

    column_indexes: list[int] = []
    for wanted in wanted_normalized:
        match_index = None
        for index, normalized_header in enumerate(normalized_headers):
            if normalized_header == wanted:
                match_index = index
                break
            if wanted in wanted_variants and normalized_header in wanted_variants:
                match_index = index
                break
            if wanted and (wanted in normalized_header or normalized_header in wanted):
                match_index = index
                break
        if match_index is None:
            return []
        column_indexes.append(match_index)

    result_rows: list[dict] = []
    for row in rows_data[header_row_index + 1 if header_row_index is not None else 1 :]:
        cells = row.get("cells") or []
        if not any(cell.strip() for cell in cells):
            continue
        extracted = {}
        for header_name, column_index in zip(wanted_headers, column_indexes):
            raw_value = cells[column_index] if column_index < len(cells) else ""
            extracted[header_name] = normalize_litigantes_value(raw_value)
        rut_value = extracted.get("Rut/Pasaporte", "")
        name_value = extracted.get("Nombre o Razón Social", "")
        if not rut_value or not name_value or not re.search(r"\d", rut_value):
            continue
        result_rows.append(extracted)

    return result_rows


def extract_rows_from_table(table: dict, wanted_headers: list[str]) -> list[dict]:
    rows_data = table.get("rows_data") or []
    if not rows_data:
        return []

    header_row_index = None
    header_cells: list[str] = []
    for index, row in enumerate(rows_data):
        cells = row.get("cells") or []
        if row.get("kind") == "header" and cells:
            header_row_index = index
            header_cells = cells
            break

    if not header_cells and rows_data:
        first_row_cells = rows_data[0].get("cells") or []
        if first_row_cells:
            header_row_index = 0
            header_cells = first_row_cells

    if not header_cells:
        return []

    normalized_headers = [normalize_header_name(cell) for cell in header_cells]
    wanted_normalized = [normalize_header_name(header) for header in wanted_headers]

    column_indexes: list[int] = []
    for wanted in wanted_normalized:
        match_index = None
        for index, normalized_header in enumerate(normalized_headers):
            if normalized_header == wanted:
                match_index = index
                break
            if wanted and (wanted in normalized_header or normalized_header in wanted):
                match_index = index
                break
        if match_index is None:
            return []
        column_indexes.append(match_index)

    result_rows: list[dict] = []
    for row in rows_data[header_row_index + 1 if header_row_index is not None else 1 :]:
        cells = row.get("cells") or []
        if not any(cell.strip() for cell in cells):
            continue
        extracted = {}
        for header_name, column_index in zip(wanted_headers, column_indexes):
            raw_value = cells[column_index] if column_index < len(cells) else ""
            extracted[header_name] = normalize_litigantes_value(raw_value)
        result_rows.append(extracted)

    return result_rows


def extract_litigantes_rows_from_html(source: str) -> list[dict]:
    wanted_headers = [
        "Sujeto",
        "Rut/Pasaporte",
        "Nombre o Raz\u00f3n Social",
        "Fec. Nacimiento",
        "Edad",
    ]
    best_rows: list[dict] = []
    for table in extract_tables_from_html_source(source):
        table_rows = extract_litigantes_rows_from_table(table, wanted_headers)
        if len(table_rows) > len(best_rows):
            best_rows = table_rows
    if best_rows:
        return best_rows

    text_rows = extract_litigantes_rows_from_pdf_text(source)
    if text_rows:
        return text_rows
    return best_rows


def extract_litigantes_rows_from_dom(driver, log_callback: LogCallback | None = None) -> list[dict]:
    try:
        result = driver.execute_script(LITIGANTES_DOM_EXTRACTOR_SCRIPT)
    except WebDriverException as exc:
        emit_log(log_callback, f"Litigantes: DOM extractor fallo: {exc}")
        return []

    if not result or not isinstance(result, dict):
        return []

    rows = result.get("rows") or []
    if not rows:
        return []

    header_names = result.get("headers") or []
    wanted_headers = [
        "Sujeto",
        "Rut/Pasaporte",
        "Nombre o Razón Social",
        "Fec. Nacimiento",
        "Edad",
    ]
    extracted: list[dict] = []

    if header_names:
        normalized_headers = [normalize_header_name(str(header)) for header in header_names]
        header_indexes: list[int] = []
        for wanted in wanted_headers:
            normalized_wanted = normalize_header_name(wanted)
            match_index = None
            for index, normalized_header in enumerate(normalized_headers):
                if normalized_header == normalized_wanted or normalized_wanted in normalized_header or normalized_header in normalized_wanted:
                    match_index = index
                    break
            if match_index is None:
                return []
            header_indexes.append(match_index)

        for row in rows:
            if isinstance(row, dict):
                cells = [str(row.get(header, "")) for header in header_names]
            else:
                cells = [str(item) for item in row]
            values = {}
            for header_name, column_index in zip(wanted_headers, header_indexes):
                values[header_name] = normalize_litigantes_value(cells[column_index] if column_index < len(cells) else "")
            rut_value = values.get("Rut/Pasaporte", "")
            name_value = values.get("Nombre o Razón Social", "")
            if not rut_value or not name_value or not re.search(r"\d", rut_value):
                continue
            extracted.append(values)
    else:
        for row in rows:
            if isinstance(row, dict):
                cells = [str(value) for value in row.values()]
            else:
                cells = [str(item) for item in row]
            if len(cells) < 2:
                continue
            values = {
                "Sujeto": normalize_litigantes_value(cells[0] if len(cells) > 0 else ""),
                "Rut/Pasaporte": normalize_litigantes_value(cells[1] if len(cells) > 1 else ""),
                "Nombre o Razón Social": normalize_litigantes_value(cells[2] if len(cells) > 2 else ""),
                "Fec. Nacimiento": normalize_litigantes_value(cells[3] if len(cells) > 3 else ""),
                "Edad": normalize_litigantes_value(cells[4] if len(cells) > 4 else ""),
            }
            rut_value = values.get("Rut/Pasaporte", "")
            name_value = values.get("Nombre o Razón Social", "")
            if not rut_value or not name_value or not re.search(r"\d", rut_value):
                continue
            extracted.append(values)

    emit_log(log_callback, f"Litigantes: DOM extractor encontro {len(extracted)} filas")
    return extracted

def print_litigantes_table_from_html(source: str) -> bool:
    wanted_headers = [
        "Sujeto",
        "Rut/Pasaporte",
        "Nombre o Raz\u00f3n Social",
        "Fec. Nacimiento",
        "Edad",
    ]
    best_rows = extract_litigantes_rows_from_html(source)
    if not best_rows:
        return False

    seen = set()
    printed = 0
    for row in best_rows:
        values = [normalize_litigantes_value(row.get(header, "")) for header in wanted_headers]
        key = tuple(values)
        if key in seen or not any(values):
            continue
        seen.add(key)
        printed += 1
        print(f"{printed}. {' | '.join(values)}")
    return printed > 0


def extract_cons_lit_rows_from_html(source: str) -> list[dict]:
    source_headers = [
        "RIT",
        "Fec. Ing.",
        "Fec. lt. trmite",
        "Tribunal",
        "Materia(Trmino)",
    ]
    output_headers = [
        "RIT",
        "Fec. Ing.",
        "Fec. Últ. trámite",
        "Tribunal",
        "Materia(Término)",
    ]
    best_rows: list[dict] = []
    for table in extract_tables_from_html_source(source):
        raw_rows = extract_rows_from_table(table, source_headers)
        table_rows = []
        for raw_row in raw_rows:
            normalized_row = {}
            for source_header, output_header in zip(source_headers, output_headers):
                normalized_row[output_header] = raw_row.get(source_header, "")
            table_rows.append(normalized_row)
        if len(table_rows) > len(best_rows):
            best_rows = table_rows
    return best_rows


def _looks_like_rit_value(value: str) -> bool:
    parts = [part.strip() for part in (value or "").upper().split("-")]
    return len(parts) == 3 and bool(parts[0]) and bool(parts[1]) and bool(parts[2]) and parts[2].isdigit()


def extract_pending_case_rows_from_html(source: str) -> list[dict]:
    best_rows: list[dict] = []
    best_score = 0
    hidden_headers = {
        normalize_header_name("Sel."),
        normalize_header_name("Doc."),
        normalize_header_name("Tipo trámite"),
        normalize_header_name("Funcionario"),
        normalize_header_name("Obs. Devuelto"),
    }

    for table in extract_tables_from_html_source(source):
        rows_data = table.get("rows_data") or []
        if not rows_data:
            continue

        header_row_index = None
        header_cells: list[str] = []
        for index, row in enumerate(rows_data):
            cells = row.get("cells") or []
            if row.get("kind") == "header" and cells:
                header_row_index = index
                header_cells = cells
                break

        if not header_cells and rows_data:
            first_row_cells = rows_data[0].get("cells") or []
            if first_row_cells:
                header_row_index = 0
                header_cells = first_row_cells

        if not header_cells:
            continue

        normalized_headers = [normalize_header_name(cell) for cell in header_cells]
        rit_indexes = [
            index
            for index, header in enumerate(normalized_headers)
            if "rit" in header or "rol" in header or "causa" in header
        ]
        if not rit_indexes:
            continue

        candidate_rows: list[dict] = []
        for row in rows_data[header_row_index + 1 if header_row_index is not None else 1 :]:
            cells = row.get("cells") or []
            if not any(cell.strip() for cell in cells):
                continue

            extracted: dict[str, str] = {}
            for header_name, cell_index in zip(header_cells, range(len(header_cells))):
                raw_value = cells[cell_index] if cell_index < len(cells) else ""
                extracted[header_name] = normalize_litigantes_value(raw_value)

            rit_value = ""
            rit_header_name = ""
            for header_name, value in extracted.items():
                normalized_header = normalize_header_name(header_name)
                if "rit" in normalized_header or "rol" in normalized_header or "causa" in normalized_header:
                    rit_value = value
                    rit_header_name = header_name
                    break

            if not rit_value:
                for value in extracted.values():
                    if _looks_like_rit_value(value):
                        rit_value = value
                        break

            if not rit_value:
                continue

            try:
                rit_type, rit_number, rit_year = parse_rit_value(rit_value)
                rit_value = f"{rit_type}-{rit_number}-{rit_year}"
            except ValueError:
                if not _looks_like_rit_value(rit_value):
                    continue

            normalized_row = {
                key: value
                for key, value in extracted.items()
                if key and normalize_header_name(key) not in hidden_headers
            }
            normalized_row["RIT"] = rit_value
            if rit_header_name and rit_header_name != "RIT":
                normalized_row.setdefault(rit_header_name, rit_value)
            candidate_rows.append(normalized_row)

        score = len(candidate_rows)
        if score > best_score:
            best_score = score
            best_rows = candidate_rows

    return best_rows


def click_pending_cases_all_and_submit(driver) -> None:
    driver.switch_to.default_content()
    driver.execute_script(
        """
        var form = document.forms["TramitarPpalForm"];
        if (!form) {
            throw new Error("No se encontro TramitarPpalForm");
        }

        var radios = form.querySelectorAll('input[type="radio"][name="TIP_ConsultaL"]');
        if (!radios || radios.length < 1) {
            throw new Error("No se encontraron opciones de consulta");
        }

        var selected = false;
        for (var i = 0; i < radios.length; i++) {
            var radio = radios[i];
            var labelText = "";
            if (radio.parentNode) {
                labelText = String(radio.parentNode.textContent || "").replace(/\\s+/g, " ").trim();
            }
            if (i === 0 || /\\bTodas\\b/i.test(labelText)) {
                radio.checked = true;
                selected = true;
            } else {
                radio.checked = false;
            }
        }

        if (!selected) {
            throw new Error("No se pudo marcar la opcion Todas");
        }

        var submitButton = form.querySelector('input[type="submit"][value="Cons.Actuaciones"]');
        if (!submitButton) {
            submitButton = form.querySelector('input[name="irAccionTramitarT"]');
        }
        if (!submitButton) {
            throw new Error("No se encontro el boton Cons.Actuaciones");
        }

        if (typeof submitButton.click === "function") {
            submitButton.click();
        } else {
            submitButton.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        }
        """
    )
    wait_for_ready(driver, timeout=30)


def collect_pending_case_rows(driver, log_callback: LogCallback | None = None) -> list[dict]:
    total_start = time.perf_counter()
    emit_log(log_callback, "Pendientes: abriendo listado de causas")
    driver.get(PENDING_CASES_URL)
    wait_for_ready(driver, timeout=30)

    emit_log(log_callback, "Pendientes: seleccionando Todas y consultando actuaciones")
    click_pending_cases_all_and_submit(driver)

    deadline = time.time() + float(os.getenv("SITFA_WINDOW_WAIT", "8"))
    last_rows: list[dict] = []
    last_url = ""

    while time.time() < deadline:
        scope_results = inspect_window_scopes(driver)
        last_url = driver.current_url
        best_rows: list[dict] = []
        for scope in scope_results:
            scope_rows = extract_pending_case_rows_from_html(scope.get("html") or "")
            if len(scope_rows) > len(best_rows):
                best_rows = scope_rows
        last_rows = best_rows
        if last_rows:
            break
        time.sleep(0.4)

    emit_log(
        log_callback,
        f"Pendientes: {len(last_rows)} filas cargadas en {time.perf_counter() - total_start:.1f}s "
        f"(url={last_url or PENDING_CASES_URL})",
    )
    return last_rows


def print_cons_lit_table_from_html(source: str) -> bool:
    wanted_headers = [
        "RIT",
        "Fec. Ing.",
        "Fec. \u00dalt. tr\u00e1mite",
        "Tribunal",
        "Materia(T\u00e9rmino)",
    ]
    best_rows = extract_cons_lit_rows_from_html(source)
    if not best_rows:
        return False

    print("Cons. Lit.")
    seen = set()
    printed = 0
    for row in best_rows:
        values = [normalize_litigantes_value(row.get(header, "")) for header in wanted_headers]
        key = tuple(values)
        if key in seen or not any(values):
            continue
        seen.add(key)
        printed += 1
        print(f"{printed}. {' | '.join(values)}")
    return printed > 0


def log_cons_lit_rows(log_callback: LogCallback | None, rows: list[dict]) -> None:
    if not rows:
        emit_log(log_callback, "Cons. Lit.: sin filas para mostrar")
        return

    emit_log(log_callback, f"Cons. Lit.: mostrando {len(rows)} filas")
    for index, row in enumerate(rows, start=1):
        rit = normalize_litigantes_value(row.get("RIT", ""))
        fec_ing = normalize_litigantes_value(row.get("Fec. Ing.", ""))
        fec_ult = normalize_litigantes_value(row.get("Fec. Ãšlt. trÃ¡mite", ""))
        tribunal = normalize_litigantes_value(row.get("Tribunal", ""))
        materia = normalize_litigantes_value(row.get("Materia(TÃ©rmino)", ""))
        emit_log(
            log_callback,
            f"Cons. Lit. {index}: {rit} | {fec_ing} | {fec_ult} | {tribunal} | {materia}",
        )


def restore_primary_window(driver, original_handle: str) -> None:
    try:
        handles = list(driver.window_handles)
    except WebDriverException:
        return

    for handle in handles:
        if handle == original_handle:
            continue
        try:
            driver.switch_to.window(handle)
            driver.close()
        except WebDriverException:
            continue

    if not original_handle:
        return

    try:
        driver.switch_to.window(original_handle)
        driver.switch_to.default_content()
        wait_for_ready(driver, timeout=5)
    except WebDriverException:
        return


def dump_popup_html(driver, prefix: str) -> Path:
    scope_results = wait_for_window_scopes_content(driver, timeout=float(os.getenv("SITFA_WINDOW_WAIT", "8")))
    dump_dir = Path("debug_dumps") / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        (dump_dir / "popup_window.html").write_text(driver.page_source or "", encoding="utf-8")
    except WebDriverException:
        pass

    for scope in scope_results:
        scope_index = scope.get("frame_index") or "0"
        html_source = scope.get("html") or ""
        if not html_source:
            continue
        (dump_dir / f"scope_{scope_index}.html").write_text(html_source, encoding="utf-8")

    return dump_dir


def dump_popup_html_from_url(driver, prefix: str, popup_url: str) -> Path:
    dump_dir = Path("debug_dumps") / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    _, popup_html = fetch_session_resource_text(driver, popup_url)
    (dump_dir / "popup_window.html").write_text(popup_html, encoding="utf-8")

    frame_src = extract_popup_frame_src(popup_html)
    if frame_src:
        try:
            _, frame_html = fetch_session_resource_text(driver, frame_src)
            (dump_dir / "scope_1.html").write_text(frame_html, encoding="utf-8")
        except Exception:
            pass

    return dump_dir


def _open_litigantes_action_popup_from_litigantes(
    driver,
    action_target: str,
    subject_label: str = "DDO.",
    litigantes_popup_url: str = "",
    log_callback: LogCallback | None = None,
    target_row_data: dict[str, str] | None = None,
    action_log_prefix: str = "Litigantes action",
) -> tuple[str | None, str]:
    original_handle = ""
    opened_temp_handle = ""
    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        original_handle = ""

    try:
        emit_log(log_callback, f"{action_log_prefix}: inicio de apertura")
        if litigantes_popup_url:
            try:
                resolved_url = litigantes_popup_url
                try:
                    _, wrapper_html = fetch_session_resource_text(driver, litigantes_popup_url)
                    wrapper_frame_src = extract_popup_frame_src(wrapper_html)
                    if wrapper_frame_src:
                        resolved_url = wrapper_frame_src
                        emit_log(log_callback, f"{action_log_prefix}: frame_src resuelto para popup {resolved_url}")
                except Exception as exc:
                    emit_log(log_callback, f"{action_log_prefix}: no se pudo resolver frame_src: {exc}")

                before_handles = set(driver.window_handles)
                driver.execute_script("window.open(arguments[0], '_blank');", resolved_url)
                wait_start = time.perf_counter()
                while time.perf_counter() - wait_start < 10:
                    current_handles = set(driver.window_handles)
                    new_handles = list(current_handles - before_handles)
                    if new_handles:
                        opened_temp_handle = new_handles[0]
                        break
                    time.sleep(0.2)
                if not opened_temp_handle:
                    raise RuntimeError("No se pudo abrir una ventana nueva para Litigantes")
                driver.switch_to.window(opened_temp_handle)
                wait_for_ready(driver, timeout=10)
                emit_log(log_callback, f"{action_log_prefix}: popup de litigantes abierto en ventana temporal")
                scope_results = wait_for_window_scopes_content(driver, timeout=8.0)
                scope_summary = [
                    f"{scope.get('frame_index')}:{(scope.get('frame_name') or '').strip()} "
                    f"text_has_ddo={'DDO.' in ((scope.get('text') or '') + ' ' + (scope.get('html') or '')).upper()}"
                    for scope in scope_results
                ]
                emit_log(log_callback, f"{action_log_prefix}: scopes={scope_summary}")
            except Exception as exc:
                emit_log(log_callback, f"{action_log_prefix}: no se pudo abrir popup temporal: {exc}")
                if opened_temp_handle:
                    try:
                        driver.close()
                    except WebDriverException:
                        pass
                if original_handle:
                    try:
                        driver.switch_to.window(original_handle)
                    except WebDriverException:
                        pass
                return None, "popup_open_failed"

        target_subject = normalize_litigantes_value(subject_label).strip().upper()
        target_values: dict[str, str] = {}
        if target_row_data:
            for key in ("Sujeto", "Rut/Pasaporte", "Nombre o Razón Social", "Fec. Nacimiento"):
                normalized_value = normalize_litigantes_value(str(target_row_data.get(key, ""))).strip().upper()
                if normalized_value:
                    target_values[key] = normalized_value
        emit_log(
            log_callback,
            f"{action_log_prefix}: objetivo sujeto={target_subject!r} campos={list(target_values.keys())}",
        )
        target_row = None
        candidate_count = 0
        fallback_count = 0
        sample_labels: list[str] = []
        try:
            rows = []
            scope_results = wait_for_window_scopes_content(driver, timeout=4.0)
            target_scope = None
            scope_terms = [target_subject] + [value for value in target_values.values() if value]
            for scope in scope_results:
                scope_text = f"{scope.get('text') or ''}\n{scope.get('html') or ''}".upper()
                if any(term and term in scope_text for term in scope_terms):
                    target_scope = scope
                    break

            if target_scope and target_scope.get("frame_path"):
                frame_path = [int(part) for part in target_scope.get("frame_path") or []]
                if frame_path and switch_to_frame_path(driver, frame_path):
                    rows = driver.find_elements(By.TAG_NAME, "tr")
            if not rows:
                rows = driver.find_elements(By.TAG_NAME, "tr")
        except WebDriverException as exc:
            emit_log(log_callback, f"{action_log_prefix}: no se pudieron leer filas: {exc}")
            return None, "row_read_failed"

        emit_log(log_callback, f"{action_log_prefix}: filas visibles={len(rows)}")

        for row in rows:
            try:
                row_cells = row.find_elements(By.TAG_NAME, "td") + row.find_elements(By.TAG_NAME, "th")
            except WebDriverException:
                continue

            cell_values: list[str] = []
            row_values_by_header = {
                "Sujeto": "",
                "Rut/Pasaporte": "",
                "Nombre o Razón Social": "",
                "Fec. Nacimiento": "",
                "Edad": "",
            }
            for cell in row_cells:
                try:
                    cell_value = normalize_litigantes_value(
                        str(cell.text or cell.get_attribute("innerText") or cell.get_attribute("textContent") or "")
                    ).strip().upper()
                except WebDriverException:
                    continue
                if not cell_value:
                    continue
                cell_values.append(cell_value)
            if cell_values:
                row_values_by_header["Sujeto"] = cell_values[0] if len(cell_values) > 0 else ""
                row_values_by_header["Rut/Pasaporte"] = cell_values[1] if len(cell_values) > 1 else ""
                row_values_by_header["Nombre o Razón Social"] = cell_values[2] if len(cell_values) > 2 else ""
                row_values_by_header["Fec. Nacimiento"] = cell_values[3] if len(cell_values) > 3 else ""
                row_values_by_header["Edad"] = cell_values[4] if len(cell_values) > 4 else ""

            exact_match = False
            subject_match = bool(target_subject) and row_values_by_header.get("Sujeto", "") == target_subject
            if target_values:
                exact_match = True
                for key, expected_value in target_values.items():
                    if expected_value and row_values_by_header.get(key, "") != expected_value:
                        exact_match = False
                        break
                if not exact_match and subject_match:
                    exact_match = True
                    fallback_count += 1
            else:
                exact_match = subject_match or any(cell_value == target_subject for cell_value in cell_values)

            if not exact_match:
                continue

            candidate_count += 1
            if len(sample_labels) < 5:
                sample_labels.append(" | ".join(cell_values))
            target_row = row
            break

        emit_log(
            log_callback,
            f"{action_log_prefix}: busqueda objetivo={target_subject} exactos={candidate_count} fallback={fallback_count} samples={sample_labels}",
        )

        if target_row is None:
            try:
                preview_rows = []
                for row in rows[:8]:
                    try:
                        preview = row.text or row.get_attribute("innerText") or row.get_attribute("textContent") or ""
                    except WebDriverException:
                        preview = ""
                    preview = normalize_litigantes_value(preview).strip()
                    if preview:
                        preview_rows.append(preview[:220])
                emit_log(log_callback, f"{action_log_prefix}: primeras filas={preview_rows}")
            except WebDriverException:
                pass
            return None, "row_not_found"

        try:
            driver.execute_script(
                """
                var row = arguments[0];
                if (row) {
                    if (typeof row.scrollIntoView === 'function') {
                        row.scrollIntoView({block: 'center', inline: 'nearest'});
                    }
                    if (typeof row.click === 'function') {
                        row.click();
                    } else {
                        row.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    }
                }
                """,
                target_row,
            )
            emit_log(log_callback, f"{action_log_prefix}: click sobre fila ejecutado")
        except WebDriverException as exc:
            emit_log(log_callback, f"{action_log_prefix}: fallo al clickear fila: {exc}")
            return None, "row_click_failed"

        button = None
        button_candidates = []
        try:
            button_candidates = driver.execute_script(
                """
                function normalize(value) {
                    return String(value || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                }
                var target = arguments[0];
                var elements = Array.from(document.querySelectorAll('button, input, a, span, td, div'));
                var matches = [];
                for (var i = 0; i < elements.length; i++) {
                    var el = elements[i];
                    var text = normalize(el.innerText || el.textContent || el.value || '');
                    var attrs = normalize(
                        (el.getAttribute('id') || '') + ' ' +
                        (el.getAttribute('name') || '') + ' ' +
                        (el.getAttribute('title') || '') + ' ' +
                        (el.getAttribute('aria-label') || '') + ' ' +
                        (el.getAttribute('value') || '')
                    );
                    var visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    if (visible && (text.indexOf(target) >= 0 || attrs.indexOf(target) >= 0)) {
                        matches.push({
                            tag: el.tagName,
                            id: el.id || '',
                            name: el.getAttribute('name') || '',
                            value: el.getAttribute('value') || '',
                            text: (el.innerText || el.textContent || '').trim(),
                            title: el.getAttribute('title') || '',
                            aria: el.getAttribute('aria-label') || ''
                        });
                    }
                }
                return matches;
                """,
                action_target,
            ) or []
        except WebDriverException as exc:
            emit_log(log_callback, f"{action_log_prefix}: error buscando boton {action_target}: {exc}")
            return None, "button_search_failed"

        if button_candidates:
            emit_log(log_callback, f"{action_log_prefix}: candidatos {action_target}={button_candidates[:5]}")
            try:
                button = driver.execute_script(
                    """
                    function normalize(value) {
                        return String(value || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                    }
                    var target = arguments[0];
                    var elements = Array.from(document.querySelectorAll('button, input, a, span, td, div'));
                    for (var i = 0; i < elements.length; i++) {
                        var el = elements[i];
                        var text = normalize(el.innerText || el.textContent || el.value || '');
                        var attrs = normalize(
                            (el.getAttribute('id') || '') + ' ' +
                            (el.getAttribute('name') || '') + ' ' +
                            (el.getAttribute('title') || '') + ' ' +
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('value') || '')
                        );
                        var visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        if (visible && (text.indexOf(target) >= 0 || attrs.indexOf(target) >= 0)) {
                            return el;
                        }
                    }
                    return null;
                    """,
                    action_target,
                )
            except WebDriverException:
                button = None

        if button is None:
            emit_log(log_callback, f"{action_log_prefix}: boton {action_target} no encontrado")
            return None, "button_not_found"

        try:
            disabled_attr = button.get_attribute("disabled")
            if disabled_attr is not None or "disabled" in (button.get_attribute("class") or "").lower():
                emit_log(log_callback, f"{action_log_prefix}: boton {action_target} sigue deshabilitado")
                return None, "button_disabled"
        except WebDriverException:
            pass

        captured_url = ""
        original_dialog = None
        original_open = None
        try:
            captured_url = driver.execute_script(
                """
                var button = arguments[0];
                var capturedUrl = "";
                var originalDialog = window.showModalDialog;
                var originalOpen = window.open;
                try {
                    window.showModalDialog = function(url) {
                        capturedUrl = String(url || "");
                        return null;
                    };
                    window.open = function(url) {
                        capturedUrl = String(url || "");
                        return null;
                    };
                    if (button && typeof button.click === 'function') {
                        button.click();
                    } else if (button) {
                        button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    }
                } finally {
                window.showModalDialog = originalDialog;
                window.open = originalOpen;
            }
            return capturedUrl;
                """,
                button,
            )
        except WebDriverException as exc:
            emit_log(log_callback, f"{action_log_prefix}: fallo al clickear boton {action_target}: {exc}")
            return None, "button_click_failed"

        popup_url = str(captured_url or "").strip()
        emit_log(log_callback, f"{action_log_prefix}: popup {action_target} capturado={bool(popup_url)} url={popup_url!r}")
        if not popup_url:
            return None, "popup_url_missing"
        return popup_url, ""
    finally:
        if opened_temp_handle:
            try:
                driver.close()
            except WebDriverException:
                pass
        if original_handle:
            try:
                driver.switch_to.window(original_handle)
                driver.switch_to.default_content()
                wait_for_ready(driver, timeout=5)
            except WebDriverException:
                pass


def open_cons_lit_popup_from_litigantes(
    driver,
    subject_label: str = "DDO.",
    litigantes_popup_url: str = "",
    log_callback: LogCallback | None = None,
) -> tuple[str | None, str]:
    return _open_litigantes_action_popup_from_litigantes(
        driver,
        action_target="CONS. LIT.",
        subject_label=subject_label,
        litigantes_popup_url=litigantes_popup_url,
        log_callback=log_callback,
        action_log_prefix="Litigantes cons",
    )


def open_cartola_bco_estado_popup_from_litigantes(
    driver,
    selected_litigante: dict[str, str] | None = None,
    subject_label: str = "DDO.",
    litigantes_popup_url: str = "",
    log_callback: LogCallback | None = None,
) -> tuple[str | None, str]:
    derived_subject = subject_label
    if selected_litigante:
        selected_subject = normalize_litigantes_value(str(selected_litigante.get("Sujeto", ""))).strip()
        if selected_subject:
            derived_subject = selected_subject
    return _open_litigantes_action_popup_from_litigantes(
        driver,
        action_target="CARTOLA BCO.ESTADO",
        subject_label=derived_subject,
        litigantes_popup_url=litigantes_popup_url,
        log_callback=log_callback,
        target_row_data=selected_litigante,
        action_log_prefix="Litigantes cartola",
    )


def switch_to_frame_path(driver, frame_path: list[int]) -> bool:
    driver.switch_to.default_content()
    try:
        frames = []
        for frame_index in frame_path:
            frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
            frame_position = frame_index - 1
            if frame_position < 0 or frame_position >= len(frames):
                return False
            driver.switch_to.frame(frames[frame_position])
        return True
    except WebDriverException:
        return False


def inspect_window_scopes(driver) -> list[dict]:
    results: list[dict] = []

    def collect_scope(scope_name: str, frame_path: list[int], frame_src: str = "") -> None:
        html_source = ""
        error = ""
        tables = []
        text = ""

        try:
            text = read_frame_text(driver)
        except WebDriverException as exc:
            error = str(exc)

        try:
            tables = extract_tables_from_current_frame(driver)
        except WebDriverException as exc:
            if not error:
                error = str(exc)
            tables = []

        try:
            html_source = driver.page_source or ""
        except WebDriverException as exc:
            if not error:
                error = str(exc)
            html_source = ""

        if not html_source:
            try:
                html_source = driver.execute_script("return document.documentElement.outerHTML || document.body.outerHTML || '';") or ""
            except WebDriverException:
                html_source = ""

        if frame_src and (not html_source or "<iframe" in html_source.lower()):
            try:
                _, fetched_html = fetch_session_resource_text(driver, frame_src)
                if fetched_html:
                    html_source = fetched_html
                    if not text:
                        text = html_source_to_text(html_source)
                    if not tables:
                        tables = extract_tables_from_html_source(html_source)
            except Exception as exc:
                if not error:
                    error = str(exc)

        if not text:
            text = html_source_to_text(html_source) if html_source else read_frame_source_text(driver)

        results.append(
            {
                "frame_index": "0" if not frame_path else "/".join(str(part) for part in frame_path),
                "frame_path": frame_path[:],
                "frame_depth": len(frame_path),
                "frame_name": scope_name,
                "frame_src": frame_src,
                "error": error,
                "tables": tables,
                "text": text,
                "html": html_source,
            }
        )

        try:
            child_frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        except WebDriverException:
            child_frames = []

        for child_index, child_frame in enumerate(child_frames, start=1):
            child_name = child_frame.get_attribute("name") or child_frame.get_attribute("id") or f"frame_{child_index}"
            child_src = child_frame.get_attribute("src") or ""
            try:
                driver.switch_to.frame(child_frame)
                collect_scope(child_name, frame_path + [child_index], child_src)
            except WebDriverException as exc:
                results.append(
                    {
                        "frame_index": "/".join(str(part) for part in (frame_path + [child_index])),
                        "frame_path": frame_path + [child_index],
                        "frame_depth": len(frame_path) + 1,
                        "frame_name": child_name,
                        "frame_src": child_src,
                        "error": str(exc),
                        "tables": [],
                        "text": "",
                        "html": "",
                    }
                )
            finally:
                try:
                    driver.switch_to.parent_frame()
                except WebDriverException:
                    driver.switch_to.default_content()

    driver.switch_to.default_content()
    collect_scope("documento principal", [])
    driver.switch_to.default_content()
    return results


def wait_for_window_scopes_content(driver, timeout: float = 8.0) -> list[dict]:
    deadline = time.time() + timeout
    last_results: list[dict] = []

    while time.time() < deadline:
        last_results = inspect_window_scopes(driver)
        for scope in last_results:
            scope_text = f"{scope.get('text') or ''}\n{scope.get('html') or ''}".upper()
            if scope.get("tables") or "DDO." in scope_text:
                return last_results
        time.sleep(0.4)

    return last_results


def dump_litigantes_popup_html(driver, scope_results: list[dict]) -> Path:
    dump_dir = Path("debug_dumps") / f"litigantes_{time.strftime('%Y%m%d_%H%M%S')}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        (dump_dir / "popup_window.html").write_text(driver.page_source or "", encoding="utf-8")
    except WebDriverException:
        pass

    for scope in scope_results:
        scope_index = scope.get("frame_index") or "0"
        html_source = scope.get("html") or ""
        if not html_source:
            continue
        (dump_dir / f"scope_{scope_index}.html").write_text(html_source, encoding="utf-8")

    return dump_dir




def submit_login(driver, username: str, password: str) -> None:
    wait_for_form(driver, "InicioAplicacionForm", timeout=20)
    data = {
        "username": username,
        "password": password,
        "machineName": socket.gethostname() or "0",
        "windowsUser": getpass.getuser() or "0",
        "localIp": get_local_ip(),
        "macAddress": get_mac_address(),
    }
    driver.execute_script(FILL_LOGIN_SCRIPT, data)
    driver.execute_script(SUBMIT_LOGIN_SCRIPT)


def inspect_interactive_elements(driver) -> list[dict]:
    results = []

    driver.switch_to.default_content()
    frame_count = len(driver.find_elements(By.TAG_NAME, "frame")) + len(driver.find_elements(By.TAG_NAME, "iframe"))
    frame_indexes = ["0"] + [str(index) for index in range(1, frame_count + 1)]

    for frame_index in frame_indexes:
        driver.switch_to.default_content()
        frame_name = ""
        if frame_index != "0":
            try:
                frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                frame = frames[int(frame_index) - 1]
                frame_name = frame.get_attribute("name") or ""
                driver.switch_to.frame(frame)
            except WebDriverException as exc:
                results.append(
                    {
                        "frame_index": frame_index,
                        "frame_name": frame_name,
                        "url": "",
                        "title": "",
                        "elements": [],
                        "error": str(exc),
                    }
                )
                continue

        try:
            frame_result = driver.execute_script(ELEMENTS_SCRIPT)
        except WebDriverException as exc:
            frame_result = {"title": "", "url": driver.current_url, "elements": [], "error": str(exc)}

        frame_result["frame_index"] = frame_index
        frame_result["frame_name"] = frame_name
        results.append(frame_result)

    driver.switch_to.default_content()
    return results


def safe_filename(value: str, fallback: str = "frame") -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
        else:
            cleaned.append("_")

    name = "".join(cleaned).strip("_")
    return name[:80] or fallback


def current_html(driver) -> str:
    return driver.execute_script("return document.documentElement.outerHTML;") or ""


def dump_current_state(driver) -> Path:
    dump_dir = Path("debug_dumps") / time.strftime("%Y%m%d_%H%M%S")
    dump_dir.mkdir(parents=True, exist_ok=True)

    driver.switch_to.default_content()
    metadata = {
        "current_url": driver.current_url,
        "title": driver.title,
        "window_handles": driver.window_handles,
    }

    try:
        (dump_dir / "root.html").write_text(current_html(driver), encoding="utf-8")
    except WebDriverException as exc:
        metadata["root_error"] = str(exc)

    try:
        driver.save_screenshot(str((dump_dir / "screenshot.png").resolve()))
    except WebDriverException as exc:
        metadata["screenshot_error"] = str(exc)

    frames = get_frames_info(driver)
    metadata["frames"] = frames
    for frame in frames:
        driver.switch_to.default_content()
        frame_name = frame.get("name") or frame.get("id") or f"frame_{frame.get('index')}"
        filename = f"{int(frame.get('index') or 0):02d}_{safe_filename(frame_name)}.html"
        try:
            if switch_to_frame_descriptor(driver, frame):
                (dump_dir / filename).write_text(current_html(driver), encoding="utf-8")
        except WebDriverException as exc:
            metadata.setdefault("frame_errors", {})[filename] = str(exc)

    driver.switch_to.default_content()
    try:
        elements = inspect_interactive_elements(driver)
        (dump_dir / "elements.json").write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
    except WebDriverException as exc:
        metadata["elements_error"] = str(exc)

    try:
        actions = get_clickable_actions(driver)
        (dump_dir / "actions.json").write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
    except WebDriverException as exc:
        metadata["actions_error"] = str(exc)

    driver.switch_to.default_content()
    (dump_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Estado actual exportado en: {dump_dir.resolve()}")
    return dump_dir


def get_frames_info(driver, verbose: bool = True) -> list[dict]:
    driver.switch_to.default_content()
    try:
        info = driver.execute_script(FRAMES_INFO_SCRIPT)
    except WebDriverException as exc:
        if verbose:
            print(f"No se pudo leer informacion de frames: {exc}")
        return []

    frames = info.get("frames") or []
    debug_frames = verbose or os.getenv("SITFA_DEBUG_FRAMES", "").strip() == "1"
    if debug_frames:
        Path("frames_debug.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if frames and verbose:
        print("")
        print("Frames detectados:")
        for frame in frames:
            print(
                f"  {frame.get('index')}. "
                f"name={frame.get('name')!r} id={frame.get('id')!r} src={frame.get('src')!r}"
            )
        print("Detalle guardado en: frames_debug.json")
    return frames


def switch_to_frame_descriptor(driver, frame: dict) -> bool:
    driver.switch_to.default_content()
    name = frame.get("name") or ""
    frame_id = frame.get("id") or ""
    index = int(frame.get("index") or 0)

    attempts = []
    if name:
        attempts.append(("name", name))
    if frame_id and frame_id != name:
        attempts.append(("id", frame_id))
    if index:
        attempts.append(("index", index - 1))

    for kind, value in attempts:
        try:
            if kind == "index":
                driver.switch_to.frame(value)
            else:
                driver.switch_to.frame(value)
            return True
        except WebDriverException:
            driver.switch_to.default_content()
            continue

    return False


def get_clickable_actions(driver, verbose: bool = True) -> list[dict]:
    menu = []

    driver.switch_to.default_content()
    frames_info = get_frames_info(driver, verbose=verbose)
    frame_indexes = ["0"] + [str(frame["index"]) for frame in frames_info]
    frames_by_index = {str(frame["index"]): frame for frame in frames_info}

    for frame_index in frame_indexes:
        driver.switch_to.default_content()
        frame_name = ""
        if frame_index != "0":
            frame = frames_by_index.get(frame_index, {})
            frame_name = frame.get("name") or frame.get("id") or ""
            if not switch_to_frame_descriptor(driver, frame):
                if verbose:
                    print(f"No se pudo entrar al frame {frame_index} ({frame_name})")
                continue

        try:
            frame_result = driver.execute_script(CLICKABLES_SCRIPT)
        except WebDriverException as exc:
            if verbose:
                print(f"No se pudieron leer acciones del frame {frame_index}: {exc}")
            continue

        for action in frame_result["actions"]:
            action["frame_index"] = frame_index
            action["frame_name"] = frame_name
            action["frame_url"] = frame_result.get("url", "")
            menu.append(action)

    driver.switch_to.default_content()
    return menu


def print_action_menu(actions: list[dict]) -> None:
    print("")
    print("Acciones disponibles:")
    if not actions:
        print("  No hay botones, links, pestanas o iconos clicables visibles.")
        return

    for index, action in enumerate(actions, start=1):
        label = action.get("label") or "(sin texto)"
        kind = action.get("kind", "")
        type_name = action.get("type") or action.get("role") or ""
        suffix = f" [{kind}{':' + type_name if type_name else ''}]"
        print(f"  {index}. {label}{suffix}")


def switch_to_action_frame(driver, action: dict) -> None:
    driver.switch_to.default_content()
    if action["frame_index"] == "0":
        return

    frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
    frame_position = int(action["frame_index"]) - 1
    if frame_position < 0 or frame_position >= len(frames):
        raise WebDriverException(f"No existe frame {action['frame_index']} en la pagina actual")
    driver.switch_to.frame(frames[frame_position])


def native_click_action(driver, action: dict) -> None:
    selector = action.get("selector") or ""
    if not selector:
        raise WebDriverException("La accion no tiene selector para click nativo")

    try:
        switch_to_action_frame(driver, action)
        element = driver.find_element(By.CSS_SELECTOR, selector)
        element.click()
    finally:
        driver.switch_to.default_content()


def get_frame_location(driver, frame_name: str) -> str:
    driver.switch_to.default_content()
    try:
        driver.switch_to.frame(frame_name)
        return driver.execute_script("return location.href;") or ""
    finally:
        driver.switch_to.default_content()


def navigate_named_frame(driver, frame_name: str, href: str) -> None:
    driver.switch_to.default_content()
    did_navigate = driver.execute_script(
        """
        var frameName = arguments[0];
        var href = arguments[1];
        var frames = [];
        var frameTags = document.getElementsByTagName("frame");
        var iframeTags = document.getElementsByTagName("iframe");

        for (var i = 0; i < frameTags.length; i++) {
            frames.push(frameTags[i]);
        }
        for (var j = 0; j < iframeTags.length; j++) {
            frames.push(iframeTags[j]);
        }

        for (var k = 0; k < frames.length; k++) {
            var frame = frames[k];
            if (frame.getAttribute("name") === frameName || frame.id === frameName) {
                frame.src = href;
                return true;
            }
        }

        return false;
        """,
        frame_name,
        href,
    )

    if not did_navigate:
        raise WebDriverException(f"No encontre el frame destino {frame_name!r}")

    driver.switch_to.frame(frame_name)
    wait_for_ready(driver, timeout=20)
    driver.switch_to.default_content()


def get_window_frames_summary(driver) -> list[dict]:
    try:
        info = driver.execute_script(FRAMES_INFO_SCRIPT)
    except WebDriverException:
        return []

    return info.get("frames") or []


def collect_windows_info(driver) -> list[dict]:
    try:
        handles = driver.window_handles
    except WebDriverException as exc:
        print(f"No se pudieron leer las ventanas abiertas: {exc}")
        return []

    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        original_handle = handles[-1] if handles else ""

    windows = []
    for index, handle in enumerate(handles, start=1):
        try:
            driver.switch_to.window(handle)
            driver.switch_to.default_content()
            windows.append(
                {
                    "index": index,
                    "handle": handle,
                    "current": handle == original_handle,
                    "url": driver.current_url,
                    "title": driver.title,
                    "frames": get_window_frames_summary(driver),
                }
            )
        except WebDriverException as exc:
            windows.append(
                {
                    "index": index,
                    "handle": handle,
                    "current": handle == original_handle,
                    "url": "",
                    "title": "",
                    "frames": [],
                    "error": str(exc),
                }
            )

    if original_handle in handles:
        try:
            driver.switch_to.window(original_handle)
            driver.switch_to.default_content()
        except WebDriverException:
            pass

    return windows


def print_windows_info(driver, verbose: bool = True) -> list[dict]:
    windows = collect_windows_info(driver)
    if not verbose:
        return windows
    print("")
    print("Ventanas abiertas:")
    if not windows:
        print("  No pude leer ventanas abiertas.")
        return windows

    for window in windows:
        marker = "*" if window.get("current") else " "
        url = window.get("url") or "(sin url)"
        title = window.get("title") or "(sin titulo)"
        frames = window.get("frames") or []
        suffix = f" | frames={len(frames)}" if frames else ""
        if window.get("error"):
            suffix += f" | error={window['error']}"
        print(f" {marker} {window['index']}. {title} | {url}{suffix}")
        for frame in frames:
            frame_name = frame.get("name") or frame.get("id") or f"frame_{frame.get('index')}"
            print(f"      frame {frame.get('index')}: {frame_name} -> {frame.get('src') or ''}")

    return windows


def switch_to_window_number(driver, window_number: int) -> bool:
    handles = driver.window_handles
    if window_number < 1 or window_number > len(handles):
        print("Numero de ventana fuera de rango.")
        return False

    driver.switch_to.window(handles[window_number - 1])
    driver.switch_to.default_content()
    print(f"Ventana activa: {window_number} | {driver.current_url}")
    return True


def snapshot_window_urls(driver) -> dict[str, str]:
    try:
        handles = driver.window_handles
    except WebDriverException:
        return {}

    try:
        original_handle = driver.current_window_handle
    except WebDriverException:
        original_handle = handles[-1] if handles else ""

    urls = {}
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            driver.switch_to.default_content()
            urls[handle] = driver.current_url
        except WebDriverException:
            urls[handle] = ""

    if original_handle in handles:
        try:
            driver.switch_to.window(original_handle)
            driver.switch_to.default_content()
        except WebDriverException:
            pass

    return urls


def wait_for_window_update(
    driver,
    before_urls: dict[str, str],
    timeout: float = 8.0,
    verbose: bool = True,
) -> str | None:
    deadline = time.time() + timeout
    before_handles = set(before_urls)
    selected_handle = None
    selected_reason = ""

    while time.time() < deadline:
        time.sleep(0.5)
        after_urls = snapshot_window_urls(driver)
        after_handles = set(after_urls)

        new_handles = [handle for handle in after_handles if handle not in before_handles]
        if new_handles:
            selected_handle = new_handles[-1]
            selected_reason = "nueva ventana"
            break

        changed_handles = [
            handle
            for handle, after_url in after_urls.items()
            if handle in before_urls and after_url and after_url != before_urls.get(handle)
        ]
        if changed_handles:
            selected_handle = changed_handles[-1]
            selected_reason = "ventana actualizada"
            break

    if not selected_handle:
        return None

    driver.switch_to.window(selected_handle)
    driver.switch_to.default_content()
    wait_for_ready(driver, timeout=10)
    if verbose:
        print(f"Detectada {selected_reason}: {driver.current_url}")
    return selected_handle


def wait_for_window_content(driver, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            current_url = (driver.current_url or "").strip()
        except WebDriverException:
            current_url = ""

        if current_url and current_url.lower() != "about:blank":
            try:
                wait_for_ready(driver, timeout=2)
            except TimeoutException:
                pass
            return

        time.sleep(0.3)

    raise TimeoutException("La ventana nueva quedo en blanco.")


def click_action(
    driver,
    action: dict,
    verbose: bool = True,
    log_callback: LogCallback | None = None,
    log_prefix: str = "Accion",
) -> None:
    current_url = driver.current_url
    before_urls = snapshot_window_urls(driver)
    current_handles = set(before_urls)
    href = (action.get("href") or "").strip()
    target = (action.get("target") or "").strip()
    frame_url_before = ""
    frame_url_after = ""

    if verbose:
        print(
            "Accion seleccionada: "
            f"frame={action['frame_index']} selector={action.get('selector')} "
            f"name={action.get('name')!r} value={action.get('value')!r} "
            f"href={action.get('href')!r} target={action.get('target')!r}"
        )

    if href and target and not target.startswith("_"):
        step_start = time.perf_counter()
        absolute_href = urljoin(action.get("frame_url") or current_url, href)
        frame_url_before = get_frame_location(driver, target)
        if verbose:
            print(f"Navegando frame {target!r} a: {absolute_href}")
        navigate_named_frame(driver, target, absolute_href)
        frame_url_after = get_frame_location(driver, target)
        emit_log(log_callback, f"{log_prefix}: navegacion de frame en {time.perf_counter() - step_start:.1f}s")
    else:
        step_start = time.perf_counter()
        try:
            native_click_action(driver, action)
            emit_log(log_callback, f"{log_prefix}: click nativo en {time.perf_counter() - step_start:.1f}s")
        except WebDriverException as exc:
            urls_after_native = snapshot_window_urls(driver)
            new_handles_after_native = [handle for handle in urls_after_native if handle not in current_handles]
            if new_handles_after_native:
                emit_log(
                    log_callback,
                    f"{log_prefix}: click nativo con excepcion pero abrio ventana en {time.perf_counter() - step_start:.1f}s",
                )
                if verbose:
                    print(f"Click nativo reporto error, pero abrio {len(new_handles_after_native)} ventana(s): {exc}")
            else:
                if verbose:
                    print(f"Click nativo no funciono; intento activador JS asincrono: {exc}")
                switch_to_action_frame(driver, action)
                try:
                    async_start = time.perf_counter()
                    driver.execute_script(ASYNC_CLICK_ACTION_SCRIPT, action["action_index"])
                    emit_log(log_callback, f"{log_prefix}: click JS asincrono en {time.perf_counter() - async_start:.1f}s")
                except TimeoutException as exc:
                    emit_log(
                        log_callback,
                        f"{log_prefix}: click JS asincrono timeout en {time.perf_counter() - async_start:.1f}s",
                    )
                    if verbose:
                        print(f"El activador JS asincrono quedo en timeout; reviso si la accion abrio una ventana: {exc}")
                finally:
                    driver.switch_to.default_content()

    pause_start = time.perf_counter()
    time.sleep(float(os.getenv("SITFA_POST_CLICK_PAUSE", "0.2")))
    emit_log(log_callback, f"{log_prefix}: pausa posterior al click en {time.perf_counter() - pause_start:.1f}s")

    wait_start = time.perf_counter()
    wait_for_window_update(
        driver,
        before_urls,
        timeout=float(os.getenv("SITFA_WINDOW_WAIT", "8")),
        verbose=verbose,
    )
    emit_log(log_callback, f"{log_prefix}: ventana detectada en {time.perf_counter() - wait_start:.1f}s")

    content_start = time.perf_counter()
    wait_for_window_content(driver, timeout=float(os.getenv("SITFA_WINDOW_WAIT", "8")))
    emit_log(log_callback, f"{log_prefix}: contenido de ventana listo en {time.perf_counter() - content_start:.1f}s")

    ready_start = time.perf_counter()
    wait_for_ready(driver, timeout=15)
    emit_log(log_callback, f"{log_prefix}: readyState listo en {time.perf_counter() - ready_start:.1f}s")

    screenshot_path = Path("ultima_accion.png").resolve()
    should_capture = verbose or os.getenv("SITFA_DEBUG_SCREENSHOT", "").strip() == "1"
    if should_capture:
        driver.save_screenshot(str(screenshot_path))
    if verbose:
        print(f"URL antes: {current_url}")
        print(f"URL despues: {driver.current_url}")
        if target and frame_url_after:
            print(f"Frame {target!r} antes: {frame_url_before}")
            print(f"Frame {target!r} despues: {frame_url_after}")
        print(f"Captura despues de accion: {screenshot_path}")
        print_windows_info(driver, verbose=True)


def run_action_menu(driver) -> None:
    while True:
        try:
            print_windows_info(driver)
            actions = get_clickable_actions(driver)
        except WebDriverException as exc:
            print(f"No se pudo leer la pagina actual: {exc}")
            option = input("Presiona r para reintentar, h para exportar HTML, w para ventanas o q para salir: ").strip().lower()
            if option in {"q", "salir", ""}:
                return
            if option in {"h", "html", "dump"}:
                try:
                    dump_current_state(driver)
                except WebDriverException as dump_exc:
                    print(f"No se pudo exportar HTML: {dump_exc}")
            if option in {"w", "ventanas", "windows"}:
                windows = print_windows_info(driver)
                choice = input("Numero de ventana para activar, Enter para mantener actual: ").strip()
                if choice.isdigit():
                    switch_to_window_number(driver, int(choice))
            continue

        print_action_menu(actions)
        raw_option = input(
            "Elige numero, r para refrescar, h para exportar HTML, w para ventanas, g <url>, q para salir: "
        ).strip()
        option = raw_option.lower()

        if option in {"q", "salir", ""}:
            return
        if option.startswith("g "):
            safe_get(driver, raw_option[2:].strip(), timeout=20)
            continue
        if option in {"r", "refrescar"}:
            continue
        if option in {"h", "html", "dump"}:
            try:
                dump_current_state(driver)
            except WebDriverException as exc:
                print(f"No se pudo exportar HTML: {exc}")
            continue
        if option in {"w", "ventanas", "windows"}:
            windows = print_windows_info(driver)
            choice = input("Numero de ventana para activar, Enter para mantener actual: ").strip()
            if choice.isdigit():
                switch_to_window_number(driver, int(choice))
            continue
        if not option.isdigit():
            print("Opcion invalida.")
            continue

        selected_index = int(option) - 1
        if selected_index < 0 or selected_index >= len(actions):
            print("Numero fuera de rango.")
            continue

        selected_action = actions[selected_index]
        print(f"Ejecutando: {selected_action.get('label') or '(sin texto)'}")
        try:
            click_action(driver, selected_action)
        except WebDriverException as exc:
            print(f"No se pudo ejecutar la accion: {exc}")
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass


def main() -> None:
    browser, headless, dump_litigantes_html = parse_runtime_options()
    username, password = read_credentials()
    case_type, case_number, case_year = read_case_query()
    driver = create_driver(initial_url=LOGIN_URL, browser=browser, headless=headless)
    try:
        wait_for_ready(driver, timeout=10)
        submit_login(driver, username, password)
        wait_for_ready(driver, timeout=30)

        submit_case_query(driver, case_type, case_number, case_year)
        if dump_litigantes_html:
            export_litigantes_popup_html(driver)
            return
        history_rows = print_history_tab(driver)
        liquidacion_rows = print_liquidacion_tab(driver)
        escritos_resolver_rows = print_escritos_resolver_tab(driver)
        prompt_section_selection(driver, history_rows, liquidacion_rows, escritos_resolver_rows)
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


if __name__ == "__main__":
    main()

