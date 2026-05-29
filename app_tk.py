import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from selenium.common.exceptions import WebDriverException

from main import LOGIN_URL, consult_case, create_driver, login_to_sitfa


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

                    browser = "edge" if payload.get("headless") else "ie"
                    mode = "headless" if payload.get("headless") else "visible"
                    self._log(f"Creando driver en modo {mode}")
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
        self.headless_var = tk.BooleanVar(value=False)
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
        self.log_text = None

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

        ttk.Checkbutton(card, text="Modo no visible (Edge headless)", variable=self.headless_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 16)
        )

        self.login_button = ttk.Button(card, text="Ingresar", command=self._on_login)
        self.login_button.grid(row=4, column=0, columnspan=2, sticky="ew")

        ttk.Label(card, textvariable=self.status_var, foreground="#334155", wraplength=420).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(16, 0)
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

        notebook = ttk.Notebook(self.case_frame)
        notebook.pack(fill="both", expand=True)

        self.history_tree = self._build_tree_tab(notebook, "Historia", ("Fecha", "Referencia"), ("fecha", "referencia"))
        self.liquidacion_tree = self._build_tree_tab(notebook, "Liquidacion", ("Fecha", "Referencia"), ("fecha", "referencia"))
        self.escritos_tree = self._build_tree_tab(notebook, "Esc. por Resolv.", ("Fecha", "Referencia"), ("fecha", "referencia"))
        self.litigantes_tree = self._build_tree_tab(
            notebook,
            "Litigantes",
            ("Sujeto", "Rut/Pasaporte", "Nombre o Razon Social", "Fec. Nacimiento", "Edad"),
            ("Sujeto", "Rut/Pasaporte", "Nombre o Razón Social", "Fec. Nacimiento", "Edad"),
        )
        self.cons_lit_tree = self._build_tree_tab(
            notebook,
            "Cons. Lit.",
            ("RIT", "Fec. Ing.", "Fec. Ult. tramite", "Tribunal", "Materia(Termino)"),
            ("RIT", "Fec. Ing.", "Fec. Últ. trámite", "Tribunal", "Materia(Término)"),
        )
        self.log_text = self._build_log_tab(notebook, "Logs")

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

    def _fill_tree(self, tree: ttk.Treeview, rows: list[dict], keys: tuple[str, ...]) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for row in rows:
            values = [row.get(key, "") for key in keys]
            tree.insert("", "end", values=values)

    def _fill_section(self, section: str, rows: list[dict]) -> None:
        if section == "historia":
            self._fill_tree(self.history_tree, rows, ("fecha", "referencia"))
        elif section == "liquidacion":
            self._fill_tree(self.liquidacion_tree, rows, ("fecha", "referencia"))
        elif section == "escritos":
            self._fill_tree(self.escritos_tree, rows, ("fecha", "referencia"))
        elif section == "litigantes":
            self._fill_tree(
                self.litigantes_tree,
                rows,
                ("Sujeto", "Rut/Pasaporte", "Nombre o Razón Social", "Fec. Nacimiento", "Edad"),
            )
        elif section == "cons_lit":
            self._fill_tree(
                self.cons_lit_tree,
                rows,
                ("RIT", "Fec. Ing.", "Fec. Últ. trámite", "Tribunal", "Materia(Término)"),
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

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("Salir", "Hay una operacion en curso. Desea cerrar igualmente?"):
                return
        self._worker.submit("shutdown")


if __name__ == "__main__":
    app = SitfaApp()
    app.mainloop()
