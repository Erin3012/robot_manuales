from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import time
from pathlib import Path


def _proceso_termino(pid: int) -> bool:
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    proceso = kernel32.OpenProcess(0x00100000, False, pid)
    if not proceso:
        return True
    try:
        return kernel32.WaitForSingleObject(proceso, 0) != 0x00000102
    finally:
        kernel32.CloseHandle(proceso)


def reemplazar_y_reiniciar(pid: int, source: Path, target: Path) -> None:
    for _ in range(120):
        if _proceso_termino(pid):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("El programa anterior no terminó a tiempo.")

    if not source.is_file():
        raise FileNotFoundError(f"No existe la actualización descargada: {source}")
    if not target.parent.exists():
        raise FileNotFoundError(f"No existe la carpeta destino: {target.parent}")

    ultimo_error: OSError | None = None
    staged = target.with_name(target.name + ".update")
    for _ in range(20):
        try:
            shutil.copyfile(source, staged)
            os.replace(staged, target)
            ultimo_error = None
            break
        except OSError as error:
            ultimo_error = error
            time.sleep(0.5)
    if ultimo_error:
        raise ultimo_error

    try:
        source.unlink(missing_ok=True)
        source.parent.rmdir()
    except OSError:
        pass

    subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    try:
        reemplazar_y_reiniciar(args.pid, Path(args.source), Path(args.target))
    except Exception as error:
        target = Path(args.target)
        try:
            (target.parent / "actualizacion_error.log").write_text(str(error), encoding="utf-8")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
