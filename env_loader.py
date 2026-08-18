# -*- coding: utf-8 -*-
"""
env_loader.py — Carga el archivo .env en os.environ.

DEBE importarse ANTES que cualquier módulo que lea configuración (auth,
github_trigger, ...), por eso es el primer import de gui.py. Sin dependencias
externas (no usa python-dotenv). No pisa variables que ya existan en el entorno
real del sistema, así que en el runner de GitHub mandan los Secrets.

En el .exe empaquetado, el .env viaja dentro (datas del .spec) y `resource_path`
lo resuelve vía sys._MEIPASS; corriendo desde el código, lo lee de la raíz.
"""
import os

from helpers import resource_path


def cargar_env():
    ruta = resource_path(".env")
    if not os.path.exists(ruta):
        return
    try:
        with open(ruta, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                clave, _, valor = linea.partition("=")
                clave = clave.strip()
                valor = valor.strip().strip('"').strip("'")
                if clave and clave not in os.environ:
                    os.environ[clave] = valor
    except Exception:
        pass


cargar_env()
