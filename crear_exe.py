from __future__ import annotations

import os
import re
from pathlib import Path

import PyInstaller.__main__


ROOT = Path(__file__).resolve().parent
ARCHIVO_VERSION = ROOT / "build_version.txt"
ARCHIVO_VERSION_PY = ROOT / "version.py"
VERSION_FORZADA = os.environ.get("ROBOT_MANUALES_RELEASE_VERSION", "").strip()


def obtener_version() -> int:
    if VERSION_FORZADA:
        if not re.fullmatch(r"\d+", VERSION_FORZADA):
            raise ValueError("ROBOT_MANUALES_RELEASE_VERSION debe ser un número, por ejemplo 1")
        return int(VERSION_FORZADA)

    try:
        actual = int(ARCHIVO_VERSION.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        actual = 0
    return actual + 1


def escribir_version(version: int) -> None:
    ARCHIVO_VERSION.write_text(f"{version}\n", encoding="utf-8")
    ARCHIVO_VERSION_PY.write_text(
        '"""Versión generada durante el build."""\n\n'
        f"APP_VERSION = {version}\n",
        encoding="utf-8",
    )


def construir(script: str, nombre: str, incluir_dependencias: bool = False) -> None:
    opciones = [
        script,
        f"--name={nombre}",
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
    ]
    if incluir_dependencias:
        opciones.extend(
            [
                "--hidden-import=playwright.sync_api",
                "--hidden-import=greenlet",
                "--hidden-import=fitz",
                "--hidden-import=pymupdf",
                "--hidden-import=PIL._tkinter_finder",
                "--collect-all=fitz",
                "--collect-all=pymupdf",
            ]
        )
    PyInstaller.__main__.run(opciones)


def main() -> None:
    version = obtener_version()
    escribir_version(version)
    nombre_exe = f"Robot_Manuales_v{version}"
    print(f"Construyendo {nombre_exe}.exe...")
    construir("updater_helper.py", "updater_helper")
    construir("app_tk.py", nombre_exe, incluir_dependencias=True)
    print(f"Build terminado. Archivos en: {ROOT / 'dist'}")


if __name__ == "__main__":
    main()
