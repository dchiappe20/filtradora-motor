# -*- coding: utf-8 -*-
"""
seguimiento_compra_agil.py — Filtrado en la nube y seguimiento de estado.

Dos trabajos, los dos POR EMPRESA, que corren dentro del barrido:

  · `filtrar_para_empresa`  — aplica las reglas de la empresa a la copia
    compartida de Compra Ágil y deja el resultado en `compra_agil_seguimiento`.
    Antes esto lo hacía cada app, con los mismos datos, en cada visita.

  · `revisar_estado`  — vuelve a preguntarle al portal por cada cotización que
    la empresa está siguiendo y actualiza su llamado y sus cierres. Es lo que
    da el «seguimiento»: hasta ahora, entre barridos completos, una cotización
    con el primer cierre vencido sólo se podía mostrar como «Revisar en web».

Lo que hace esto viable es el tamaño: el conjunto filtrado son cientos de
cotizaciones, no las ~38.000 de la ventana. Pedirle al portal una ficha por cada
una cabe de sobra en el presupuesto de un barrido diurno; hacerlo con la ventana
entera es justo lo que sólo puede permitirse el barrido de la madrugada.

El filtrado NO se reimplementa: se llama a `processor.filtrar_licitaciones`, el
mismo motor que corría en la app. Así lo que ve el usuario no cambia.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd

import compra_agil_api
import datos_nube
import filtros_manager
import preferencias_empresa
import processor

# Hilos con los que se piden las fichas del seguimiento. Bastante por debajo de
# los de la descarga (MAX_WORKERS = 6): aquí no hay prisa —son cientos de
# fichas, no miles— y el barrido diurno comparte el portal con gente trabajando.
HILOS_REVISION = 3

# Cuántas cotizaciones se revisan como mucho en una pasada. Si una empresa tiene
# filtros muy amplios, esto evita que el seguimiento se coma el barrido entero;
# lo que quede se revisa en la corrida siguiente, empezando por lo más antiguo.
MAX_REVISIONES = 600

_COL_ID = "Numero Adquisición"
_COL_LLAMADO = "Llamado"
_COL_CIERRE1 = "Fecha Cierre 1er Llamado"
_COL_CIERRE2 = "Fecha Cierre 2do Llamado"


def _ahora_iso():
    return datetime.now(timezone.utc).isoformat()


def _fijar_empresa(empresa_id, nombre=""):
    """Todo el stack (datos_nube, filtros_manager, preferencias_empresa) saca la
    empresa del entorno cuando no hay sesión. El barrido va empresa por empresa,
    así que se cambia aquí en cada vuelta."""
    os.environ["EMPRESA_ID"] = str(empresa_id)
    os.environ["EMPRESA_NOMBRE"] = nombre or ""


# ---------------------------------------------------------------------------
# 1. Filtrar
# ---------------------------------------------------------------------------

def filtrar_para_empresa(empresa_id, nombre="", df_crudo=None, log=print):
    """Aplica los filtros de la empresa y guarda el conjunto. -> dict con el resumen.

    `df_crudo` se pasa ya leído cuando se recorren varias empresas: la copia de
    Compra Ágil es la misma para todas y bajarla una vez por empresa sería pagar
    el egress N veces.

    Devuelve {"cotizaciones": n, "filas": n, "omitida": bool, "motivo": str}.
    """
    _fijar_empresa(empresa_id, nombre)

    reglas = filtros_manager.reglas_planas_de_empresa(empresa_id)
    if not reglas:
        # Sin reglas NO se toca nada. Filtrar con una lista vacía dejaría el
        # conjunto en cero y la empresa perdería lo que ya venía siguiendo, que
        # desde fuera es indistinguible de «hoy no hubo nada».
        log(f"  {nombre}: sin filtros cargados en la nube; se deja como estaba.")
        return {"cotizaciones": 0, "filas": 0, "omitida": True,
                "motivo": "sin_filtros"}

    if df_crudo is None:
        df_crudo = datos_nube.leer_tabla(compra_agil_api.TABLA_NUBE)
    if df_crudo is None or df_crudo.empty:
        log(f"  {nombre}: no hay datos de Compra Ágil en la nube todavía.")
        return {"cotizaciones": 0, "filas": 0, "omitida": True, "motivo": "sin_datos"}

    df_filtrado = processor.filtrar_licitaciones(df_crudo, reglas)
    if df_filtrado is None or df_filtrado.empty:
        log(f"  {nombre}: ninguna cotización coincide con sus {len(reglas)} reglas.")
        datos_nube.guardar_seguimiento(pd.DataFrame())
        return {"cotizaciones": 0, "filas": 0, "omitida": False, "motivo": ""}

    # La preferencia de la empresa recorta lo que se seguirá. Se lee de la nube
    # y se fuerza el refresco: en un barrido largo el cache podría ser de hace
    # horas y la preferencia haber cambiado.
    preferencia = preferencias_empresa.llamado_seguimiento(empresa_id, refrescar=True)
    if preferencia != preferencias_empresa.POR_DEFECTO:
        antes = df_filtrado[_COL_ID].nunique()
        mascara = df_filtrado[_COL_LLAMADO].apply(
            lambda v: preferencias_empresa.aplica_llamado(v, preferencia))
        df_filtrado = df_filtrado[mascara]
        despues = df_filtrado[_COL_ID].nunique() if not df_filtrado.empty else 0
        log(f"  {nombre}: preferencia '{preferencia}' deja {despues} de {antes}.")

    # `primera_deteccion` se conserva: es lo único que distingue una cotización
    # recién aparecida de una que la empresa lleva días mirando.
    previas = datos_nube.primeras_detecciones()
    ahora = _ahora_iso()
    df_filtrado = df_filtrado.copy()
    df_filtrado["Primera Detección"] = df_filtrado[_COL_ID].astype(str).map(
        lambda c: previas.get(c, ahora))
    df_filtrado["Estado Seguimiento"] = "vigente"
    df_filtrado["Última Revisión"] = ""

    filas = datos_nube.guardar_seguimiento(df_filtrado)
    cotizaciones = df_filtrado[_COL_ID].nunique()
    nuevas = sum(1 for c in df_filtrado[_COL_ID].astype(str).unique() if c not in previas)
    log(f"  {nombre}: {cotizaciones} cotizaciones ({filas} filas), {nuevas} nuevas.")
    return {"cotizaciones": int(cotizaciones), "filas": filas,
            "nuevas": nuevas, "omitida": False, "motivo": ""}


# ---------------------------------------------------------------------------
# 2. Seguir el estado
# ---------------------------------------------------------------------------

def _vencida(fecha_texto, ahora):
    """True si esa fecha ya pasó. Vacía o ilegible cuenta como no vencida."""
    if not str(fecha_texto).strip():
        return False
    fecha = pd.to_datetime(fecha_texto, errors="coerce")
    return bool(pd.notna(fecha) and fecha < ahora)


def revisar_estado(empresa_id, nombre="", log=print):
    """Refresca el estado de lo que la empresa sigue. -> dict con el resumen.

    Dos pasos, y el orden importa porque el barato descarta trabajo del caro:

      1. Lo que ya tiene el 2do cierre vencido se cierra SIN tocar el portal:
         se sabe con la fecha guardada. Misma idea que
         `compra_agil_api._codigos_segundo_cierre_vencido`.
      2. Del resto se pide la ficha y se actualizan llamado y cierres.
    """
    _fijar_empresa(empresa_id, nombre)

    try:
        vigentes = datos_nube.seguimiento_vigentes()
    except datos_nube.ErrorNube as e:
        log(f"  {nombre}: no se pudo leer el seguimiento ({e}).")
        return {"revisadas": 0, "cerradas": 0, "cambios": 0, "error": str(e)}

    if not vigentes:
        log(f"  {nombre}: no hay cotizaciones en seguimiento.")
        return {"revisadas": 0, "cerradas": 0, "cambios": 0}

    ahora = pd.Timestamp.now()

    # --- 1. Las que se cierran sin preguntar ---------------------------------
    ya_cerradas = [v["codigo"] for v in vigentes if _vencida(v["cierre2"], ahora)]
    if ya_cerradas:
        datos_nube.cerrar_seguimiento(ya_cerradas)

    por_revisar = [v for v in vigentes if v["codigo"] not in set(ya_cerradas)]
    if len(por_revisar) > MAX_REVISIONES:
        log(f"  {nombre}: {len(por_revisar)} en seguimiento, se revisan "
            f"{MAX_REVISIONES} en esta pasada; el resto, en la siguiente.")
        por_revisar = por_revisar[:MAX_REVISIONES]

    # --- 2. Las que hay que confirmar en el portal ---------------------------
    cambios = 0
    fallidas = 0

    def _revisar(entrada):
        ficha = compra_agil_api._obtener_ficha(entrada["codigo"])
        if not ficha:
            return entrada["codigo"], None
        filas = compra_agil_api._procesar_ficha(ficha)
        return entrada["codigo"], (filas[0] if filas else None)

    with ThreadPoolExecutor(max_workers=HILOS_REVISION) as pool:
        futuros = {pool.submit(_revisar, v): v for v in por_revisar}
        for futuro in as_completed(futuros):
            entrada = futuros[futuro]
            try:
                _codigo, fila = futuro.result()
            except Exception:
                fallidas += 1
                continue

            if fila is None:
                # El portal no la sirvió. No se toca nada: puede ser un fallo
                # pasajero, y darla por cerrada la escondería de la pantalla.
                fallidas += 1
                continue

            nuevos = {
                "llamado": fila.get(_COL_LLAMADO, "") or "",
                "fecha_cierre_1er_llamado": fila.get(_COL_CIERRE1, "") or "",
                "fecha_cierre_2do_llamado": fila.get(_COL_CIERRE2, "") or "",
                "ultima_revision": _ahora_iso(),
            }
            # Si además ya venció el 2do cierre que acaba de informar, se cierra.
            if _vencida(nuevos["fecha_cierre_2do_llamado"], ahora):
                nuevos["estado_seguimiento"] = "cerrada"

            cambio = (nuevos["llamado"] != entrada["llamado"]
                      or nuevos["fecha_cierre_2do_llamado"] != entrada["cierre2"])
            try:
                datos_nube.actualizar_seguimiento(entrada["codigo"], nuevos)
            except datos_nube.ErrorNube:
                fallidas += 1
                continue
            if cambio:
                cambios += 1

    log(f"  {nombre}: revisadas {len(por_revisar)}, {cambios} con cambio de estado, "
        f"{len(ya_cerradas)} cerradas por fecha, {fallidas} que el portal no sirvió.")
    return {"revisadas": len(por_revisar), "cerradas": len(ya_cerradas),
            "cambios": cambios, "fallidas": fallidas}
