import os
import sys
import json
import uuid


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def _get_appdata_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    carpeta = os.path.join(base, "Amilab")
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def obtener_id_pc():
    return str(uuid.getnode())


def id_corto(valor) -> str:
    """Primeros 8 caracteres de un UUID, para escribir en logs.

    Los logs de GitHub Actions de un repositorio publico los ve cualquiera. Un
    UUID de empresa completo es un identificador utilizable; ocho caracteres
    bastan para seguir una corrida en el log y no sirven para nada mas.
    """
    texto = str(valor or "").strip()
    return texto[:8] if texto else "?"


def ruta_sesion():
    return os.path.join(_get_appdata_dir(), "sesion_amilab.json")


def leer_sesion_cruda():
    """Contenido del archivo de sesión tal cual, sin validar nada.

    Es la que hay que usar para ACTUALIZAR la sesión: los tokens de Supabase
    Auth y los datos del usuario se escriben en momentos distintos, y validar
    aquí haría que un guardado descartara lo que escribió el otro.
    """
    try:
        with open(ruta_sesion(), "r", encoding="utf-8") as f:
            datos = json.load(f)
    except Exception:
        return {}

    return datos if isinstance(datos, dict) else {}


def leer_sesion():
    """Sesión válida de este equipo. Devuelve {} si no hay o no sirve.

    Vive aquí y no en auth.py a propósito: gui.py la consulta al arrancar para
    decidir si mostrar el login, y auth.py arrastra supabase/cryptography, cuyo
    import es lento en discos de red. Esta función sólo toca un JSON.
    """
    datos = leer_sesion_cruda()

    if not datos.get("email"):
        return {}

    # Una sesión copiada desde otro PC no vale: el hwid tiene que coincidir.
    if datos.get("hwid") and datos["hwid"] != obtener_id_pc():
        return {}

    return datos


# La conciencia de DPI la fija `theme.preparar_dpi()` antes de crear la ventana.
# Aquí había un SetProcessDpiAwareness(1) (consciente del DPI del sistema) que,
# por ejecutarse al importar, ganaba siempre: con la escala de Windows al 125% o
# 150% las fuentes crecían dentro de cajas del mismo tamaño y los diálogos
# aparecían cortados. Se declara como NO consciente para que Windows agrande la
# ventana entera y todo quede proporcionado.
