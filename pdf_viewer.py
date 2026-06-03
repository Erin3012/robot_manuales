import argparse
import json
import queue
import socket
import shutil
import threading
import uuid
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import fitz
from PIL import Image, ImageTk


HOST = "127.0.0.1"
PORT = 54877
CACHE_DIR = Path(__file__).with_name(".pdf_viewer_cache")


class PdfViewerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Robot Manuales - Visor de PDFs")
        self.root.geometry("1200x800")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Cerrar pestaña actual", command=self._close_current_tab).pack(side="right")

        self.message_queue: queue.Queue[dict] = queue.Queue()
        self.tab_counters: dict[str, int] = {}
        self._recent_sources: dict[str, float] = {}
        self._home_tab = None

        self._build_empty_state()
        self._start_server()
        self.root.after(150, self._poll_queue)

    def _build_empty_state(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Inicio")
        self._home_tab = frame

        label = ttk.Label(
            frame,
            text="Los PDFs abiertos apareceran como nuevas pestañas.",
            padding=24,
        )
        label.pack(anchor="center", expand=True)

    def _start_server(self) -> None:
        thread = threading.Thread(target=self._server_loop, daemon=True)
        thread.start()

    def _server_loop(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, PORT))
            server.listen(5)
            while True:
                conn, _ = server.accept()
                with conn:
                    chunks = []
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                if not chunks:
                    continue
                try:
                    payload = json.loads(b"".join(chunks).decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                source_path = Path(payload.get("path", ""))
                if not source_path.exists():
                    continue
                source_key = str(source_path.resolve()).lower()
                now = time.monotonic()
                last_seen = self._recent_sources.get(source_key)
                if last_seen is not None and now - last_seen < 2.0:
                    try:
                        conn.sendall(b"OK")
                    except OSError:
                        pass
                    continue
                self._recent_sources[source_key] = now
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cached_path = CACHE_DIR / f"{source_path.stem}_{uuid.uuid4().hex}{source_path.suffix}"
                try:
                    shutil.copy2(source_path, cached_path)
                except OSError:
                    continue
                payload["path"] = str(cached_path)
                self.message_queue.put(payload)
                try:
                    conn.sendall(b"OK")
                except OSError:
                    pass

    def _poll_queue(self) -> None:
        while True:
            try:
                payload = self.message_queue.get_nowait()
            except queue.Empty:
                break
            self.open_pdf(Path(payload["path"]), payload.get("title"))
        self.root.after(150, self._poll_queue)

    def _unique_tab_title(self, base_title: str) -> str:
        base_title = base_title.strip() or "PDF"
        current = self.tab_counters.get(base_title, 0) + 1
        self.tab_counters[base_title] = current
        return base_title if current == 1 else f"{base_title} ({current})"

    def _close_current_tab(self) -> None:
        selected = self.notebook.select()
        if not selected:
            return
        try:
            widget = self.notebook.nametowidget(selected)
        except Exception:
            return
        if widget is self._home_tab:
            return
        cache_path = getattr(widget, "_pdf_cache_path", None)
        try:
            self.notebook.forget(widget)
        except Exception:
            return
        if cache_path:
            try:
                Path(str(cache_path)).unlink(missing_ok=True)
            except OSError:
                pass
        remaining_tabs = self.notebook.tabs()
        if remaining_tabs:
            self.notebook.select(remaining_tabs[-1])
        elif self._home_tab is not None:
            self.notebook.select(self._home_tab)

    def open_pdf(self, pdf_path: Path, title: str | None = None) -> None:
        pdf_path = pdf_path.resolve()
        if not pdf_path.exists():
            return

        tab_title = self._unique_tab_title(title or pdf_path.stem)
        frame = ttk.Frame(self.notebook)
        setattr(frame, "_pdf_cache_path", str(pdf_path))
        self.notebook.add(frame, text=tab_title)
        self.notebook.select(frame)

        outer = ttk.Frame(frame)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, background="#111111")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def _on_frame_configure(_event: object) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        def _on_mousewheel(event: tk.Event) -> None:
            delta = getattr(event, "delta", 0)
            if delta:
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        scroll_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<Button-4>", _on_mousewheel), add="+")
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<Button-5>", _on_mousewheel), add="+")
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<Button-4>"), add="+")
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<Button-5>"), add="+")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        images = []
        try:
            document = fitz.open(pdf_path)
        except Exception as exc:
            error = ttk.Label(scroll_frame, text=f"No se pudo abrir el PDF: {exc}", padding=16)
            error.pack(anchor="w")
            frame._pdf_images = images  # type: ignore[attr-defined]
            return

        scale = 1.5
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

        frame._pdf_images = images  # type: ignore[attr-defined]
        try:
            document.close()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    app = PdfViewerApp()
    app.run()


if __name__ == "__main__":
    main()
