# -*- coding: utf-8 -*-
"""
generar_fichas_licitaciones.py — Las respuestas raras de la API de licitaciones
para la prueba de contrato.

Las reales casi nunca traen los casos difíciles (entre 29 bajadas el 2026-09-11
no apareció ninguno), así que se fabrican aquí. Se escriben con `ensure_ascii`:
los caracteres raros quedan escapados en vez de invisibles en el archivo.

Cada ejemplo lleva el código y el día contra el que se compara la fecha de
publicación, como los recibe `_filas_de_respuesta`.

Uso:  python pruebas/generar_fichas_licitaciones.py
"""
import json
import os

CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fichas_licitaciones")
NBSP, SEP_ARCHIVO, BOM, EM_ESPACIO = chr(0xa0), chr(0x1c), chr(0xfeff), chr(0x2003)
HOY = "2026-09-11"


def respuesta(detalle):
    return {"Cantidad": 1, "Version": "v1", "Listado": [detalle]}


def detalle(**cambios):
    base = {
        "CodigoExterno": "9999-1-LE26",
        "Nombre": "Licitación de prueba",
        "CodigoEstado": 5,
        "Comprador": {"NombreOrganismo": "Hospital de Prueba", "RutUnidad": "61.606.606-4"},
        "Fechas": {"FechaPublicacion": HOY + "T10:30:00.47", "FechaCierre": "2026-09-25T15:00:00"},
        "Items": {"Cantidad": 1, "Listado": [
            {"NombreProducto": "Guantes", "Descripcion": "Guantes de nitrilo talla M", "Cantidad": 100},
        ]},
    }
    base.update(cambios)
    return base


EJEMPLOS = {
    # La API manda un solo ítem como objeto y no como lista.
    "sintetica_items_como_objeto": respuesta(detalle(Items={"Cantidad": 1, "Listado": {
        "NombreProducto": "Mascarillas", "Descripcion": "Caja de 50", "Cantidad": 3}})),

    # Descripciones que no dicen nada: se usa el nombre, y no entran al texto de filtrar.
    "sintetica_descripciones_vacias": respuesta(detalle(Items={"Listado": [
        {"NombreProducto": "Jabón", "Descripcion": "Sin descripción", "Cantidad": 1},
        {"NombreProducto": "Cloro", "Descripcion": "SIN DESCRIPCION", "Cantidad": 2},
        {"NombreProducto": "Papel", "Descripcion": "N/A", "Cantidad": 3},
        {"NombreProducto": "Alcohol", "Descripcion": None, "Cantidad": 4},
        {"NombreProducto": "none", "Descripcion": "", "Cantidad": 5},
        {"NombreProducto": None, "Descripcion": "Guantes quirúrgicos", "Cantidad": 6},
    ]})),

    # Espacios que Python y JavaScript no quitan igual, _x000D_, y cantidades de todo tipo.
    "sintetica_espacios_y_cantidades": respuesta(detalle(Items={"Listado": [
        {"NombreProducto": " Gasa" + NBSP, "Descripcion": SEP_ARCHIVO + " Estéril_x000D_ " + EM_ESPACIO, "Cantidad": 2.5},
        {"NombreProducto": "Vendas", "Descripcion": "Elásticas" + BOM, "Cantidad": 3.0},
        {"NombreProducto": "Suero", "Descripcion": "1 L", "Cantidad": "12"},
        {"NombreProducto": "Jeringas", "Descripcion": "5 ml", "Cantidad": None},
        {"NombreProducto": "Agujas", "Descripcion": "21G"},
        {"NombreProducto": "Algodón", "Descripcion": 123, "Cantidad": True},
    ]})),

    # Repetidos (se queda la primera) y nombre de la licitación nulo.
    "sintetica_duplicadas": respuesta(detalle(Nombre=None, Items={"Listado": [
        {"NombreProducto": "A", "Descripcion": "Papel", "Cantidad": 1},
        {"NombreProducto": "B", "Descripcion": "Papel", "Cantidad": 2},
        {"NombreProducto": "Papel", "Descripcion": "", "Cantidad": 4},
    ]})),

    # Sin comprador y sin ítems: OK, pero sin filas.
    "sintetica_sin_items_ni_comprador": respuesta(detalle(Comprador=None, Items=None)),
    "sintetica_items_listado_nulo": respuesta(detalle(Items={"Listado": None})),

    # El listado del día trae licitaciones publicadas OTRO día.
    "sintetica_otra_fecha": respuesta(detalle(Fechas={"FechaPublicacion": "2026-08-02T09:00:00",
                                                      "FechaCierre": "2026-09-30T15:00:00"})),
    "sintetica_publicacion_nula": respuesta(detalle(Fechas={"FechaPublicacion": None})),

    # Sin detalle, y el portal saturado.
    "sintetica_vacio": {"Cantidad": 0, "Listado": []},
    "sintetica_mensaje": {"Codigo": 203, "Mensaje": "Lo sentimos. Hemos detectado que existen peticiones simultáneas."},

    # Donde el Python lanzaría (y reintentaría): Fechas nula.
    "sintetica_fechas_nulas": respuesta(detalle(Fechas=None)),
}


def main():
    os.makedirs(CARPETA, exist_ok=True)
    for nombre, resp in EJEMPLOS.items():
        with open(os.path.join(CARPETA, nombre + ".json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump({"codigo": "9999-1-LE26", "fecha": HOY, "respuesta": resp}, f,
                      ensure_ascii=True, indent=1)
            f.write("\n")
        print("escrita", nombre)


if __name__ == "__main__":
    main()
