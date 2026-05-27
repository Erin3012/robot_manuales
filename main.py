import getpass
import json
import os
import socket
import sys
import uuid
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOGIN_URL = "http://www.familia.pjud/SITFAWEB/jsp/Login/Login.jsp"
POST_LOGIN_URL = "http://www.familia.pjud/SITFAWEB/MenuLinkAction.do?opMenu=ConsultaRolTramitar&opRol=59"


ELEMENTS_SCRIPT = """
() => {
    const cssPath = (element) => {
        if (element.id) {
            return `#${CSS.escape(element.id)}`;
        }

        const parts = [];
        while (element && element.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
            let selector = element.nodeName.toLowerCase();
            if (element.name) {
                selector += `[name="${CSS.escape(element.name)}"]`;
            } else {
                let index = 1;
                let sibling = element;
                while ((sibling = sibling.previousElementSibling)) {
                    if (sibling.nodeName === element.nodeName) {
                        index++;
                    }
                }
                selector += `:nth-of-type(${index})`;
            }
            parts.unshift(selector);
            element = element.parentElement;
        }

        return parts.join(" > ");
    };

    const textOf = (element) => (element.innerText || element.value || element.textContent || "")
        .replace(/\\s+/g, " ")
        .trim()
        .slice(0, 120);

    const elements = [];

    document.querySelectorAll("form").forEach((element) => {
        elements.push({
            kind: "form",
            selector: cssPath(element),
            name: element.getAttribute("name") || "",
            id: element.id || "",
            method: element.getAttribute("method") || "",
            action: element.getAttribute("action") || "",
        });
    });

    document.querySelectorAll("input, select, textarea, button, a").forEach((element) => {
        const item = {
            kind: element.tagName.toLowerCase(),
            selector: cssPath(element),
            name: element.getAttribute("name") || "",
            id: element.id || "",
            type: element.getAttribute("type") || "",
            value: element.value || "",
            text: textOf(element),
            href: element.getAttribute("href") || "",
            disabled: Boolean(element.disabled),
            visible: Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length),
        };

        if (element.tagName.toLowerCase() === "select") {
            item.options = Array.from(element.options).map((option) => ({
                text: option.text,
                value: option.value,
                selected: option.selected,
            }));
        }

        elements.push(item);
    });

    return {
        title: document.title,
        url: location.href,
        elements,
    };
}
"""


CLICKABLES_SCRIPT = """
() => {
    const cssPath = (element) => {
        if (element.id) {
            return `#${CSS.escape(element.id)}`;
        }

        const parts = [];
        while (element && element.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
            let selector = element.nodeName.toLowerCase();
            if (element.name) {
                selector += `[name="${CSS.escape(element.name)}"]`;
            } else {
                let index = 1;
                let sibling = element;
                while ((sibling = sibling.previousElementSibling)) {
                    if (sibling.nodeName === element.nodeName) {
                        index++;
                    }
                }
                selector += `:nth-of-type(${index})`;
            }
            parts.unshift(selector);
            element = element.parentElement;
        }

        return parts.join(" > ");
    };

    const isVisible = (element) => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
    );

    const textOf = (element) => (
        element.innerText ||
        element.value ||
        element.getAttribute("title") ||
        element.getAttribute("alt") ||
        element.getAttribute("aria-label") ||
        element.getAttribute("name") ||
        element.id ||
        element.getAttribute("href") ||
        ""
    ).replace(/\\s+/g, " ").trim().slice(0, 120);

    const candidateSelector = [
        "button",
        "input[type='button']",
        "input[type='submit']",
        "input[type='image']",
        "a[href]",
        "[onclick]",
        "[role='button']",
        "[role='tab']",
        "img"
    ].join(",");

    const seen = new Set();
    const actions = [];

    document.querySelectorAll(candidateSelector).forEach((source) => {
        const target = source.closest(
            "button,input[type='button'],input[type='submit'],input[type='image'],a[href],[onclick],[role='button'],[role='tab']"
        ) || source;

        if (seen.has(target)) {
            return;
        }
        seen.add(target);

        const tag = target.tagName.toLowerCase();
        const type = (target.getAttribute("type") || "").toLowerCase();
        const role = (target.getAttribute("role") || "").toLowerCase();
        const isAction =
            tag === "button" ||
            tag === "a" ||
            ["button", "submit", "image"].includes(type) ||
            ["button", "tab"].includes(role) ||
            target.hasAttribute("onclick");

        if (!isAction || !isVisible(target) || target.disabled) {
            return;
        }

        const selector = cssPath(target);
        const selectorIndex = Array.from(document.querySelectorAll(selector)).indexOf(target);

        actions.push({
            action_index: actions.length,
            kind: tag,
            type,
            role,
            label: textOf(target),
            name: target.getAttribute("name") || "",
            value: target.value || target.getAttribute("value") || "",
            selector,
            selector_index: selectorIndex,
            href: target.getAttribute("href") || "",
            onclick: target.getAttribute("onclick") || "",
        });
    });

    return {
        title: document.title,
        url: location.href,
        actions,
    };
}
"""


CLICK_ACTION_SCRIPT = """
(actionIndex) => {
    const isVisible = (element) => Boolean(
        element.offsetWidth || element.offsetHeight || element.getClientRects().length
    );

    const candidateSelector = [
        "button",
        "input[type='button']",
        "input[type='submit']",
        "input[type='image']",
        "a[href]",
        "[onclick]",
        "[role='button']",
        "[role='tab']",
        "img"
    ].join(",");

    const seen = new Set();
    const actions = [];

    document.querySelectorAll(candidateSelector).forEach((source) => {
        const target = source.closest(
            "button,input[type='button'],input[type='submit'],input[type='image'],a[href],[onclick],[role='button'],[role='tab']"
        ) || source;

        if (seen.has(target)) {
            return;
        }
        seen.add(target);

        const tag = target.tagName.toLowerCase();
        const type = (target.getAttribute("type") || "").toLowerCase();
        const role = (target.getAttribute("role") || "").toLowerCase();
        const isAction =
            tag === "button" ||
            tag === "a" ||
            ["button", "submit", "image"].includes(type) ||
            ["button", "tab"].includes(role) ||
            target.hasAttribute("onclick");

        if (isAction && isVisible(target) && !target.disabled) {
            actions.push(target);
        }
    });

    const target = actions[actionIndex];
    if (!target) {
        throw new Error(`No existe accion clicable con indice ${actionIndex}`);
    }

    target.scrollIntoView({ block: "center", inline: "center" });
    const tag = target.tagName.toLowerCase();
    const type = (target.getAttribute("type") || "").toLowerCase();
    const href = target.getAttribute("href") || "";

    if ((tag === "input" || tag === "button") && type === "submit" && target.form) {
        if (typeof target.form.requestSubmit === "function") {
            target.form.requestSubmit(target);
            return;
        }

        if (target.name) {
            const submitter = document.createElement("input");
            submitter.type = "hidden";
            submitter.name = target.name;
            submitter.value = target.value || "";
            target.form.appendChild(submitter);
        }

        HTMLFormElement.prototype.submit.call(target.form);
        return;
    }

    const clickEvent = new MouseEvent("click", {
        bubbles: true,
        cancelable: true,
        view: window,
    });
    try {
        Object.defineProperty(clickEvent, "srcElement", { value: target });
        Object.defineProperty(clickEvent, "fromElement", { value: null });
        Object.defineProperty(clickEvent, "toElement", { value: target });
    } catch (error) {
        // Some event properties are read-only in Chromium.
    }

    if (typeof target.onclick === "function") {
        const previousEvent = window.event;
        window.event = clickEvent;
        try {
            const result = target.onclick.call(target, clickEvent);
            if (result === false || clickEvent.defaultPrevented) {
                return;
            }
        } finally {
            window.event = previousEvent;
        }

        if (tag === "a" && href && href !== "#") {
            window.location.href = target.href;
        }
        return;
    }

    if (typeof target.click === "function") {
        const previousEvent = window.event;
        window.event = clickEvent;
        try {
            ["mouseover", "mousedown", "mouseup"].forEach((eventType) => {
                target.dispatchEvent(new MouseEvent(eventType, {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                }));
            });
        } finally {
            window.event = previousEvent;
        }
        target.click();
        return;
    }

    const wasNotCancelled = target.dispatchEvent(clickEvent);

    if (wasNotCancelled && tag === "a" && href && href !== "#") {
        window.location.href = target.href;
    }
}
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
    case_type = (os.getenv("SITFA_TIP_CAUSA") or input("Tipo causa (ej: C): ")).strip().upper()
    case_number = (os.getenv("SITFA_ROL_CAUSA") or input("Rol causa: ")).strip()
    case_year = (os.getenv("SITFA_ERA_CAUSA") or input("Año causa (ej: 2026): ")).strip()

    if not case_type:
        raise ValueError("Falta el tipo de causa. Define SITFA_TIP_CAUSA o ingresalo por consola.")
    if not case_number:
        raise ValueError("Falta el rol de causa. Define SITFA_ROL_CAUSA o ingresalo por consola.")
    if not case_year:
        raise ValueError("Falta el año de causa. Define SITFA_ERA_CAUSA o ingresalo por consola.")

    return case_type, case_number, case_year


def submit_login(page, username: str, password: str) -> None:
    machine_name = socket.gethostname() or "0"
    windows_user = getpass.getuser() or "0"
    local_ip = get_local_ip()
    mac_address = get_mac_address()

    page.locator('input[name="username"]').fill(username)
    page.locator('input[name="password"]').fill(password)

    page.evaluate(
        """
        ({ username, password, machineName, windowsUser, localIp, macAddress }) => {
            const form = document.forms["InicioAplicacionForm"];
            if (!form) {
                throw new Error("No se encontro el formulario InicioAplicacionForm");
            }

            form.elements["username"].value = username.padEnd(50, " ");
            form.elements["password"].value = password;
            form.elements["nomequipo"].value = machineName;
            form.elements["usuequipo"].value = windowsUser;
            form.elements["ipequipo"].value = localIp;
            form.elements["MAC_equipo"].value = macAddress;

            let accept = form.querySelector('input[type="hidden"][name="Aceptar"]');
            if (!accept) {
                accept = document.createElement("input");
                accept.type = "hidden";
                accept.name = "Aceptar";
                form.appendChild(accept);
            }
            accept.value = "Aceptar";

            form.submit();
        }
        """,
        {
            "username": username,
            "password": password,
            "machineName": machine_name,
            "windowsUser": windows_user,
            "localIp": local_ip,
            "macAddress": mac_address,
        },
    )


def consult_case(page, case_type: str, case_number: str, case_year: str) -> None:
    page.locator('select[name="TIP_Causa"]').select_option(case_type)
    page.locator('input[name="ROL_Causa"]').fill(case_number)
    page.locator('select[name="ERA_Causa"]').select_option(case_year)
    page.evaluate(
        """
        ({ caseType, caseNumber, caseYear }) => {
            const form = document.forms["TramitarPpalForm"];
            if (!form) {
                throw new Error("No se encontro el formulario TramitarPpalForm");
            }

            form.elements["TIP_Causa"].value = caseType;
            form.elements["ROL_Causa"].value = caseNumber;
            form.elements["ERA_Causa"].value = caseYear;

            if (typeof window.ValidaCampos === "function" && window.ValidaCampos() === false) {
                throw new Error("ValidaCampos rechazo la consulta");
            }

            HTMLFormElement.prototype.submit.call(form);
        }
        """,
        {
            "caseType": case_type,
            "caseNumber": case_number,
            "caseYear": case_year,
        },
    )


def inspect_interactive_elements(page) -> list[dict]:
    results = []

    for index, frame in enumerate(page.frames):
        try:
            frame_result = frame.evaluate(ELEMENTS_SCRIPT)
        except Exception as exc:
            frame_result = {
                "title": "",
                "url": frame.url,
                "elements": [],
                "error": str(exc),
            }

        frame_result["frame_index"] = index
        frame_result["frame_name"] = frame.name
        results.append(frame_result)

    return results


def print_interactive_elements(results: list[dict]) -> None:
    for frame in results:
        print("")
        print(f"Frame {frame['frame_index']} name={frame['frame_name']!r}")
        print(f"URL: {frame['url']}")
        if frame.get("error"):
            print(f"Error leyendo frame: {frame['error']}")
            continue

        for number, element in enumerate(frame["elements"], start=1):
            label = element.get("text") or element.get("value") or element.get("name") or element.get("id")
            details = [
                f"{number:03d}",
                element["kind"],
                f"selector={element['selector']!r}",
            ]
            if element.get("name"):
                details.append(f"name={element['name']!r}")
            if element.get("id"):
                details.append(f"id={element['id']!r}")
            if element.get("type"):
                details.append(f"type={element['type']!r}")
            if label:
                details.append(f"label={label!r}")
            if element.get("href"):
                details.append(f"href={element['href']!r}")
            if element.get("visible") is not None:
                details.append(f"visible={element['visible']}")
            print(" | ".join(details))


def get_clickable_actions(page) -> list[dict]:
    menu = []

    for frame_index, frame in enumerate(page.frames):
        try:
            frame_result = frame.evaluate(CLICKABLES_SCRIPT)
        except Exception as exc:
            print(f"No se pudieron leer acciones del frame {frame_index}: {exc}")
            continue

        for action in frame_result["actions"]:
            action["frame_index"] = frame_index
            action["frame_name"] = frame.name
            action["frame_url"] = frame.url
            menu.append(action)

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


def wait_after_action(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass


def newest_page_after_action(context, pages_before):
    new_pages = [candidate for candidate in context.pages if candidate not in pages_before]
    if not new_pages:
        return None

    new_page = new_pages[-1]
    try:
        new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    return new_page


def click_action(page, action: dict):
    current_url = page.url
    pages_before = list(page.context.pages)
    frame = page.frames[action["frame_index"]]
    print(
        "Accion seleccionada: "
        f"frame={action['frame_index']} selector={action.get('selector')} "
        f"name={action.get('name')!r} value={action.get('value')!r}"
    )

    kind = (action.get("kind") or "").lower()
    type_name = (action.get("type") or "").lower()
    has_onclick = bool(action.get("onclick"))
    use_submit_fallback = kind == "input" and type_name == "submit"
    use_legacy_first = has_onclick or kind in {"td", "tr", "div", "span", "img"}

    if use_legacy_first:
        try:
            frame.evaluate(CLICK_ACTION_SCRIPT, action["action_index"])
            wait_after_action(page)
            new_page = newest_page_after_action(page.context, pages_before)
            if new_page:
                print(f"Nueva ventana/pestana: {new_page.url}")
                page = new_page

            screenshot_path = Path("ultima_accion.png").resolve()
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"URL antes: {current_url}")
            print(f"URL despues: {page.url}")
            print(f"Captura despues de accion: {screenshot_path}")
            return page
        except Exception as exc:
            print(f"Activador JS legacy no funciono, usando click real: {exc}")

    if not use_submit_fallback:
        try:
            selector = action["selector"]
            selector_index = max(int(action.get("selector_index", 0)), 0)
            frame.locator(selector).nth(selector_index).click(force=True, timeout=5_000)
            wait_after_action(page)
            new_page = newest_page_after_action(page.context, pages_before)
            if new_page:
                print(f"Nueva ventana/pestana: {new_page.url}")
                page = new_page

            screenshot_path = Path("ultima_accion.png").resolve()
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"URL antes: {current_url}")
            print(f"URL despues: {page.url}")
            print(f"Captura despues de accion: {screenshot_path}")
            return page
        except Exception as exc:
            print(f"Click real no funciono, usando activador JS: {exc}")

    frame.evaluate(CLICK_ACTION_SCRIPT, action["action_index"])
    wait_after_action(page)
    new_page = newest_page_after_action(page.context, pages_before)
    if new_page:
        print(f"Nueva ventana/pestana: {new_page.url}")
        page = new_page

    screenshot_path = Path("ultima_accion.png").resolve()
    page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"URL antes: {current_url}")
    print(f"URL despues: {page.url}")
    print(f"Captura despues de accion: {screenshot_path}")
    return page


def run_action_menu(page) -> None:
    while True:
        actions = get_clickable_actions(page)
        print_action_menu(actions)
        option = input("Elige numero, r para refrescar, q para salir: ").strip().lower()

        if option in {"q", "salir", ""}:
            return
        if option in {"r", "refrescar"}:
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
        page = click_action(page, selected_action)


def main() -> None:
    username, password = read_credentials()
    case_type, case_number, case_year = read_case_query()
    login_screenshot_path = Path("login_result.png").resolve()
    target_screenshot_path = Path("consulta_rol_tramitar.png").resolve()
    query_screenshot_path = Path("consulta_rol_tramitar_resultado.png").resolve()
    elements_path = Path("consulta_rol_tramitar_elements.json").resolve()
    query_elements_path = Path("consulta_rol_tramitar_resultado_elements.json").resolve()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.on("dialog", lambda dialog: (print(f"Dialogo: {dialog.message}"), dialog.accept()))

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector('form[name="InicioAplicacionForm"]', timeout=15_000)
        submit_login(page, username, password)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        page.screenshot(path=str(login_screenshot_path), full_page=True)
        print(f"URL despues del login: {page.url}")
        print(f"Titulo despues del login: {page.title()}")
        print(f"Captura login guardada en: {login_screenshot_path}")

        page.goto(POST_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        page.screenshot(path=str(target_screenshot_path), full_page=True)
        interactive_elements = inspect_interactive_elements(page)
        elements_path.write_text(
            json.dumps(interactive_elements, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"URL actual: {page.url}")
        print(f"Titulo actual: {page.title()}")
        print(f"Captura destino guardada en: {target_screenshot_path}")
        print(f"Elementos destino guardados en: {elements_path}")

        consult_case(page, case_type, case_number, case_year)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

        page.screenshot(path=str(query_screenshot_path), full_page=True)
        query_interactive_elements = inspect_interactive_elements(page)
        query_elements_path.write_text(
            json.dumps(query_interactive_elements, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"URL despues de consultar: {page.url}")
        print(f"Titulo despues de consultar: {page.title()}")
        print(f"Captura resultado guardada en: {query_screenshot_path}")
        print(f"Elementos resultado guardados en: {query_elements_path}")

        if os.getenv("SITFA_KEEP_OPEN", "1") != "0":
            run_action_menu(page)

        browser.close()


if __name__ == "__main__":
    main()
