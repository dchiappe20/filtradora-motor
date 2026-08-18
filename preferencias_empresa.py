# -*- coding: utf-8 -*-
"""
preferencias_empresa.py — Preferencias compartidas por toda la empresa.

Por ahora una sola: qué llamados de Compra Ágil seguir. No es una preferencia
de interfaz (ésas van en `prefs_ui.py`, por PC): la usa el barrido en la nube
para decidir qué cotizaciones vigilar, así que tiene que estar donde el runner
pueda leerla y ser la misma para todo el equipo.

La escritura pasa por `guardar_preferencia_llamado`, una función de base de
datos que comprueba el rol: la tabla no tiene policy de escritura para que un
usuario normal no pueda saltarse esa comprobación escribiendo directo.
"""
import os
import threading

from auth import supabase, ejecutar_con_refresco

TABLA = "preferencias_empresa"

# Valor guardado -> texto para la interfaz.
LLAMADOS = {
    "primer":  "Sólo publicadas de primer llamado",
    "segundo": "Sólo publicadas de segundo llamado",
    "todas":   "Todas las publicadas",
}

POR_DEFECTO = "todas"

_lock = threading.Lock()
_cache = {}     # empresa_id -> valor


def _empresa_id() -> str:
    """Igual que en `datos_nube` y `filtros_manager`: el entorno manda.

    El barrido recorre las empresas fijando EMPRESA_ID en cada vuelta y no tiene
    archivo de sesión."""
    env_id = os.environ.get("EMPRESA_ID", "").strip()
    if env_id:
        return env_id
    from helpers import leer_sesion
    return leer_sesion().get("empresa_id") or ""


def llamado_seguimiento(empresa_id: str = None, refrescar: bool = False) -> str:
    """'primer' | 'segundo' | 'todas'. Cae a 'todas' ante cualquier problema.

    Que el respaldo sea 'todas' es deliberado: si no se puede leer la
    preferencia, es mejor seguir de más que dejar de vigilar cotizaciones que
    la empresa sí quería.
    """
    empresa_id = empresa_id or _empresa_id()
    if not empresa_id:
        return POR_DEFECTO

    if not refrescar:
        with _lock:
            if empresa_id in _cache:
                return _cache[empresa_id]

    valor = POR_DEFECTO
    if supabase is not None:
        try:
            resp = ejecutar_con_refresco(
                lambda: supabase.table(TABLA).select("llamado_seguimiento")
                .eq("empresa_id", empresa_id).maybe_single().execute())
            dato = (resp.data or {}).get("llamado_seguimiento")
            if dato in LLAMADOS:
                valor = dato
        except Exception:
            pass    # sin preferencia guardada, o sin conexión: 'todas'

    with _lock:
        _cache[empresa_id] = valor
    return valor


def guardar_llamado_seguimiento(valor: str):
    """Cambia la preferencia de la empresa. -> (ok, mensaje_error)

    Sólo la puede cambiar un administrador; quien lo comprueba es la función de
    base de datos, no esta capa: la interfaz esconde el control, pero eso no es
    una defensa.
    """
    if valor not in LLAMADOS:
        return False, f"Valor no válido: {valor}"
    if supabase is None:
        return False, "Sin conexión a la nube."

    try:
        ejecutar_con_refresco(
            lambda: supabase.rpc("guardar_preferencia_llamado", {"p_valor": valor}).execute())
    except Exception as e:
        detalle = str(getattr(e, "message", "") or e)
        if "42501" in detalle or "administrador" in detalle.lower():
            return False, "Sólo un administrador puede cambiar esta preferencia."
        if "PGRST202" in detalle or "42883" in detalle:
            return False, ("Falta preparar la base para esta opción: ejecuta "
                           "`sql_compra_agil_seguimiento.sql` en Supabase.")
        return False, f"No se pudo guardar: {detalle}"

    with _lock:
        _cache[_empresa_id()] = valor
    return True, ""


def aplica_llamado(llamado: str, preferencia: str) -> bool:
    """¿Esta cotización entra en el seguimiento, según la preferencia?

    `llamado` es lo que dice el portal: '1er llamado', '2do llamado' o vacío.

    Las de llamado desconocido SIEMPRE entran, incluso con una preferencia
    estricta. El portal deja el campo vacío mientras no ha decidido, y
    descartarlas ahí sería perder justo las que todavía pueden acabar en el
    llamado que a la empresa le interesa.
    """
    if preferencia == "todas" or not preferencia:
        return True
    texto = str(llamado or "").strip().lower()
    if not texto:
        return True
    if preferencia == "primer":
        return texto.startswith("1")
    if preferencia == "segundo":
        return texto.startswith("2")
    return True
