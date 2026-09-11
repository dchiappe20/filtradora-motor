# -*- coding: utf-8 -*-
"""
filas_python.py — Lo que el PYTHON del motor guardaría para cada ficha de ejemplo.

Es la mitad Python de la prueba de contrato: la otra mitad
(`contrato_fichas.test.ts`) pasa las mismas fichas por la función de Supabase y
exige el mismo resultado, fila a fila y carácter a carácter.

Se ejecuta el código REAL del motor —`_procesar_ficha`, el `drop_duplicates` del
lote y `_a_filas`—, sacado del fuente con `ast` en vez de importando los módulos:
importarlos abriría la conexión a Supabase, que aquí no hace falta ni existe.

Cada ficha va sola, como un lote de una. Así el resultado no depende de las
demás fichas del ejemplo.

Uso:  python pruebas/filas_python.py > esperado.json
"""
import ast
import glob
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generar_buscador_ts import constantes  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extraer(archivo, nombres):
    arbol = ast.parse(open(os.path.join(RAIZ, archivo), encoding="utf-8").read())
    cuerpo, vistos = [], set()
    for n in arbol.body:
        nombre = (n.name if isinstance(n, ast.FunctionDef)
                  else n.targets[0].id if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                  else None)
        if nombre in nombres:
            cuerpo.append(n)
            vistos.add(nombre)
    faltan = set(nombres) - vistos
    if faltan:
        raise SystemExit(f"no encontrado en {archivo}: {sorted(faltan)}")
    return cuerpo


def main():
    ns = {"pd": pd}
    for archivo, nombres in (
        ("compra_agil_api.py", ["_LLAMADO_TEXTOS", "COLUMNAS_COMPLETAS", "TABLA_NUBE",
                                "_texto_cantidad", "_procesar_ficha"]),
        ("datos_nube.py", ["MAPEO_COMPRA_AGIL", "_limpiar_celda", "_a_filas"]),
    ):
        exec(compile(ast.Module(body=_extraer(archivo, nombres), type_ignores=[]), archivo, "exec"), ns)
    ns["_MAPEOS"] = {ns["TABLA_NUBE"]: ns["MAPEO_COMPRA_AGIL"]}

    salida = {"__constantes__": constantes()}
    for ruta in sorted(glob.glob(os.path.join(RAIZ, "pruebas", "fichas_compra_agil", "*.json"))):
        with open(ruta, encoding="utf-8") as f:
            ficha = json.load(f)["payload"]
        df = pd.DataFrame(ns["_procesar_ficha"](ficha), columns=ns["COLUMNAS_COMPLETAS"])
        if not df.empty:
            df = df.drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"])
        salida[os.path.basename(ruta)] = ns["_a_filas"](ns["TABLA_NUBE"], df, None)

    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(salida, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
