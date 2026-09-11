# -*- coding: utf-8 -*-
"""
filas_python_licitaciones.py — Lo que el PYTHON del motor haría con cada
respuesta de ejemplo de la API de licitaciones.

Es la mitad Python de `contrato_licitaciones.test.ts`: la otra mitad pasa las
mismas respuestas por la función de Supabase `licitaciones-reciente` y exige el
mismo estado y las mismas filas, carácter a carácter.

Se ejecuta el código REAL del motor —`_filas_de_respuesta`, el `drop_duplicates`
del lote y `_a_filas`—, sacado del fuente con `ast` para no abrir la conexión a
Supabase que traería importar los módulos.

Uso:  python pruebas/filas_python_licitaciones.py > esperado_licitaciones.json
"""
import ast
import glob
import json
import os
import sys

import pandas as pd

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA = os.path.join(RAIZ, "pruebas", "fichas_licitaciones")


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


def cargar(ruta):
    """(código, día de publicación esperado, respuesta) de un ejemplo.

    Los ejemplos hechos a mano lo dicen explícito; los reales son la respuesta
    tal cual, y el código y el día salen del nombre del archivo y de la ficha."""
    with open(ruta, encoding="utf-8") as f:
        obj = json.load(f)
    if "respuesta" in obj:
        return obj["codigo"], obj["fecha"], obj["respuesta"]
    codigo = os.path.splitext(os.path.basename(ruta))[0]
    fecha = str(obj["Listado"][0]["Fechas"]["FechaPublicacion"])[:10]
    return codigo, fecha, obj


def main():
    ns = {"pd": pd}
    for archivo, nombres in (
        ("api_client.py", ["URL_API", "MAX_WORKERS_LICIT", "TABLA_NUBE", "COLUMNAS_COMPLETAS",
                           "_texto_cantidad", "_filas_de_respuesta"]),
        ("datos_nube.py", ["MAPEO_LICITACIONES", "_limpiar_celda", "_a_filas"]),
    ):
        exec(compile(ast.Module(body=_extraer(archivo, nombres), type_ignores=[]), archivo, "exec"), ns)
    ns["_MAPEOS"] = {ns["TABLA_NUBE"]: ns["MAPEO_LICITACIONES"]}

    salida = {"__constantes__": {"URL_API": ns["URL_API"], "HILOS": ns["MAX_WORKERS_LICIT"]}}
    for ruta in sorted(glob.glob(os.path.join(CARPETA, "*.json"))):
        nombre = os.path.basename(ruta)
        codigo, fecha, respuesta = cargar(ruta)
        try:
            filas, estado = ns["_filas_de_respuesta"](respuesta, codigo, fecha)
        except Exception:
            salida[nombre] = {"estado": "EXCEPCION"}
            continue
        df = pd.DataFrame(filas, columns=ns["COLUMNAS_COMPLETAS"])
        if not df.empty:
            df = df.drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"])
        salida[nombre] = {"estado": estado, "filas": ns["_a_filas"](ns["TABLA_NUBE"], df, None)}

    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(salida, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
