# -*- coding: utf-8 -*-
"""
generar_fichas_sinteticas.py — Las fichas raras de la prueba de contrato.

Las fichas reales del buscador casi nunca traen los casos difíciles (entre 57
bajadas el 2026-09-11 no apareció ninguno), así que se fabrican aquí: nulos,
sin productos, espacios que Python y JavaScript no quitan igual, cantidades de
todo tipo, productos repetidos.

Se generan con código y se escriben con `ensure_ascii`: los caracteres raros
quedan escapados (\\u00a0) en vez de invisibles en el archivo, que es donde
una ficha a mano se rompió la primera vez.

Uso:  python pruebas/generar_fichas_sinteticas.py
"""
import json
import os

CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fichas_compra_agil")

NBSP, SEP_ARCHIVO, EM_ESPACIO, BOM, TAB = chr(0xa0), chr(0x1c), chr(0x2003), chr(0xfeff), chr(9)

FICHAS = {
    # Nulos en productos: Python guarda "None" (str(None)), y por eso la segunda
    # fila se repite con la primera y se va.
    "sintetica_nulos": {
        "codigo": "9999-1-COT26",
        "nombre": "Prueba: productos con valores nulos",
        "descripcion": "Descripción general",
        "informacion_institucion": {"organismo_comprador": "Municipalidad de Prueba",
                                    "rut_organismo_comprador": "69.000.000-0"},
        "fecha_publicacion": "2026-09-11 10:00",
        "fecha_cierre": "2026-09-12 10:00",
        "fecha_cierre_primer_llamado": "2026-09-12 10:00",
        "fecha_cierre_segundo_llamado": None,
        "estado_convocatoria": 1,
        "productos_solicitados": [
            {"nombre": None, "descripcion": None, "cantidad": None},
            {"nombre": "Guantes", "descripcion": None, "cantidad": 5},
            {"nombre": None, "descripcion": "Mascarillas", "cantidad": 3},
        ],
    },
    # Sin productos: una fila con la descripción general. Sin institución y sin
    # primer cierre (se usa `fecha_cierre`).
    "sintetica_sin_productos": {
        "codigo": "9999-2-COT26",
        "nombre": "Prueba: sin productos, institución nula y sin primer cierre",
        "descripcion": "  Servicio de aseo_x000D_ mensual  ",
        "informacion_institucion": None,
        "fecha_publicacion": "2026-09-11 11:00",
        "fecha_cierre": "2026-09-13 18:00",
        "fecha_cierre_segundo_llamado": "",
        "productos_solicitados": [],
    },
    # Casi nada: sin código, todo nulo, convocatoria como texto ("1" no es 1).
    "sintetica_sin_nada": {
        "nombre": None,
        "descripcion": None,
        "informacion_institucion": {"organismo_comprador": None},
        "fecha_publicacion": None,
        "fecha_cierre_primer_llamado": "",
        "fecha_cierre": None,
        "estado_convocatoria": "1",
        "productos_solicitados": None,
    },
    # Espacios que Python quita y JavaScript no (y al revés), _x000D_, 2do
    # llamado, RUT numérico y cantidades de todo tipo.
    "sintetica_espacios_y_cantidades": {
        "codigo": "9999-3-COT26",
        "nombre": "Prueba: espacios raros, _x000D_ y cantidades de todo tipo",
        "descripcion": "",
        "informacion_institucion": {"organismo_comprador": "Hospital de Prueba",
                                    "rut_organismo_comprador": 61606606},
        "fecha_publicacion": "2026-09-11 12:00",
        "fecha_cierre": "2026-09-15 15:00",
        "fecha_cierre_primer_llamado": "2026-09-12 15:00",
        "fecha_cierre_segundo_llamado": "2026-09-15 15:00",
        "estado_convocatoria": 2,
        "productos_solicitados": [
            {"nombre": TAB + " Guantes de nitrilo" + NBSP,
             "descripcion": SEP_ARCHIVO + " Talla M_x000D_ " + EM_ESPACIO, "cantidad": 2.5},
            {"nombre": "Jabón", "descripcion": "", "cantidad": 3.0},
            {"nombre": "Alcohol gel", "descripcion": "Botella 1 L", "cantidad": "12"},
            {"nombre": "Toallas", "descripcion": "Papel" + BOM, "cantidad": True},
            {"nombre": "Cloro", "descripcion": "Bidón 5 L", "cantidad": 100},
        ],
    },
    # Repetidos: se queda la primera de cada (código, descripción). Una
    # descripción vacía cae al nombre, y un «producto» que no es un objeto.
    "sintetica_duplicadas": {
        "codigo": "9999-4-COT26",
        "nombre": "Prueba: productos que se repiten",
        "descripcion": "No debería usarse: hay productos",
        "informacion_institucion": {"organismo_comprador": "Servicio de Prueba",
                                    "rut_organismo_comprador": "61.000.000-1"},
        "fecha_publicacion": "2026-09-11 13:00",
        "fecha_cierre_primer_llamado": "2026-09-12 13:00",
        "estado_convocatoria": 1,
        "productos_solicitados": [
            {"nombre": "A", "descripcion": "Papel", "cantidad": 1},
            {"nombre": "B", "descripcion": "Papel", "cantidad": 2},
            {"nombre": "Papel", "descripcion": "", "cantidad": 4},
            {"nombre": "C", "descripcion": "  papel ", "cantidad": 1},
        ],
    },
}


def main():
    os.makedirs(CARPETA, exist_ok=True)
    for nombre, payload in FICHAS.items():
        ruta = os.path.join(CARPETA, nombre + ".json")
        with open(ruta, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"success": "OK", "payload": payload}, f, ensure_ascii=True, indent=1)
            f.write("\n")
        print("escrita", os.path.basename(ruta))


if __name__ == "__main__":
    main()
