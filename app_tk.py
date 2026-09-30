
import queue
import threading
import uuid
import tkinter as tk
import calendar
from datetime import date, datetime, timedelta
import tempfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urljoin

import fitz
from PIL import Image, ImageTk
from browser_compat import WebDriverException
from actualizador import verificar_actualizacion_obligatoria

from main import (
    LOGIN_URL,
    collect_pending_case_rows,
    consult_case,
    consult_history_only,
    create_driver,
    download_pdf_with_session,
    login_to_sitfa,
    normalize_litigantes_value,
    open_cartola_bco_estado_popup_from_litigantes,
    resolve_litigantes_popup_url,
    wait_for_ready,
)
from web_app import (
    _resolve_cartola_popup_url,
    apply_cartola_filters_and_consult,
    build_cartola_xls_bytes,
    cartola_popup_select_account_and_consult,
    read_cartola_payload,
)


class BackendWorker:
    def __init__(self, result_queue: queue.Queue) -> None:
        self._result_queue = result_queue
        self._task_queue: queue.Queue = queue.Queue()
        self._selected_litigante: dict | None = None
        self._cartola_handle = ""
        self._cartola_data: dict = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, action: str, payload: dict | None = None) -> None:
        self._task_queue.put((action, payload or {}))

    def _emit(self, event: str, payload: dict | None = None) -> None:
        self._result_queue.put((event, payload or {}))

    def _log(self, message: str) -> None:
        self._emit("log", {"message": message})

    def _section_result(self, section: str, rows: list[dict]) -> None:
        self._emit("section_result", {"section": section, "rows": rows})

    def _run(self) -> None:
        driver = None
        while True:
            action, payload = self._task_queue.get()
            try:
                if action == "shutdown":
                    if driver is not None:
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
                    self._emit("shutdown_complete")
                    return

                if action == "login":
                    if driver is not None:
                        try:
                            driver.quit()
                        except WebDriverException:
                            pass
                        driver = None

                    mode = "no visible" if payload.get("headless") else "visible"
                    browser = (payload.get("browser") or "chrome").strip().lower()
                    self._log(f"Creando driver en modo {mode} con {browser.upper()}")
                    driver = create_driver(
                        initial_url=LOGIN_URL,
                        browser=browser,
                        headless=bool(payload.get("headless")),
                    )
                    login_to_sitfa(driver, payload["username"], payload["password"], log_callback=self._log)
                    self._selected_litigante = None
                    self._cartola_handle = ""
                    self._cartola_data = {}
                    self._emit("login_success", {"driver_ready": True})
                    continue

                if action == "consult":
                    if driver is None:
                        raise RuntimeError("No hay una sesion activa.")
                    results = consult_case(
                        driver,
                        payload["rit"],
                        log_callback=self._log,
                        section_callback=self._section_result,
                    )
                    self._emit("consult_success", {"rit": payload["rit"], "results": results})
                    continue

                if action == "pending":
                    if driver is None:
                        raise RuntimeError("No hay una sesion activa.")
                    rows = collect_pending_case_rows(driver, log_callback=self._log)
                    self._emit("pending_result", {"rows": rows})
                    continue

                if action == "history_detail":
                    if driver is None:
                        raise RuntimeError("No hay una sesion activa.")
                    rows = consult_history_only(driver, payload["rit"], log_callback=self._log)
                    self._emit("history_detail_result", {"rit": payload["rit"], "rows": rows})
                    continue

                if action == "select_litigante":
                    self._selected_litigante = payload.get("litigante") or None
                    self._cartola_handle = ""
                    self._cartola_data = {}
                    self._emit("litigante_selected", {"litigante": self._selected_litigante or {}})
                    continue

                if action == "cartola_open":
                    if driver is None:
                        raise RuntimeError("No hay una sesion activa.")
                    litigante = self._selected_litigante
                    if not litigante:
                        raise RuntimeError("Primero selecciona un litigante.")
                    popup_url = resolve_litigantes_popup_url(driver, log_callback=self._log)
                    if not popup_url:
                        raise RuntimeError("No se pudo resolver la ventana de Litigantes.")
                    popup_url, reason = open_cartola_bco_estado_popup_from_litigantes(
                        driver,
                        selected_litigante=litigante,
                        litigantes_popup_url=popup_url,
                        log_callback=self._log,
                    )
                    if not popup_url:
                        raise RuntimeError("No se pudo abrir Cartola Banco Estado" + (f": {reason}" if reason else "."))
                    # Mantener la ventana envolvente original de SITFA. La URL
                    # interna IrPopUpCausaAccion.do puede perder el contexto
                    # que el servidor usa para cargar las cuentas.
                    popup_url = urljoin(driver.current_url, popup_url)
                    before = set(driver.window_handles)
                    original = driver.current_window_handle
                    driver.execute_script("window.open(arguments[0], '_blank');", popup_url)
                    new_handle = ""
                    deadline = datetime.now().timestamp() + 6
                    while datetime.now().timestamp() < deadline:
                        new = list(set(driver.window_handles) - before)
                        if new:
                            new_handle = new[0]
                            break
                        threading.Event().wait(0.1)
                    if new_handle:
                        driver.switch_to.window(new_handle)
                    else:
                        driver.get(popup_url)
                    wait_for_ready(driver, timeout=10)
                    self._cartola_handle = driver.current_window_handle
                    try:
                        self._cartola_data = read_cartola_payload(driver)
                    except RuntimeError as wrapper_error:
                        # La ventana PopUpPpal.jsp es una envolvente; el
                        # formulario real puede estar en IrPopUpCausaAccion.do.
                        resolved_url = _resolve_cartola_popup_url(
                            driver,
                            popup_url,
                            log_callback=self._log,
                        )
                        if not resolved_url or resolved_url == popup_url:
                            raise wrapper_error
                        driver.get(resolved_url)
                        wait_for_ready(driver, timeout=10)
                        self._cartola_data = read_cartola_payload(driver)
                    # Algunos formularios SITFA solo llenan las cuentas después
                    # de ejecutar la acción interna "Predet." y consultar.
                    if not self._cartola_data.get("accounts"):
                        fallback = cartola_popup_select_account_and_consult(driver, log_callback=self._log)
                        account = str(fallback.get("account") or "")
                        if account:
                            self._cartola_data["accounts"] = [{"value": account, "text": account}]
                            self._cartola_data["selected_account"] = account
                        self._cartola_data["movements"] = fallback.get("movements") or []
                    try:
                        driver.switch_to.window(original)
                        driver.switch_to.default_content()
                    except WebDriverException:
                        pass
                    self._emit("cartola_result", {"data": self._cartola_data})
                    continue

                if action == "cartola_consult":
                    if driver is None or not self._cartola_handle:
                        raise RuntimeError("Primero abre la Cartola Banco Estado.")
                    original = driver.current_window_handle
                    driver.switch_to.window(self._cartola_handle)
                    self._cartola_data = apply_cartola_filters_and_consult(
                        driver,
                        account=payload.get("account", ""),
                        start_date=payload.get("start_date", ""),
                        end_date=payload.get("end_date", ""),
                        log_callback=self._log,
                    )
                    try:
                        driver.switch_to.window(original)
                        driver.switch_to.default_content()
                    except WebDriverException:
                        pass
                    self._emit("cartola_result", {"data": self._cartola_data})
                    continue

                if action == "cartola_export":
                    movements = self._cartola_data.get("movements") or []
                    if not movements:
                        raise RuntimeError("No hay movimientos para exportar.")
                    rut = str((self._selected_litigante or {}).get("Rut/Pasaporte") or "")
                    content = build_cartola_xls_bytes(
                        movements,
                        title="Banco Estado",
                        account=str(self._cartola_data.get("selected_account") or ""),
                        rut=rut,
                        start_date=str(self._cartola_data.get("start_date") or ""),
                        end_date=str(self._cartola_data.get("end_date") or ""),
                    )
                    output = Path(tempfile.gettempdir()) / f"cartola_banco_estado_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xls"
                    output.write_bytes(content)
                    self._emit("cartola_exported", {"path": str(output)})
                    continue

                if action == "open_pdf":
                    if driver is None:
                        raise RuntimeError("No hay una sesion activa.")
                    pdf_url = (payload.get("pdf_url") or "").strip()
                    if not pdf_url:
                        raise RuntimeError("La fila no tiene PDF asociado.")
                    prefix = (payload.get("prefix") or "pdf").strip() or "pdf"
                    output_path = Path(tempfile.gettempdir()) / f"sitfa_{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.pdf"
                    downloaded_path = download_pdf_with_session(driver, pdf_url, output_path)
                    self._emit("log", {"message": f"PDF abierto: {downloaded_path.name}"})
                    self._emit("pdf_opened", {"path": str(downloaded_path), "pdf_url": pdf_url})
                    continue

                self._emit("error", {"message": f"Accion desconocida: {action}"})
            except Exception as exc:
                self._emit("error", {"message": str(exc), "action": action})


class SitfaApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Consulta SITFA")
        self.geometry("1180x760")
        self.minsize(1000, 680)

        self._result_queue: queue.Queue = queue.Queue()
        self._worker = BackendWorker(self._result_queue)
        self._busy = False

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.headless_var = tk.BooleanVar(value=True)
        self.browser_var = tk.StringVar(value="chrome")
        self.rit_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ingrese sus credenciales para iniciar sesion.")
        self.session_var = tk.StringVar(value="Sin sesion iniciada")
        self.case_var = tk.StringVar(value="Sin causa consultada")

        self.login_frame = ttk.Frame(self, padding=16)
        self.case_frame = ttk.Frame(self, padding=16)

        self.history_tree = None
        self.liquidacion_tree = None
        self.escritos_tree = None
        self.litigantes_tree = None
        self.cons_lit_tree = None
        self.pending_tree = None
        self.detail_tree = None
        self.cartola_tree = None
        self._cartola_data: dict = {}
        self._selected_litigante: dict | None = None
        self.cartola_account_var = tk.StringVar()
        self.cartola_start_var = tk.StringVar()
        self.cartola_end_var = tk.StringVar()
        self.cartola_status_var = tk.StringVar(value="Cartola no abierta")
        self.cartola_account_combo = None
        self.detail_rit_var = tk.StringVar()
        self.pdf_notebook = None
        self.pdf_status_var = tk.StringVar(value="Sin PDF cargado")
        self.pdf_zoom_var = tk.DoubleVar(value=1.35)
        self.log_text = None
        self._section_rows: dict[str, list[dict]] = {}
        self._pdf_tabs: dict[str, dict[str, object]] = {}
        self._pdf_tab_counters: dict[str, int] = {}
        self._current_pdf_tab_id: str = ""
        self._pdf_home_tab_id: str = ""

        self._build_login_frame()
        self._build_case_frame()

        self.login_frame.pack(fill="both", expand=True)
        self.case_frame.pack_forget()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_results)

    def _build_login_frame(self) -> None:
        card = ttk.Frame(self.login_frame, padding=24)
        card.place(relx=0.5, rely=0.45, anchor="center")

        ttk.Label(card, text="Ingreso SITFA", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(card, text="Usuario").grid(row=1, column=0, sticky="w", pady=(16, 6))
        ttk.Entry(card, textvariable=self.username_var, width=34).grid(row=1, column=1, sticky="ew", pady=(16, 6))

        ttk.Label(card, text="Clave").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.password_var, show="*", width=34).grid(row=2, column=1, sticky="ew", pady=6)

        ttk.Label(card, text="Navegador").grid(row=3, column=0, sticky="w", pady=(10, 6))
        browser_selector = ttk.Combobox(
            card,
            textvariable=self.browser_var,
            values=("chrome", "edge"),
            state="readonly",
            width=31,
        )
        browser_selector.grid(row=3, column=1, sticky="ew", pady=(10, 6))

        ttk.Checkbutton(card, text="Modo no visible", variable=self.headless_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 16)
        )

        self.login_button = ttk.Button(card, text="Ingresar", command=self._on_login)
        self.login_button.grid(row=5, column=0, columnspan=2, sticky="ew")

        ttk.Label(card, textvariable=self.status_var, foreground="#334155", wraplength=420).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(16, 0)
        )
        card.columnconfigure(1, weight=1)

    def _build_case_frame(self) -> None:
        header = ttk.Frame(self.case_frame)
        header.pack(fill="x", pady=(0, 12))

        ttk.Label(header, text="Consulta de causa", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.session_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(header, textvariable=self.case_var).grid(row=2, column=0, sticky="w", pady=(2, 0))

        action_bar = ttk.Frame(self.case_frame)
        action_bar.pack(fill="x", pady=(0, 12))

        ttk.Label(action_bar, text="RIT").pack(side="left")
        self.rit_entry = ttk.Entry(action_bar, textvariable=self.rit_var, width=22)
        self.rit_entry.pack(side="left", padx=(8, 12))

        self.consult_button = ttk.Button(action_bar, text="Consultar", command=self._on_consult)
        self.consult_button.pack(side="left")

        self.pending_button = ttk.Button(action_bar, text="Causas pendientes", command=self._on_pending)
        self.pending_button.pack(side="left", padx=(8, 0))

        self.detail_button = ttk.Button(action_bar, text="Historia RIT", command=self._on_history_detail)
        self.detail_button.pack(side="left", padx=(8, 0))

        self.cartola_button = ttk.Button(action_bar, text="Abrir cartola", command=self._on_open_cartola)
        self.cartola_button.pack(side="left", padx=(8, 0))

        self.export_cartola_button = ttk.Button(action_bar, text="Exportar cartola", command=self._on_export_cartola)
        self.export_cartola_button.pack(side="left", padx=(8, 0))

        self.new_query_button = ttk.Button(action_bar, text="Nueva consulta", command=self._on_new_query)
        self.new_query_button.pack(side="left", padx=(8, 0))

        self.exit_button = ttk.Button(action_bar, text="Salir", command=self._on_close)
        self.exit_button.pack(side="right")

        content = ttk.Panedwindow(self.case_frame, orient="horizontal")
        content.pack(fill="both", expand=True)

        left_panel = ttk.Frame(content)
        left_panel.columnconfigure(0, weight=1)
        left_panel.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(left_panel)
        notebook.grid(row=0, column=0, sticky="nsew")

        self.pending_tree = self._build_tree_tab(notebook, "Pendientes", ("RIT", "Detalle", "Tribunal"), ("RIT", "Detalle", "Tribunal"))
        self.history_tree = self._build_tree_tab(
            notebook,
            "Historia",
            ("Fecha", "Tip. Ing.", "Referencia"),
            ("fecha", "tip_ing", "referencia"),
        )
        self.liquidacion_tree = self._build_tree_tab(notebook, "Liquidacion", ("Fecha", "Referencia"), ("fecha", "referencia"))
        self.escritos_tree = self._build_tree_tab(notebook, "Esc. por Resolv.", ("Fecha", "Referencia"), ("fecha", "referencia"))
        self.litigantes_tree = self._build_tree_tab(
            notebook,
            "Litigantes",
            ("Sujeto", "Rut/Pasaporte", "Nombre o Razon Social", "Fec. Nacimiento", "Edad"),
            ("Sujeto", "Rut/Pasaporte", "Nombre o Raz?n Social", "Fec. Nacimiento", "Edad"),
        )
        self.cons_lit_tree = self._build_tree_tab(
            notebook,
            "Cons. Lit.",
            ("RIT", "Fec. Ing.", "Fec. Ult. tramite", "Tribunal", "Materia(Termino)"),
            ("RIT", "Fec. Ing.", "Fec. ?lt. tr?mite", "Tribunal", "Materia(T?rmino)"),
        )
        self.detail_tree = self._build_tree_tab(
            notebook,
            "Historia RIT",
            ("Fecha", "Tip. Ing.", "Referencia"),
            ("fecha", "tip_ing", "referencia"),
        )
        self.cartola_tree = self._build_tree_tab(
            notebook,
            "Cartola",
            ("Fecha", "Tipo movimiento", "Monto"),
            ("Fecha", "Tipo movimiento", "Monto"),
        )
        self.log_text = self._build_log_tab(notebook, "Logs")

        litigante_toolbar = ttk.Frame(left_panel)
        litigante_toolbar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(litigante_toolbar, text="Seleccionar litigante", command=self._on_select_litigante).pack(side="left")
        ttk.Label(litigante_toolbar, text="  Selecciona una fila en la pestaña Litigantes y luego pulsa el botón.").pack(side="left")

        cartola_toolbar = ttk.Frame(left_panel)
        cartola_toolbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(cartola_toolbar, text="Cuenta").pack(side="left")
        self.cartola_account_combo = ttk.Combobox(
            cartola_toolbar,
            textvariable=self.cartola_account_var,
            width=16,
            state="readonly",
        )
        self.cartola_account_combo.pack(side="left", padx=(4, 8))
        ttk.Label(cartola_toolbar, text="Desde").pack(side="left")
        ttk.Entry(cartola_toolbar, textvariable=self.cartola_start_var, width=11).pack(side="left", padx=(4, 2))
        ttk.Button(cartola_toolbar, text="📅", width=3, command=lambda: self._pick_date(self.cartola_start_var)).pack(side="left", padx=(0, 8))
        ttk.Label(cartola_toolbar, text="Hasta").pack(side="left")
        ttk.Entry(cartola_toolbar, textvariable=self.cartola_end_var, width=11).pack(side="left", padx=(4, 2))
        ttk.Button(cartola_toolbar, text="📅", width=3, command=lambda: self._pick_date(self.cartola_end_var)).pack(side="left", padx=(0, 8))
        ttk.Button(cartola_toolbar, text="Consultar movimientos", command=self._on_consult_cartola).pack(side="left")
        ttk.Label(cartola_toolbar, textvariable=self.cartola_status_var, foreground="#334155").pack(side="left", padx=(8, 0))

        right_panel = ttk.Frame(content)
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(2, weight=1)

        top_pdf_bar = ttk.Frame(right_panel)
        top_pdf_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(top_pdf_bar, text="Visor PDF", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(top_pdf_bar, textvariable=self.pdf_status_var, foreground="#334155").pack(side="right")

        zoom_bar = ttk.Frame(right_panel)
        zoom_bar.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        ttk.Button(zoom_bar, text="-", width=3, command=lambda: self._change_pdf_zoom(-0.1)).pack(side="left")
        ttk.Button(zoom_bar, text="+", width=3, command=lambda: self._change_pdf_zoom(0.1)).pack(side="left", padx=(6, 0))
        ttk.Button(zoom_bar, text="Ajustar", command=self._fit_pdf_width).pack(side="left", padx=(10, 0))
        ttk.Button(zoom_bar, text="100%", command=self._reset_pdf_zoom).pack(side="left", padx=(6, 0))
        ttk.Button(zoom_bar, text="Cerrar pestaña", command=self._close_current_pdf_tab).pack(side="left", padx=(12, 0))
        ttk.Label(zoom_bar, textvariable=self.pdf_zoom_var, foreground="#334155").pack(side="right")

        self.pdf_notebook = ttk.Notebook(right_panel)
        self.pdf_notebook.grid(row=2, column=0, sticky="nsew")
        self.pdf_notebook.bind("<<NotebookTabChanged>>", self._on_pdf_tab_changed)
        self._build_pdf_home_tab()

        content.add(left_panel, weight=3)
        content.add(right_panel, weight=2)
        # En algunas versiones de Tk, el peso no evita que el primer panel
        # quede colapsado si su ancho solicitado inicial es cero.
        content.pane(left_panel, weight=3)
        content.pane(right_panel, weight=2)

        def _set_initial_split() -> None:
            if not self.winfo_exists():
                return
            width = self.case_frame.winfo_width()
            if width <= 1:
                width = self.winfo_width()
            if width <= 1:
                self.after(80, _set_initial_split)
                return
            content.sashpos(0, max(520, min(int(width * 0.60), width - 420)))

        self.after(120, _set_initial_split)

        footer = ttk.Frame(self.case_frame)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status_var, foreground="#334155", wraplength=920).pack(anchor="w")

    def _build_tree_tab(self, notebook: ttk.Notebook, title: str, headers: tuple[str, ...], keys: tuple[str, ...]):
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text=title)

        tree = ttk.Treeview(frame, columns=keys, show="headings")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        for key, header in zip(keys, headers):
            tree.heading(key, text=header)
            width = 150 if len(header) < 14 else 220
            if "Materia" in header or "Nombre" in header or "Tribunal" in header or "Referencia" in header:
                width = 320
            tree.column(key, width=width, anchor="w")

        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree.bind("<Double-1>", self._on_tree_double_click)

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _build_log_tab(self, notebook: ttk.Notebook, title: str):
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text=title)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="Clear", command=self._clear_logs).pack(side="right")

        text = tk.Text(frame, wrap="word", state="disabled")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=yscroll.set)

        text.grid(row=1, column=0, sticky="nsew")
        yscroll.grid(row=1, column=1, sticky="ns")

        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        return text

    def _build_pdf_home_tab(self) -> None:
        if self.pdf_notebook is None:
            return
        frame = ttk.Frame(self.pdf_notebook, padding=16)
        self.pdf_notebook.add(frame, text="Inicio")
        ttk.Label(
            frame,
            text="Los PDFs abiertos apareceran como nuevas pestañas aqui.\nSelecciona una pestaña y usa 'Cerrar pestaña' para quitarla.",
            justify="center",
            padding=24,
        ).pack(anchor="center", expand=True)
        self._pdf_home_tab_id = getattr(frame, "_pdf_tab_id", "")

    def _unique_pdf_tab_title(self, base_title: str) -> str:
        base_title = base_title.strip() or "PDF"
        current = self._pdf_tab_counters.get(base_title, 0) + 1
        self._pdf_tab_counters[base_title] = current
        return base_title if current == 1 else f"{base_title} ({current})"

    def _get_selected_pdf_tab_id(self) -> str:
        if self.pdf_notebook is None:
            return ""
        selected = self.pdf_notebook.select()
        if not selected:
            return ""
        try:
            widget = self.nametowidget(selected)
        except Exception:
            return ""
        return str(getattr(widget, "_pdf_tab_id", ""))

    def _on_pdf_tab_changed(self, _event: tk.Event) -> None:
        tab_id = self._get_selected_pdf_tab_id()
        if not tab_id:
            self._current_pdf_tab_id = ""
            self.pdf_status_var.set("Sin PDF cargado")
            self.pdf_zoom_var.set(1.35)
            return
        self._current_pdf_tab_id = tab_id
        state = self._pdf_tabs.get(tab_id, {})
        title = str(state.get("title", "PDF"))
        zoom = float(state.get("zoom", 1.35) or 1.35)
        self.pdf_status_var.set(f"{title}  x{zoom:.2f}")
        self.pdf_zoom_var.set(round(zoom, 2))

    def _bind_pdf_mousewheel(self, canvas: tk.Canvas, _event: tk.Event) -> None:
        canvas.bind_all("<MouseWheel>", lambda event: self._on_pdf_mousewheel(canvas, event))
        canvas.bind_all("<Button-4>", lambda event: self._on_pdf_mousewheel(canvas, event))
        canvas.bind_all("<Button-5>", lambda event: self._on_pdf_mousewheel(canvas, event))

    def _unbind_pdf_mousewheel(self, canvas: tk.Canvas, _event: tk.Event) -> None:
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    def _on_pdf_mousewheel(self, canvas: tk.Canvas, event: tk.Event) -> None:
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(int(-1 * (delta / 120)), "units")
        elif getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")

    def _render_pdf_tab(self, tab_id: str, scale: float | None = None) -> None:
        state = self._pdf_tabs.get(tab_id)
        if not state:
            return

        pdf_path = Path(str(state.get("path", ""))).resolve()
        scroll_frame = state.get("scroll_frame")
        if not isinstance(scroll_frame, ttk.Frame):
            return

        if not pdf_path.exists():
            self.pdf_status_var.set("El PDF no existe")
            return

        for child in list(scroll_frame.winfo_children()):
            child.destroy()
        state["images"] = []

        if scale is None:
            scale = float(state.get("zoom", self.pdf_zoom_var.get() or 1.35) or 1.35)
        scale = max(0.5, min(3.0, scale))
        state["zoom"] = round(scale, 2)
        state["title"] = str(state.get("title", pdf_path.name))
        self.pdf_zoom_var.set(round(scale, 2))
        self.pdf_status_var.set(f"{state['title']}  x{scale:.2f}")

        images: list[ImageTk.PhotoImage] = []
        try:
            document = fitz.open(pdf_path)
        except Exception as exc:
            ttk.Label(scroll_frame, text=f"No se pudo abrir el PDF: {exc}", padding=16).pack(anchor="w")
            state["images"] = images
            return

        matrix = fitz.Matrix(scale, scale)
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            photo = ImageTk.PhotoImage(image)
            images.append(photo)

            page_header = ttk.Label(scroll_frame, text=f"Pagina {page_number + 1}", padding=(12, 12, 12, 4))
            page_header.pack(anchor="w")

            image_label = ttk.Label(scroll_frame, image=photo)
            image_label.pack(anchor="w", padx=12, pady=(0, 12))

        state["images"] = images
        canvas = state.get("canvas")
        if isinstance(canvas, tk.Canvas):
            canvas.yview_moveto(0)
            canvas.configure(scrollregion=canvas.bbox("all"))
        try:
            document.close()
        except Exception:
            pass

    def _create_pdf_tab(self, pdf_path: Path, title: str | None = None) -> None:
        if self.pdf_notebook is None:
            return

        pdf_path = pdf_path.resolve()
        if not pdf_path.exists():
            return

        tab_title = self._unique_pdf_tab_title(title or pdf_path.stem)
        frame = ttk.Frame(self.pdf_notebook)
        canvas = tk.Canvas(frame, highlightthickness=0, background="#111111")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def _on_frame_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        scroll_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<Enter>", lambda _event: self._bind_pdf_mousewheel(canvas, _event))
        canvas.bind("<Leave>", lambda _event: self._unbind_pdf_mousewheel(canvas, _event))

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tab_id = uuid.uuid4().hex
        setattr(frame, "_pdf_tab_id", tab_id)
        self._pdf_tabs[tab_id] = {
            "frame": frame,
            "canvas": canvas,
            "scroll_frame": scroll_frame,
            "path": str(pdf_path),
            "title": tab_title,
            "zoom": float(self.pdf_zoom_var.get() or 1.35),
            "images": [],
        }

        self.pdf_notebook.add(frame, text=tab_title)
        self.pdf_notebook.select(frame)
        self._current_pdf_tab_id = tab_id
        self._render_pdf_tab(tab_id, float(self.pdf_zoom_var.get() or 1.35))

    def _change_pdf_zoom(self, delta: float) -> None:
        tab_id = self._get_selected_pdf_tab_id()
        if not tab_id:
            return
        state = self._pdf_tabs.get(tab_id)
        if not state:
            return
        current = float(state.get("zoom", 1.35) or 1.35)
        new_scale = max(0.5, min(3.0, current + delta))
        self._render_pdf_tab(tab_id, new_scale)

    def _reset_pdf_zoom(self) -> None:
        tab_id = self._get_selected_pdf_tab_id()
        if not tab_id:
            return
        if tab_id in self._pdf_tabs:
            self._render_pdf_tab(tab_id, 1.35)

    def _fit_pdf_width(self) -> None:
        tab_id = self._get_selected_pdf_tab_id()
        state = self._pdf_tabs.get(tab_id)
        if not state:
            return
        canvas = state.get("canvas")
        if not isinstance(canvas, tk.Canvas):
            return
        width = max(400, canvas.winfo_width() - 40)
        scale = max(0.5, min(3.0, width / 900.0))
        self._render_pdf_tab(tab_id, scale)

    def _delete_pdf_file(self, path_value: str | None) -> None:
        if not path_value:
            return
        try:
            path = Path(path_value)
        except Exception:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _close_pdf_tab(self, tab_id: str) -> None:
        if tab_id == self._pdf_home_tab_id or tab_id not in self._pdf_tabs:
            return
        state = self._pdf_tabs.pop(tab_id)
        frame = state.get("frame")
        if isinstance(frame, ttk.Frame) and self.pdf_notebook is not None:
            try:
                self.pdf_notebook.forget(frame)
            except Exception:
                pass
        self._delete_pdf_file(str(state.get("path", "")))
        if self._current_pdf_tab_id == tab_id:
            self._current_pdf_tab_id = ""
        if self.pdf_notebook is not None and self.pdf_notebook.tabs():
            self.pdf_notebook.select(self.pdf_notebook.tabs()[-1])

    def _close_current_pdf_tab(self) -> None:
        self._close_pdf_tab(self._get_selected_pdf_tab_id())

    def _close_all_pdf_tabs(self) -> None:
        for tab_id in list(self._pdf_tabs.keys()):
            self._close_pdf_tab(tab_id)
        if self.pdf_notebook is not None and self._pdf_home_tab_id:
            try:
                home_widget = self.nametowidget(self._pdf_home_tab_id) if self._pdf_home_tab_id else None
            except Exception:
                home_widget = None
            if home_widget is not None:
                try:
                    self.pdf_notebook.select(home_widget)
                except Exception:
                    pass
        self.pdf_status_var.set("Sin PDF cargado")
        self.pdf_zoom_var.set(1.35)
        self._current_pdf_tab_id = ""

    def _display_pdf(self, pdf_path: Path) -> None:
        self._create_pdf_tab(pdf_path, pdf_path.stem)

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.status_var.set(message)
        state = "disabled" if busy else "normal"
        self.login_button.config(state=state)
        self.consult_button.config(state=state)
        for button in (
            self.pending_button,
            self.detail_button,
            self.cartola_button,
            self.export_cartola_button,
        ):
            button.config(state=state)
        self.new_query_button.config(state=state)
        self.exit_button.config(state=state)

    def _on_login(self) -> None:
        if self._busy:
            return
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            messagebox.showerror("Login", "Debe ingresar usuario y clave.")
            return
        self._set_busy(True, "Iniciando sesion...")
        self._append_log("Solicitud de login enviada.")
        self._worker.submit(
            "login",
            {
                "username": username,
                "password": password,
                "headless": self.headless_var.get(),
                "browser": self.browser_var.get().strip().lower(),
            },
        )

    def _on_consult(self) -> None:
        if self._busy:
            return
        rit = self.rit_var.get().strip().upper()
        if not rit:
            messagebox.showerror("Consulta", "Debe ingresar un RIT.")
            return
        self._clear_result_trees()
        self._set_busy(True, f"Consultando causa {rit}...")
        self._append_log(f"Solicitud de consulta enviada para {rit}.")
        self._worker.submit("consult", {"rit": rit})

    def _on_pending(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Cargando causas pendientes...")
        self._worker.submit("pending")

    def _on_history_detail(self) -> None:
        if self._busy:
            return
        rit = self.detail_rit_var.get().strip().upper() or self.rit_var.get().strip().upper()
        if not rit:
            messagebox.showerror("Historia", "Ingresa un RIT en la consulta principal o en el campo de Historia RIT.")
            return
        self.detail_rit_var.set(rit)
        self._set_busy(True, f"Consultando historia de {rit}...")
        self._worker.submit("history_detail", {"rit": rit})

    def _on_select_litigante(self) -> None:
        if self._busy or self.litigantes_tree is None:
            return
        selection = self.litigantes_tree.selection()
        if not selection:
            messagebox.showinfo("Litigante", "Selecciona una fila en la pestaña Litigantes.")
            return
        rows = self._section_rows.get("litigantes", [])
        try:
            row = rows[int(selection[0]) - 1]
        except (ValueError, IndexError):
            return
        self._selected_litigante = row
        label = row.get("Nombre o Razón Social") or row.get("Nombre o RazÃ³n Social") or row.get("Rut/Pasaporte") or "sin identificación"
        self._append_log(f"Litigante seleccionado: {label}")
        self.status_var.set(f"Litigante seleccionado: {label}")
        self._worker.submit("select_litigante", {"litigante": row})

    def _pick_date(self, variable: tk.StringVar) -> None:
        """Muestra un calendario pequeño y escribe la fecha como dd/mm/yyyy."""
        popup = tk.Toplevel(self)
        popup.title("Seleccionar fecha")
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)

        today = date.today()
        selected = today
        raw_value = variable.get().strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                selected = datetime.strptime(raw_value, fmt).date()
                break
            except ValueError:
                pass
        month_var = tk.IntVar(value=selected.month)
        year_var = tk.IntVar(value=selected.year)
        title_var = tk.StringVar()
        grid_frame = ttk.Frame(popup, padding=8)
        grid_frame.pack()

        def render_month() -> None:
            for child in grid_frame.winfo_children():
                child.destroy()
            month = month_var.get()
            year = year_var.get()
            title_var.set(f"{calendar.month_name[month]} {year}")
            ttk.Button(grid_frame, text="‹", width=3, command=previous_month).grid(row=0, column=0)
            ttk.Label(grid_frame, textvariable=title_var, width=18, anchor="center").grid(row=0, column=1, columnspan=5)
            ttk.Button(grid_frame, text="›", width=3, command=next_month).grid(row=0, column=6)
            for column, name in enumerate(("Lu", "Ma", "Mi", "Ju", "Vi", "Sa", "Do")):
                ttk.Label(grid_frame, text=name, width=4, anchor="center").grid(row=1, column=column, pady=(8, 3))
            weeks = calendar.monthcalendar(year, month)
            for row_index, week in enumerate(weeks, start=2):
                for column, day_number in enumerate(week):
                    if day_number == 0:
                        ttk.Label(grid_frame, text="", width=4).grid(row=row_index, column=column)
                    else:
                        button = ttk.Button(
                            grid_frame,
                            text=str(day_number),
                            width=4,
                            command=lambda value=day_number: choose_day(value),
                        )
                        button.grid(row=row_index, column=column, padx=1, pady=1)

        def previous_month() -> None:
            month = month_var.get() - 1
            if month == 0:
                month = 12
                year_var.set(year_var.get() - 1)
            month_var.set(month)
            render_month()

        def next_month() -> None:
            month = month_var.get() + 1
            if month == 13:
                month = 1
                year_var.set(year_var.get() + 1)
            month_var.set(month)
            render_month()

        def choose_day(day_number: int) -> None:
            variable.set(f"{day_number:02d}/{month_var.get():02d}/{year_var.get():04d}")
            popup.destroy()

        render_month()
        popup.update_idletasks()
        x = self.winfo_rootx() + 260
        y = self.winfo_rooty() + 300
        popup.geometry(f"+{x}+{y}")

    def _on_open_cartola(self) -> None:
        if self._busy:
            return
        if not self._selected_litigante:
            messagebox.showinfo("Cartola", "Selecciona primero un litigante.")
            return
        self._set_busy(True, "Abriendo Cartola Banco Estado...")
        self._worker.submit("cartola_open")

    def _on_consult_cartola(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Consultando movimientos de cartola...")
        self._worker.submit(
            "cartola_consult",
            {
                "account": self.cartola_account_var.get().strip(),
                "start_date": self.cartola_start_var.get().strip(),
                "end_date": self.cartola_end_var.get().strip(),
            },
        )

    def _on_export_cartola(self) -> None:
        if self._busy:
            return
        if not self._cartola_data.get("movements"):
            messagebox.showinfo("Cartola", "Primero abre y consulta una cartola con movimientos.")
            return
        self._set_busy(True, "Generando archivo Excel...")
        self._worker.submit("cartola_export")

    def _on_new_query(self) -> None:
        if self._busy:
            return
        self.rit_var.set("")
        self.case_var.set("Sin causa consultada")
        self._clear_result_trees()
        self.status_var.set("Ingrese un nuevo RIT para consultar otra causa.")
        self.rit_entry.focus_set()

    def _clear_result_trees(self) -> None:
        for tree in (
            self.pending_tree,
            self.history_tree,
            self.liquidacion_tree,
            self.escritos_tree,
            self.litigantes_tree,
            self.cons_lit_tree,
            self.detail_tree,
            self.cartola_tree,
        ):
            for item in tree.get_children():
                tree.delete(item)
        self._section_rows.clear()

    def _fill_tree(self, tree: ttk.Treeview, rows: list[dict], keys: tuple[str, ...], section: str) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for index, row in enumerate(rows, start=1):
            values = [row.get(key, "") for key in keys]
            tree.insert("", "end", iid=str(index), values=values)
        self._section_rows[section] = rows

    def _fill_section(self, section: str, rows: list[dict]) -> None:
        if section == "pending":
            self._fill_tree(self.pending_tree, rows, ("RIT", "Tribunal", "Materia"), section)
        elif section == "historia":
            self._fill_tree(self.history_tree, rows, ("fecha", "tip_ing", "referencia"), section)
        elif section == "liquidacion":
            self._fill_tree(self.liquidacion_tree, rows, ("fecha", "referencia"), section)
        elif section == "escritos":
            self._fill_tree(self.escritos_tree, rows, ("fecha", "referencia"), section)
        elif section == "litigantes":
            self._fill_tree(
                self.litigantes_tree,
                rows,
                ("Sujeto", "Rut/Pasaporte", "Nombre o Razón Social", "Fec. Nacimiento", "Edad"),
                section,
            )
        elif section == "cons_lit":
            self._fill_tree(
                self.cons_lit_tree,
                rows,
                ("RIT", "Fec. Ing.", "Fec. Últ. trámite", "Tribunal", "Materia(Término)"),
                section,
            )
        elif section == "detail_history":
            self._fill_tree(self.detail_tree, rows, ("fecha", "tip_ing", "referencia"), section)
        elif section == "cartola":
            self._fill_tree(self.cartola_tree, rows, ("Fecha", "Tipo movimiento", "Monto"), section)

    def _append_log(self, message: str) -> None:
        if self.log_text is None:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message.rstrip()}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_logs(self) -> None:
        if self.log_text is None:
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _poll_results(self) -> None:
        while True:
            try:
                event, payload = self._result_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_result(event, payload)
        self.after(150, self._poll_results)

    def _handle_result(self, event: str, payload: dict) -> None:
        if event == "login_success":
            self.login_frame.pack_forget()
            self.case_frame.pack(fill="both", expand=True)
            self.session_var.set(f"Sesion iniciada: {self.username_var.get().strip()}")
            self.status_var.set("Sesion iniciada. Ingrese un RIT para consultar.")
            self._set_busy(False, self.status_var.get())
            self.rit_entry.focus_set()
            return

        if event == "section_result":
            section = payload.get("section", "")
            rows = payload.get("rows", [])
            self._fill_section(section, rows)
            return

        if event == "pending_result":
            rows = payload.get("rows", [])
            self._fill_section("pending", rows)
            self._set_busy(False, f"{len(rows)} causas pendientes cargadas.")
            return

        if event == "history_detail_result":
            rit = payload.get("rit", "")
            rows = payload.get("rows", [])
            self._fill_section("detail_history", rows)
            self._set_busy(False, f"Historia secundaria cargada para {rit}.")
            return

        if event == "cartola_result":
            self._cartola_data = payload.get("data") or {}
            self._fill_section("cartola", self._cartola_data.get("movements") or [])
            accounts = self._cartola_data.get("accounts") or []
            account_values = [
                str(account.get("value") or account.get("text") or "")
                for account in accounts
                if isinstance(account, dict)
            ]
            if self.cartola_account_combo is not None:
                self.cartola_account_combo["values"] = account_values
            selected = self._cartola_data.get("selected_account") or (accounts[0].get("value", "") if accounts else "")
            self.cartola_account_var.set(str(selected))
            self.cartola_start_var.set(str(self._cartola_data.get("start_date") or ""))
            self.cartola_end_var.set(str(self._cartola_data.get("end_date") or ""))
            self.cartola_status_var.set(f"{len(self._cartola_data.get('movements') or [])} movimientos")
            self._set_busy(False, "Cartola actualizada.")
            return

        if event == "cartola_exported":
            source = Path(payload.get("path", ""))
            target = filedialog.asksaveasfilename(
                title="Guardar cartola",
                initialfile=source.name,
                defaultextension=".xls",
                filetypes=(("Excel 97-2003", "*.xls"), ("Todos los archivos", "*.*")),
            )
            if target:
                Path(target).write_bytes(source.read_bytes())
                self._append_log(f"Cartola exportada: {target}")
                self._set_busy(False, f"Cartola guardada en {target}")
            else:
                self._set_busy(False, "Exportación cancelada.")
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
            return

        if event == "consult_success":
            results = payload["results"]
            rit = payload["rit"]
            self.case_var.set(f"Causa actual: {rit}")
            self._fill_section("historia", results.get("historia", []))
            self._fill_section("liquidacion", results.get("liquidacion", []))
            self._fill_section("escritos", results.get("escritos", []))
            self._fill_section("litigantes", results.get("litigantes", []))
            self._fill_section("cons_lit", results.get("cons_lit", []))
            self._set_busy(False, f"Consulta terminada para {rit}.")
            return

        if event == "pdf_opened":
            pdf_path = Path(payload.get("path", ""))
            self._display_pdf(pdf_path)
            self._set_busy(False, f"PDF abierto: {pdf_path.name}")
            return

        if event == "log":
            self._append_log(payload.get("message", ""))
            return

        if event == "error":
            self._set_busy(False, payload.get("message", "Ocurrio un error."))
            self._append_log(f"ERROR: {payload.get('message', 'Ocurrio un error.')}")
            messagebox.showerror("SITFA", payload.get("message", "Ocurrio un error."))
            return

        if event == "shutdown_complete":
            self.destroy()

    def _get_row_for_tree_item(self, tree: ttk.Treeview, item_id: str) -> dict | None:
        if tree is self.pending_tree:
            section = "pending"
        elif tree is self.history_tree:
            section = "historia"
        elif tree is self.liquidacion_tree:
            section = "liquidacion"
        elif tree is self.escritos_tree:
            section = "escritos"
        else:
            return None

        try:
            index = int(item_id) - 1
        except ValueError:
            return None

        rows = self._section_rows.get(section, [])
        if 0 <= index < len(rows):
            return rows[index]
        return None

    def _on_tree_double_click(self, event: tk.Event) -> None:
        if self._busy:
            return

        tree = event.widget
        if tree is self.pending_tree:
            selection = tree.selection()
            if not selection:
                return
            row = self._get_row_for_tree_item(tree, selection[0])
            rit = str((row or {}).get("RIT") or "").strip()
            if rit:
                self.rit_var.set(rit)
                self._on_consult()
            return
        if tree not in (self.history_tree, self.liquidacion_tree, self.escritos_tree):
            return

        selection = tree.selection()
        if not selection:
            return

        row = self._get_row_for_tree_item(tree, selection[0])
        if not row:
            return

        pdf_url = (row.get("pdf_url") or "").strip()
        if not pdf_url:
            messagebox.showinfo("PDF", "La fila seleccionada no tiene PDF asociado.")
            return

        if tree is self.history_tree:
            prefix = "historia"
        elif tree is self.liquidacion_tree:
            prefix = "liquidacion"
        else:
            prefix = "escritos"

        self._set_busy(True, "Abriendo PDF...")
        self._append_log(f"Solicitud para abrir PDF de {prefix}.")
        self._worker.submit("open_pdf", {"pdf_url": pdf_url, "prefix": prefix})

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("Salir", "Hay una operacion en curso. Desea cerrar igualmente?"):
                return
        self._close_all_pdf_tabs()
        self._worker.submit("shutdown")


if __name__ == "__main__":
    if not verificar_actualizacion_obligatoria():
        raise SystemExit(0)
    app = SitfaApp()
    app.mainloop()
