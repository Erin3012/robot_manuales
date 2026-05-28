import getpass
import json
import os
import re
import subprocess
import socket
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.ie.service import Service as IeService
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "http://www.familia.pjud/SITFAWEB/jsp/Login/Login.jsp"
POST_LOGIN_URL = "http://www.familia.pjud/SITFAWEB/MenuLinkAction.do?opMenu=ConsultaRolTramitar&opRol=59"
PDF_VIEWER_HOST = "127.0.0.1"
PDF_VIEWER_PORT = 54877
PDF_VIEWER_SCRIPT = Path(__file__).with_name("pdf_viewer.py")


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


def create_driver(initial_url: str | None = None):
    options = webdriver.IeOptions()
    mode = os.getenv("SITFA_IE_MODE", "ie").strip().lower()

    options.page_load_strategy = os.getenv("SITFA_PAGE_LOAD_STRATEGY", "eager")
    if initial_url:
        options.initial_browser_url = initial_url
    options.ignore_zoom_level = True
    options.ignore_protected_mode_settings = True
    options.ensure_clean_session = False
    options.native_events = False

    if mode != "ie":
        edge_path = os.getenv("SITFA_EDGE_PATH") or find_edge_path()
        options.attach_to_edge_chrome = True
        if edge_path:
            options.edge_executable_path = edge_path

    driver_path = os.getenv("IEDRIVER_PATH")
    service = IeService(executable_path=driver_path) if driver_path else IeService()
    driver = webdriver.Ie(service=service, options=options)
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
    WebDriverWait(driver, timeout).until(
        lambda current: current.execute_script(
            """
            var form = document.forms["TramitarPpalForm"];
            if (!form || !form.elements["TIP_Causa"]) {
                return false;
            }
            var options = form.elements["TIP_Causa"].options;
            for (var i = 0; i < options.length; i++) {
                if (options[i].value === arguments[0]) {
                    return true;
                }
            }
            return false;
            """,
            case_type,
        )
    )


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


def open_litigantes_popup(driver) -> str:
    driver.switch_to.default_content()
    before_urls = snapshot_window_urls(driver)
    try:
        clicked = driver.execute_script(
            """
            var candidates = document.getElementsByTagName("img");
            for (var i = 0; i < candidates.length; i++) {
                var img = candidates[i];
                var alt = String(img.getAttribute("alt") || "").toLowerCase();
                var title = String(img.getAttribute("title") || "").toLowerCase();
                var onclick = String(img.getAttribute("onclick") || "");
                if (alt.indexOf("datos litigantes") >= 0 || title.indexOf("datos litigantes") >= 0 || onclick.indexOf("ShowLitigantes") >= 0) {
                    if (typeof img.click === "function") {
                        img.click();
                        return true;
                    }
                    if (typeof img.onclick === "function") {
                        img.onclick();
                        return true;
                    }
                }
            }
            if (typeof ShowLitigantes === "function") {
                ShowLitigantes("");
                return true;
            }
            return false;
            """
        )
    except WebDriverException as exc:
        raise RuntimeError(f"No se pudo invocar ShowLitigantes: {exc}") from exc

    if not clicked:
        raise RuntimeError("No encontre el boton o funcion de Litigantes.")

    wait_for_window_update(driver, before_urls, timeout=float(os.getenv("SITFA_WINDOW_WAIT", "8")))
    wait_for_ready(driver, timeout=10)
    try:
        return driver.current_window_handle
    except WebDriverException:
        return ""


def describe_litigantes_popup(driver) -> None:
    print("")
    print("Litigantes")
    try:
        popup_handle = open_litigantes_popup(driver)
    except Exception as exc:
        print(f"No se pudo abrir Litigantes: {exc}")
        return

    if not popup_handle:
        print("No se detecto la ventana de Litigantes.")
        return

    try:
        print_windows_info(driver)
    except WebDriverException:
        pass

    try:
        current_url = driver.current_url
    except WebDriverException:
        current_url = ""

    if "DownloadFile.do" in current_url:
        output_path = Path.cwd() / "litigantes_tmp.bin"
        try:
            content_type, content = download_session_resource(driver, current_url, output_path)
        except Exception as exc:
            print(f"No se pudo descargar el contenido de Litigantes: {exc}")
            return

        lowered = content_type.lower()
        is_pdf = lowered == "application/pdf" or content.startswith(b"%PDF")
        if is_pdf:
            pdf_path = output_path.with_suffix(".pdf")
            if output_path != pdf_path:
                try:
                    output_path.rename(pdf_path)
                    output_path = pdf_path
                except OSError:
                    pass
            if open_local_file(output_path):
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass
            print(f"Litigantes descargado y abierto: {output_path}")
            return

        text_path = output_path.with_suffix(".html" if "html" in lowered else ".txt")
        try:
            output_path.rename(text_path)
            output_path = text_path
        except OSError:
            pass
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = str(content)
        print(text[:4000])
        print(f"Litigantes descargado en: {output_path}")
        return

    try:
        tables = extract_tables_from_current_context(driver)
        if tables:
            print("Tablas encontradas en Litigantes:")
            for table in tables[:10]:
                print(
                    f"  Tabla {table['table_index']} id={table['id']!r} class={table['class_name']!r} "
                    f"filas={table['rows']} columnas={table['columns']}"
                )
                sample_rows = [item.get("cells", []) for item in table.get("sample", [])]
                for line in format_table_rows(sample_rows, max_column_width=28, max_rows=5):
                    print(f"    {line}")
        else:
            body_text = ""
            try:
                body_elements = driver.find_elements(By.TAG_NAME, "body")
                if body_elements:
                    body_text = body_elements[0].text.strip()
            except WebDriverException:
                body_text = ""
            if body_text:
                print(body_text[:4000])
            else:
                print("No encontre contenido util en la ventana de Litigantes.")
    except WebDriverException as exc:
        print(f"No se pudo leer Litigantes: {exc}")


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
    wait_for_case_type_option(driver, case_type, timeout=20)

    result = driver.execute_script(
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


def extract_tables_from_current_context(driver) -> list[dict]:
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

    contexts = [{"frame_index": "0", "frame_name": ""}]
    frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
    for index, frame in enumerate(frames, start=1):
        contexts.append(
            {
                "frame_index": str(index),
                "frame_name": frame.get_attribute("name") or frame.get_attribute("id") or "",
            }
        )

    for context in contexts:
        driver.switch_to.default_content()
        if context["frame_index"] != "0":
            frame_position = int(context["frame_index"]) - 1
            try:
                driver.switch_to.frame(frames[frame_position])
            except WebDriverException as exc:
                results.append({**context, "error": str(exc), "tables": []})
                continue

        try:
            tables = extract_tables_from_current_context(driver)
        except WebDriverException as exc:
            results.append({**context, "error": str(exc), "tables": []})
            continue

        results.append({**context, "tables": tables})

    driver.switch_to.default_content()

    print("")
    print("Tablas encontradas:")
    found_any = False
    for context in results:
        for table in context.get("tables", []):
            found_any = True
            frame_label = context["frame_name"] or context["frame_index"]
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


def get_frames_info(driver) -> list[dict]:
    driver.switch_to.default_content()
    try:
        info = driver.execute_script(FRAMES_INFO_SCRIPT)
    except WebDriverException as exc:
        print(f"No se pudo leer informacion de frames: {exc}")
        return []

    frames = info.get("frames") or []
    Path("frames_debug.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if frames:
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


def get_clickable_actions(driver) -> list[dict]:
    menu = []

    driver.switch_to.default_content()
    frames_info = get_frames_info(driver)
    frame_indexes = ["0"] + [str(frame["index"]) for frame in frames_info]
    frames_by_index = {str(frame["index"]): frame for frame in frames_info}

    for frame_index in frame_indexes:
        driver.switch_to.default_content()
        frame_name = ""
        if frame_index != "0":
            frame = frames_by_index.get(frame_index, {})
            frame_name = frame.get("name") or frame.get("id") or ""
            if not switch_to_frame_descriptor(driver, frame):
                print(f"No se pudo entrar al frame {frame_index} ({frame_name})")
                continue

        try:
            frame_result = driver.execute_script(CLICKABLES_SCRIPT)
        except WebDriverException as exc:
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


def print_windows_info(driver) -> list[dict]:
    windows = collect_windows_info(driver)
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


def wait_for_window_update(driver, before_urls: dict[str, str], timeout: float = 8.0) -> str | None:
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
    print(f"Detectada {selected_reason}: {driver.current_url}")
    return selected_handle


def click_action(driver, action: dict) -> None:
    current_url = driver.current_url
    before_urls = snapshot_window_urls(driver)
    current_handles = set(before_urls)
    href = (action.get("href") or "").strip()
    target = (action.get("target") or "").strip()
    frame_url_before = ""
    frame_url_after = ""

    print(
        "Accion seleccionada: "
        f"frame={action['frame_index']} selector={action.get('selector')} "
        f"name={action.get('name')!r} value={action.get('value')!r} "
        f"href={action.get('href')!r} target={action.get('target')!r}"
    )

    if href and target and not target.startswith("_"):
        absolute_href = urljoin(action.get("frame_url") or current_url, href)
        frame_url_before = get_frame_location(driver, target)
        print(f"Navegando frame {target!r} a: {absolute_href}")
        navigate_named_frame(driver, target, absolute_href)
        frame_url_after = get_frame_location(driver, target)
    else:
        try:
            native_click_action(driver, action)
        except WebDriverException as exc:
            urls_after_native = snapshot_window_urls(driver)
            new_handles_after_native = [handle for handle in urls_after_native if handle not in current_handles]
            if new_handles_after_native:
                print(f"Click nativo reporto error, pero abrio {len(new_handles_after_native)} ventana(s): {exc}")
            else:
                print(f"Click nativo no funciono; intento activador JS asincrono: {exc}")
                switch_to_action_frame(driver, action)
                try:
                    driver.execute_script(ASYNC_CLICK_ACTION_SCRIPT, action["action_index"])
                except TimeoutException as exc:
                    print(f"El activador JS asincrono quedo en timeout; reviso si la accion abrio una ventana: {exc}")
                finally:
                    driver.switch_to.default_content()

    time.sleep(1)
    wait_for_window_update(driver, before_urls, timeout=float(os.getenv("SITFA_WINDOW_WAIT", "8")))
    wait_for_ready(driver, timeout=15)

    screenshot_path = Path("ultima_accion.png").resolve()
    driver.save_screenshot(str(screenshot_path))
    print(f"URL antes: {current_url}")
    print(f"URL despues: {driver.current_url}")
    if target and frame_url_after:
        print(f"Frame {target!r} antes: {frame_url_before}")
        print(f"Frame {target!r} despues: {frame_url_after}")
    print(f"Captura despues de accion: {screenshot_path}")
    print_windows_info(driver)


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
    username, password = read_credentials()
    case_type, case_number, case_year = read_case_query()
    driver = create_driver(initial_url=LOGIN_URL)
    try:
        wait_for_ready(driver, timeout=10)
        submit_login(driver, username, password)
        wait_for_ready(driver, timeout=30)
        safe_get(driver, POST_LOGIN_URL, timeout=30)
        wait_for_ready(driver, timeout=30)

        submit_case_query(driver, case_type, case_number, case_year)
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

