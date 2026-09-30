from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import Canvas, Label, Tk, messagebox

from version import APP_VERSION


REPOSITORIO = "Erin3012/robot_manuales"
API_RELEASE_LATEST = f"https://api.github.com/repos/{REPOSITORIO}/releases/latest"
NOMBRE_EXE = "Robot_Manuales_v{version}.exe"
MINIMO_EXE_BYTES = 100 * 1024
TIEMPO_MINIMO_ACTUALIZADOR = 1.5


class ErrorActualizacion(Exception):
    """Error controlado durante la verificacion o instalacion."""


class VentanaActualizacion:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Robot Manuales - Actualización")
        self.root.geometry("560x220")
        self.root.resizable(False, False)
        self.root.configure(bg="#f3f6fb")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.inicio = time.monotonic()

        self.root.update_idletasks()
        ancho = self.root.winfo_screenwidth()
        alto = self.root.winfo_screenheight()
        self.root.geometry(f"560x220+{(ancho - 560) // 2}+{(alto - 220) // 2}")

        encabezado = Canvas(self.root, width=560, height=78, bg="#ffffff", highlightthickness=0)
        encabezado.pack(fill="x")
        encabezado.create_oval(24, 20, 58, 54, fill="#0b74c9", outline="")
        encabezado.create_text(41, 37, text="R", fill="#ffffff", font=("Segoe UI", 14, "bold"))
        encabezado.create_text(74, 32, text="Robot Manuales", anchor="w", fill="#17365d", font=("Segoe UI", 19, "bold"))
        encabezado.create_text(535, 34, text=f"v{APP_VERSION}", anchor="e", fill="#17365d", font=("Segoe UI", 10, "bold"))

        tarjeta = Canvas(self.root, width=500, height=92, bg="#ffffff", highlightthickness=1, highlightbackground="#dce6f2")
        tarjeta.pack(pady=(16, 0))
        self.estado = Label(tarjeta, text="Verificando actualizaciones...", bg="#ffffff", fg="#60738e", font=("Segoe UI", 10))
        self.estado.place(x=20, y=17, anchor="w")
        self.lienzo = Canvas(tarjeta, width=390, height=18, bg="#ffffff", highlightthickness=0)
        self.lienzo.place(x=20, y=48)
        self.lienzo.create_rectangle(0, 2, 390, 16, fill="#e4ebf4", outline="")
        self.barra = self.lienzo.create_rectangle(0, 2, 0, 16, fill="#1677d2", outline="")
        self.porcentaje = Label(tarjeta, text="0%", bg="#ffffff", fg="#1677d2", font=("Segoe UI", 10, "bold"))
        self.porcentaje.place(x=465, y=56, anchor="e")
        self.root.update_idletasks()

    def mensaje(self, texto: str) -> None:
        self.estado.configure(text=texto)
        self.root.update_idletasks()

    def avance(self, actual: int, total: int) -> None:
        if total > 0:
            porcentaje = min(100, int(actual * 100 / total))
        else:
            porcentaje = min(95, int(self.progreso_actual + 1))
        self.set_progress(porcentaje)

    @property
    def progreso_actual(self) -> int:
        try:
            return int(self.porcentaje.cget("text").rstrip("%"))
        except (TypeError, ValueError):
            return 0

    def set_progress(self, porcentaje: int) -> None:
        porcentaje = max(0, min(100, int(porcentaje)))
        self.lienzo.coords(self.barra, 0, 2, int(390 * porcentaje / 100), 16)
        self.porcentaje.configure(text=f"{porcentaje}%")
        self.root.update_idletasks()

    def completar(self) -> None:
        while time.monotonic() - self.inicio < TIEMPO_MINIMO_ACTUALIZADOR:
            self.root.update_idletasks()
            time.sleep(0.05)
        self.set_progress(100)

    def cerrar(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


def _solicitar_json(url: str) -> dict:
    solicitud = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Robot-Manuales-Updater",
        },
    )
    try:
        with urllib.request.urlopen(solicitud, timeout=20) as respuesta:
            return json.loads(respuesta.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
        raise ErrorActualizacion(f"No se pudo consultar GitHub: {error}") from error


def _version_desde_tag(tag: object) -> int:
    coincidencia = re.fullmatch(r"v(\d+)", str(tag or "").strip())
    if not coincidencia:
        raise ErrorActualizacion(f"El release de GitHub tiene un tag inválido: {tag!r}")
    return int(coincidencia.group(1))


def _asset_exe(release: dict, version: int) -> dict:
    nombre_esperado = NOMBRE_EXE.format(version=version).lower()
    for asset in release.get("assets") or []:
        if str(asset.get("name", "")).lower() == nombre_esperado:
            return asset
    raise ErrorActualizacion(f"La release no contiene el archivo {NOMBRE_EXE.format(version=version)}.")


def _descargar_exe(url: str, destino: Path, ventana: VentanaActualizacion) -> None:
    solicitud = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "Robot-Manuales-Updater",
        },
    )
    try:
        with urllib.request.urlopen(solicitud, timeout=120) as respuesta:
            total = int(respuesta.headers.get("Content-Length") or 0)
            descargado = 0
            with destino.open("wb") as archivo:
                while True:
                    bloque = respuesta.read(1024 * 1024)
                    if not bloque:
                        break
                    archivo.write(bloque)
                    descargado += len(bloque)
                    ventana.avance(descargado, total)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as error:
        raise ErrorActualizacion(f"No se pudo descargar la actualización: {error}") from error

    if not destino.is_file() or destino.stat().st_size < MINIMO_EXE_BYTES:
        raise ErrorActualizacion("La descarga de la actualización está incompleta o es inválida.")


def _iniciar_helper(helper: Path, origen: Path, destino: Path) -> None:
    if not helper.is_file():
        raise ErrorActualizacion("Falta updater_helper.exe junto al programa.")
    argumentos = [str(helper), "--pid", str(os.getpid()), "--source", str(origen), "--target", str(destino)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(argumentos, creationflags=flags, close_fds=True, cwd=str(destino.parent))
    except OSError as error:
        raise ErrorActualizacion(f"No se pudo iniciar el instalador auxiliar: {error}") from error


def verificar_actualizacion_obligatoria() -> bool:
    """Devuelve True si la app puede continuar; False si debe terminar."""
    if not getattr(sys, "frozen", False):
        return True

    ventana = VentanaActualizacion()
    try:
        ventana.mensaje("Consultando la última versión...")
        release = _solicitar_json(API_RELEASE_LATEST)
        version_remota = _version_desde_tag(release.get("tag_name"))

        if version_remota <= APP_VERSION:
            ventana.mensaje("La aplicación ya está actualizada.")
            ventana.completar()
            ventana.cerrar()
            return True

        asset = _asset_exe(release, version_remota)
        url = str(asset.get("browser_download_url") or "").strip()
        if not url.startswith("https://"):
            raise ErrorActualizacion("La release no tiene una URL HTTPS válida para el EXE.")

        carpeta_temp = Path(tempfile.mkdtemp(prefix="robot_manuales_update_"))
        descarga = carpeta_temp / NOMBRE_EXE.format(version=version_remota)
        ventana.mensaje(f"Descargando la versión v{version_remota}...")
        _descargar_exe(url, descarga, ventana)

        destino = Path(os.path.abspath(sys.executable))
        helper = destino.parent / "updater_helper.exe"
        ventana.set_progress(95)
        ventana.mensaje("Aplicando actualización y reiniciando...")
        _iniciar_helper(helper, descarga, destino)
        ventana.completar()
        ventana.cerrar()
        return False
    except (ErrorActualizacion, OSError) as error:
        ventana.completar()
        ventana.cerrar()
        messagebox.showerror("Actualización obligatoria", f"No se puede iniciar Robot Manuales hasta actualizarlo.\n\n{error}")
        return False
    except Exception as error:
        ventana.completar()
        ventana.cerrar()
        messagebox.showerror("Actualización obligatoria", f"No se pudo verificar la versión de Robot Manuales.\n\n{error}")
        return False
