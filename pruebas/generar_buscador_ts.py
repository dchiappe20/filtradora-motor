# -*- coding: utf-8 -*-
"""
generar_buscador_ts.py — Escribe supabase/functions/compra-agil-reciente/buscador.ts
a partir de las constantes de compra_agil_api.py.

La función de Supabase tiene que hablarle al buscador EXACTAMENTE como el Python
del motor (misma URL, mismas cabeceras, mismo tamaño de página). En vez de
copiarlas a mano, se generan desde el Python, y la prueba de contrato comprueba
que sigan coincidiendo.

Uso:  python pruebas/generar_buscador_ts.py
"""
import ast
import json
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOMBRES = ("API_URL", "API_KEY", "_HEADERS", "ESTADO_PUBLICADA", "PAGINA_LISTADO", "MAX_WORKERS")


def constantes():
    """Las constantes del buscador, tal como las ve el Python con su valor por
    defecto (sin la variable de entorno que puede sobreescribir la llave)."""
    os.environ.pop("MP_BUSCADOR_API_KEY", None)
    fuente = open(os.path.join(RAIZ, "compra_agil_api.py"), encoding="utf-8").read()
    ns = {"os": os}
    for n in ast.parse(fuente).body:
        if (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in NOMBRES):
            exec(compile(ast.Module(body=[n], type_ignores=[]), "compra_agil_api.py", "exec"), ns)
    faltan = [k for k in NOMBRES if k not in ns]
    if faltan:
        raise SystemExit(f"no encontradas en compra_agil_api.py: {faltan}")
    clave_header = next((k for k, v in ns["_HEADERS"].items() if v == ns["API_KEY"]), "")
    return {
        "URL_BUSCADOR": ns["API_URL"],
        "HEADERS_BUSCADOR": ns["_HEADERS"],
        "CLAVE_HEADER_API": clave_header,
        "ESTADO_PUBLICADA": ns["ESTADO_PUBLICADA"],
        "PAGINA_LISTADO": ns["PAGINA_LISTADO"],
        "HILOS": ns["MAX_WORKERS"],
    }


def main():
    c = constantes()
    ts = (
        "// buscador.ts — GENERADO por pruebas/generar_buscador_ts.py a partir de\n"
        "// compra_agil_api.py. NO EDITAR A MANO: si cambia el Python, se vuelve a\n"
        "// generar. La prueba de contrato comprueba que coinciden.\n\n"
        f"export const URL_BUSCADOR = {json.dumps(c['URL_BUSCADOR'])};\n"
        f"export const HEADERS_BUSCADOR: Record<string, string> = "
        f"{json.dumps(c['HEADERS_BUSCADOR'], ensure_ascii=False, indent=2)};\n"
        f"export const CLAVE_HEADER_API = {json.dumps(c['CLAVE_HEADER_API'])};\n"
        f"export const ESTADO_PUBLICADA = {json.dumps(c['ESTADO_PUBLICADA'])};\n"
        f"export const PAGINA_LISTADO = {json.dumps(c['PAGINA_LISTADO'])};\n"
        f"export const HILOS = {json.dumps(c['HILOS'])};\n"
    )
    destino = os.path.join(RAIZ, "supabase", "functions", "compra-agil-reciente", "buscador.ts")
    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        f.write(ts)
    print(f"escrito {os.path.relpath(destino, RAIZ)}")


if __name__ == "__main__":
    main()
