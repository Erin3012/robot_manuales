from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class WebDriverException(Exception):
    pass


class TimeoutException(WebDriverException):
    pass


class NoSuchElementException(WebDriverException):
    pass


class By:
    TAG_NAME = "tag name"
    CSS_SELECTOR = "css selector"
    ID = "id"
    NAME = "name"
    XPATH = "xpath"


class WebDriverWait:
    def __init__(self, driver: Any, timeout: float, poll_frequency: float = 0.2) -> None:
        self.driver = driver
        self.timeout = timeout
        self.poll_frequency = poll_frequency

    def until(self, condition: Callable[[Any], Any]) -> Any:
        deadline = time.time() + self.timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                value = condition(self.driver)
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            time.sleep(self.poll_frequency)
        raise TimeoutException(str(last_error) if last_error else "Timed out")


def _selector_for(by: str, value: str) -> str:
    if by == By.CSS_SELECTOR:
        return value
    if by == By.TAG_NAME:
        return value
    if by == By.ID:
        return f"#{value}"
    if by == By.NAME:
        return f"[name='{value}']"
    if by == By.XPATH:
        raise WebDriverException("XPath no esta soportado en esta compatibilidad")
    raise WebDriverException(f"Localizador no soportado: {by}")


def _translate_script(script: str) -> str:
    translated = script.replace("arguments[0]", "arg0").replace("arguments[1]", "arg1")
    return translated


@dataclass
class _ElementLocator:
    driver: "PlaywrightDriver"
    selector: str
    index: int = 0

    @property
    def locator(self):
        return self.driver._current_context().locator(self.selector).nth(self.index)

    def _handle(self):
        try:
            return self.locator.element_handle(timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError as exc:
            raise NoSuchElementException(str(exc)) from exc

    def click(self) -> None:
        try:
            self.locator.click(timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc

    def clear(self) -> None:
        try:
            self.locator.fill("", timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc

    def send_keys(self, *values: Any) -> None:
        text = "".join("" if value is None else str(value) for value in values)
        try:
            self.locator.press_sequentially(text, timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError:
            try:
                self.locator.fill(text, timeout=self.driver._action_timeout_ms())
            except PlaywrightTimeoutError as exc:
                raise TimeoutException(str(exc)) from exc

    def get_attribute(self, name: str) -> str | None:
        try:
            return self.locator.get_attribute(name, timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc

    @property
    def text(self) -> str:
        try:
            return self.locator.inner_text(timeout=self.driver._action_timeout_ms())
        except PlaywrightTimeoutError:
            try:
                return self.locator.text_content(timeout=self.driver._action_timeout_ms()) or ""
            except PlaywrightTimeoutError as exc:
                raise TimeoutException(str(exc)) from exc

    def is_displayed(self) -> bool:
        try:
            return bool(self.locator.is_visible(timeout=self.driver._action_timeout_ms()))
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc

    def is_enabled(self) -> bool:
        try:
            return bool(self.locator.is_enabled(timeout=self.driver._action_timeout_ms()))
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc

    def find_element(self, by: str, value: str) -> "_ElementLocator":
        selector = _selector_for(by, value)
        return _ElementLocator(self.driver, f"{self.selector} {selector}", 0)

    def find_elements(self, by: str, value: str) -> list["_ElementLocator"]:
        selector = _selector_for(by, value)
        locator = self.locator.locator(selector)
        try:
            count = locator.count()
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc
        return [_ElementLocator(self.driver, f"{self.selector} {selector}", i) for i in range(count)]

    def submit(self) -> None:
        handle = self._handle()
        if handle is None:
            raise NoSuchElementException("No se pudo obtener el elemento para submit")
        try:
            self.driver._current_context().evaluate(
                """(el) => {
                    if (el && el.form) {
                        el.form.submit();
                        return true;
                    }
                    return false;
                }""",
                handle,
            )
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc


class _SwitchTo:
    def __init__(self, driver: "PlaywrightDriver") -> None:
        self._driver = driver

    def default_content(self) -> None:
        self._driver._current_frame = None

    def parent_frame(self) -> None:
        frame = self._driver._current_frame
        if frame is None or frame.parent_frame is None:
            self._driver._current_frame = None
            return
        self._driver._current_frame = frame.parent_frame

    def frame(self, frame_reference: Any) -> None:
        if frame_reference is None:
            self.default_content()
            return
        if isinstance(frame_reference, int):
            frames = self._driver._child_frames()
            if frame_reference < 0 or frame_reference >= len(frames):
                raise WebDriverException(f"No existe frame con indice {frame_reference}")
            self._driver._current_frame = frames[frame_reference]
            return
        if isinstance(frame_reference, str):
            for frame in self._driver._child_frames():
                if frame.name == frame_reference:
                    self._driver._current_frame = frame
                    return
            for element in self._driver.find_elements(By.TAG_NAME, "frame") + self._driver.find_elements(
                By.TAG_NAME, "iframe"
            ):
                if (element.get_attribute("name") or "") == frame_reference or (element.get_attribute("id") or "") == frame_reference:
                    self.frame(element)
                    return
            raise WebDriverException(f"No encontre el frame destino {frame_reference!r}")
        if isinstance(frame_reference, _ElementLocator):
            handle = frame_reference._handle()
            if handle is None:
                raise WebDriverException("No se pudo resolver el frame")
            frame = handle.content_frame()
            if frame is None:
                raise WebDriverException("No se pudo obtener el content_frame")
            self._driver._current_frame = frame
            return
        raise WebDriverException("Referencia de frame no soportada")

    def window(self, handle: str) -> None:
        self._driver._switch_to_window(handle)


class PlaywrightDriver:
    def __init__(self, initial_url: str | None = None, browser: str = "playwright", headless: bool = False):
        self._playwright = sync_playwright().start()
        self._browser_type = browser.strip().lower()
        channel = os.getenv("SITFA_PLAYWRIGHT_CHANNEL", "").strip() or None
        launch_args: dict[str, Any] = {"headless": headless}
        if channel:
            launch_args["channel"] = channel
        self._browser = self._playwright.chromium.launch(**launch_args)
        self._context = self._browser.new_context(
            viewport={"width": 1600, "height": 1200},
            ignore_https_errors=True,
        )
        self._page = self._context.new_page()
        self._current_frame = None
        self.switch_to = _SwitchTo(self)
        self._page_load_timeout = int(os.getenv("SITFA_PAGE_LOAD_TIMEOUT", "20"))
        self._script_timeout = int(os.getenv("SITFA_SCRIPT_TIMEOUT", "8"))
        self._page_load_strategy = (os.getenv("SITFA_PAGE_LOAD_STRATEGY", "eager") or "eager").strip().lower()
        if initial_url:
            self.get(initial_url)

    def _action_timeout_ms(self) -> int:
        return max(1, int(self._script_timeout * 1000))

    def _current_context(self):
        return self._current_frame or self._page

    def _child_frames(self):
        context = self._current_frame or self._page.main_frame
        return list(context.child_frames)

    def _page_for_handle(self, handle: str):
        for page in self._context.pages:
            if self._handle_for_page(page) == handle:
                return page
        return None

    def _handle_for_page(self, page) -> str:
        return f"page-{id(page)}"

    @property
    def current_url(self) -> str:
        try:
            return self._current_context().url
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    @property
    def title(self) -> str:
        try:
            context = self._current_context()
            if self._current_frame is None:
                return self._page.title()
            return context.evaluate("document.title") or ""
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    @property
    def page_source(self) -> str:
        try:
            context = self._current_context()
            if self._current_frame is None:
                return self._page.content()
            return context.content()
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    @property
    def current_window_handle(self) -> str:
        return self._handle_for_page(self._page)

    @property
    def window_handles(self) -> list[str]:
        return [self._handle_for_page(page) for page in self._context.pages]

    def get(self, url: str) -> None:
        try:
            self._page.goto(url, wait_until=self._wait_until())
            self._current_frame = None
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    def _wait_until(self) -> str:
        if self._page_load_strategy == "load":
            return "load"
        if self._page_load_strategy == "commit":
            return "commit"
        return "domcontentloaded"

    def set_page_load_timeout(self, timeout: int) -> None:
        self._page_load_timeout = timeout

    def set_script_timeout(self, timeout: int) -> None:
        self._script_timeout = timeout

    def execute_script(self, script: str, *args: Any) -> Any:
        translated = _translate_script(script)
        if "window.open(arguments[0]" in script:
            url = str(args[0]) if args else ""
            new_page = self._context.new_page()
            try:
                new_page.goto(url, wait_until=self._wait_until())
            except Exception:
                pass
            return None
        if "window.setTimeout(function() { window.location.href = targetUrl; }, 0);" in script:
            url = str(args[0]) if args else ""
            self._page.goto(url, wait_until=self._wait_until())
            self._current_frame = None
            return True

        context = self._current_context()
        js_args = list(args)
        try:
            return context.evaluate(f"(args) => {{ const arg0 = args[0]; const arg1 = args[1]; {translated} }}", js_args)
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    def find_elements(self, by: str, value: str) -> list[_ElementLocator]:
        selector = _selector_for(by, value)
        locator = self._current_context().locator(selector)
        try:
            count = locator.count()
        except PlaywrightTimeoutError as exc:
            raise TimeoutException(str(exc)) from exc
        return [_ElementLocator(self, selector, i) for i in range(count)]

    def find_element(self, by: str, value: str) -> _ElementLocator:
        elements = self.find_elements(by, value)
        if not elements:
            raise NoSuchElementException(f"No se encontro elemento para {by}={value}")
        return elements[0]

    def get_cookies(self) -> list[dict[str, Any]]:
        try:
            return self._context.cookies()
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    def save_screenshot(self, path: str) -> None:
        try:
            self._page.screenshot(path=path, full_page=True)
        except Exception as exc:
            raise WebDriverException(str(exc)) from exc

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass

    def _switch_to_window(self, handle: str) -> None:
        page = self._page_for_handle(handle)
        if page is None:
            raise WebDriverException(f"No existe la ventana {handle}")
        self._page = page
        self._current_frame = None

    def quit(self) -> None:
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

