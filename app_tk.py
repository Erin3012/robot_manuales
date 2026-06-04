import queue
import threading
import uuid
import tkinter as tk
from datetime import datetime
import tempfile
from pathlib import Path
from tkinter import messagebox, ttk

import fitz
from PIL import Image, ImageTk
from selenium.common.exceptions import WebDriverException

from main import LOGIN_URL, consult_case, create_driver, download_pdf_with_session, login_to_sitfa


class BackendWorker:
    def __init__(self, result_queue: queue.Queue) -> None:
        self._result_queue = result_queue
        self._task_queue: queue.Queue = queue.Queue()
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

        self.history_tree = self._build_tree_tab(notebook, "Historia", ("Fecha", "Referencia"), ("fecha", "referencia"))
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
        self.log_text = self._build_log_tab(notebook, "Logs")

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

        def _set_initial_split() -> None:
            if not self.winfo_exists():
                return
            width = self.case_frame.winfo_width()
            if width <= 1:
                width = self.winfo_width()
            if width <= 1:
                self.after(80, _set_initial_split)
                return
            content.sashpos(0, max(460, int(width * 0.60)))

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
            self.history_tree,
            self.liquidacion_tree,
            self.escritos_tree,
            self.litigantes_tree,
            self.cons_lit_tree,
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
        if section == "historia":
            self._fill_tree(self.history_tree, rows, ("fecha", "referencia"), section)
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
        if tree is self.history_tree:
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
    app = SitfaApp()
    app.mainloop()
