"""Prueba explícita contra SITFA usando SITFA_USER/SITFA_PASS del .env local.

    py tests/verify_cons_lit_live.py --rit Z-2248-2026
    py tests/verify_cons_lit_live.py --pendientes

No imprime credenciales, nombres ni RUT de personas. Guarda solo conteos y
resultados por causa en debug_dumps (ignorado por Git).
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def open_session():
    driver = main.create_driver(initial_url=main.LOGIN_URL, browser="chrome", headless=True)
    try:
        main.login_to_sitfa(driver, os.environ["SITFA_USER"], os.environ["SITFA_PASS"])
        return driver
    except Exception:
        driver.quit()
        raise


def check_batch(rits):
    results = []
    driver = open_session()
    try:
        for rit in rits:
            start = time.perf_counter()
            try:
                main.ensure_query_form_ready(driver, timeout=15)
                driver.switch_to.default_content()
                main.submit_case_query(driver, *main.parse_rit_value(rit))
                handle = driver.current_window_handle
                handles = set(driver.window_handles)
                litigantes, rows = main.collect_litigantes_and_cons_lit_rows(driver)
                demandados = [r for r in litigantes if main.normalize_header_name(r.get("Sujeto", "")) == "ddo"]
                eligible = []
                for row in demandados:
                    args = re.findall(r"'([^']*)'", row.get("__onclick", ""))
                    if len(args) >= 8 and args[6] == "0" and args[7] == "N":
                        eligible.append(row)
                if not litigantes:
                    raise AssertionError("No se extrajeron litigantes")
                if eligible and not rows:
                    raise AssertionError("Demandado vigente sin resultados")
                if any(not r.get("RIT") or not r.get("Tribunal") for r in rows):
                    raise AssertionError("Fila incompleta")
                if handle != driver.current_window_handle or handles != set(driver.window_handles):
                    raise AssertionError("La consulta no restauró sus ventanas")
                state = "con_resultados" if rows else ("sin_demandado" if not demandados else "no_habilitado_por_sitfa")
                result = {"rit": rit, "estado": state, "litigantes": len(litigantes), "filas": len(rows)}
            except Exception as exc:
                result = {"rit": rit, "estado": "error", "tipo_error": type(exc).__name__}
                result['trace'] = [
                    {'archivo': Path(frame.filename).name, 'linea': frame.lineno, 'funcion': frame.name}
                    for frame in traceback.extract_tb(exc.__traceback__)
                ]
            result["segundos"] = round(time.perf_counter() - start, 2)
            results.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        driver.quit()
    return results


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rit", action="append", default=[])
    parser.add_argument("--pendientes", action="store_true")
    options = parser.parse_args()
    if not options.rit and not options.pendientes:
        parser.error("Indica --rit o --pendientes")
    if not os.getenv("SITFA_USER") or not os.getenv("SITFA_PASS"):
        parser.error("Configura SITFA_USER y SITFA_PASS en el .env local")
    rits = options.rit[:]
    if options.pendientes:
        driver = open_session()
        try:
            rits.extend(r["RIT"] for r in main.collect_pending_case_rows(driver))
        finally:
            driver.quit()
    rits = list(dict.fromkeys(rits))
    print("CAUSAS_A_PROBAR", len(rits), flush=True)
    # Una sola sesión, como el worker de la aplicación. Consultas simultáneas
    # con la misma cuenta introducen interferencias y timeouts en SITFA.
    results = check_batch(rits) if rits else []
    counts = {state: sum(r["estado"] == state for r in results) for state in (
        "con_resultados", "sin_demandado", "no_habilitado_por_sitfa", "error",
    )}
    report = {"total": len(results), "conteos": counts, "resultados": results}
    report_path = Path(__file__).resolve().parents[1] / "debug_dumps" / "cons_lit_verificacion.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print("RESUMEN", json.dumps(counts), flush=True)
    return int(bool(counts["error"]))


if __name__ == "__main__":
    raise SystemExit(run())
