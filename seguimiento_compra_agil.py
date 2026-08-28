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
    con el primer cierre vencido sólo se podía mostrar como «Por confirmar».

Lo que hace esto viable es el tamaño: el conjunto filtrado son cientos de
cotizaciones, no las ~38.000 de la ventana. Pedirle al portal una ficha por cada
una cabe de sobra en el presupuesto de un barrido diurno; hacerlo con la ventana
entera es justo lo que sólo puede permitirse el barrido de la madrugada.

El filtrado NO se reimplementa: se llama a `processor.filtrar_licitaciones`, el
mismo motor que corría en la app. Así lo que ve el usuario no cambia.
"""
import os
import time
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

# Tope de seguridad por cantidad. El corte de verdad lo pone el TIEMPO (ver
# `corte` en `revisar_estado`): 600 fichas a 3 hilos son ~25 minutos, y con
# varias empresas eso no cabe en ninguna corrida. Contar fichas era medir la
# cosa equivocada.
MAX_REVISIONES = 600

# Cada cuántas fichas se mira el reloj. Con lotes de 30 el corte llega con
# menos de un minuto de retraso y no se paga una comprobación por ficha.
LOTE_REVISION = 30

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

def _conservar_seguimiento(df, previas, ahora, preservar_estado):
    """Rellena las columnas de seguimiento respetando lo que ya se sabía.

    `primera_deteccion` se conserva SIEMPRE: es lo único que distingue una
    cotización recién aparecida de una que la empresa lleva días mirando.

    El estado (llamado, cierres, última revisión) se conserva cuando
    `preservar_estado` es True Y esa cotización ya fue consultada al portal
    —lo dice `ultima_revision`—. El motivo es que `df` viene de la copia cruda
    de `compra_agil`, y ésa NO siempre es la más fresca:

      · La descarga del DÍA sólo lista lo publicado hoy. Una cotización de hace
        días que pasó a 2do llamado esta mañana sigue ahí como «1er llamado».
        Escribir ese valor pisaría lo que el seguimiento acaba de confirmar
        preguntándole al portal, y en la pantalla el cambio de llamado no
        llegaría nunca: cada barrido lo revertía.

      · La descarga COMPLETA de la madrugada sí recorre los 30 días y trae el
        llamado al día para toda la ventana. Ahí la cruda manda, y el barrido
        llama con `preservar_estado=False` para que el seguimiento parta limpio.
    """
    codigos = df[_COL_ID].astype(str)

    df["Primera Detección"] = codigos.map(
        lambda c: (previas.get(c) or {}).get("primera_deteccion") or ahora)

    if not preservar_estado:
        df["Estado Seguimiento"] = "vigente"
        df["Última Revisión"] = ""
        return df

    def _previo(codigo, campo, por_defecto=""):
        anterior = previas.get(codigo) or {}
        # Sin `ultima_revision` nunca se le preguntó al portal por ella, así que
        # lo que traiga la copia cruda es lo mejor que hay.
        if not anterior.get("ultima_revision"):
            return None
        return anterior.get(campo) or por_defecto

    def _mezclar(columna, campo):
        return [
            (_previo(c, campo) if _previo(c, campo) is not None else actual)
            for c, actual in zip(codigos, df[columna])
        ]

    df[_COL_LLAMADO] = _mezclar(_COL_LLAMADO, "llamado")
    df[_COL_CIERRE1] = _mezclar(_COL_CIERRE1, "fecha_cierre_1er_llamado")
    df[_COL_CIERRE2] = _mezclar(_COL_CIERRE2, "fecha_cierre_2do_llamado")
    df["Última Revisión"] = codigos.map(
        lambda c: (previas.get(c) or {}).get("ultima_revision") or "")
    df["Estado Seguimiento"] = codigos.map(
        lambda c: (previas.get(c) or {}).get("estado_seguimiento") or "vigente")
    return df


def filtrar_para_empresa(empresa_id, nombre="", df_crudo=None,
                         preservar_estado=True, log=print):
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

    previas = datos_nube.estado_seguimiento_previo()
    ahora = _ahora_iso()
    df_filtrado = df_filtrado.copy()
    df_filtrado = _conservar_seguimiento(df_filtrado, previas, ahora, preservar_estado)

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


def revisar_estado(empresa_id, nombre="", corte=None, log=print, avisar=None):
    """Refresca el estado de lo que la empresa sigue. -> dict con el resumen.

    `avisar(revisadas, total)` se llama al terminar cada lote. Hace falta porque
    esta fase se lleva hasta 55 minutos y quien la mira desde la app no tiene por
    qué quedarse sin noticias: la app da por colgada una descarga cuya fila no se
    refresca en 180 segundos, así que sin esto la barra se apagaba a mitad del
    barrido aunque todo fuera bien.

    `corte` es una marca de `time.monotonic()` a partir de la cual se para y se
    guarda lo que lleve. Es lo que impide que el seguimiento se coma el barrido:
    sin él, 600 fichas a 3 hilos son ~25 minutos POR EMPRESA y el job de GitHub
    moría a los 120 sin llegar a terminar ninguna.

    Dos pasos, y el orden importa porque el barato descarta trabajo del caro:

      1. Lo que ya tiene el 2do cierre vencido se cierra SIN tocar el portal:
         se sabe con la fecha guardada. Misma idea que
         `compra_agil_api._codigos_segundo_cierre_vencido`.
      2. Del resto se pide la ficha y se actualizan llamado y cierres, de lo
         más rezagado a lo más reciente (lo ordena `seguimiento_vigentes`).
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

    # Las que el usuario ve como «Por confirmar» van PRIMERO.
    #
    # Son las que tienen el 1er cierre vencido y siguen figurando en 1er
    # llamado: exactamente las que el portal ya movió y nosotros todavía no
    # sabemos a dónde. Son las únicas cuyo estado va a cambiar de verdad, y las
    # únicas que en pantalla se ven "sin resolver".
    #
    # Importa cuando el tiempo no alcanza para todas: con varias empresas cada
    # una recibe una porción del presupuesto, y es mucho mejor gastarla en las
    # veinte que están en el aire que en las trescientas que siguen igual.
    # `vigentes` ya viene de lo más rezagado a lo más reciente, y `sorted` es
    # estable, así que ese orden se conserva dentro de cada grupo.
    def _por_confirmar(v):
        return (str(v["llamado"]).strip() == "1er llamado"
                and _vencida(v["cierre1"], ahora))

    por_revisar = sorted(por_revisar, key=lambda v: not _por_confirmar(v))
    urgentes = sum(1 for v in por_revisar if _por_confirmar(v))
    if urgentes:
        log(f"  {nombre}: {urgentes} por confirmar, van primero.")

    if len(por_revisar) > MAX_REVISIONES:
        por_revisar = por_revisar[:MAX_REVISIONES]

    # --- 2. Las que hay que confirmar en el portal ---------------------------
    cambios = 0
    fallidas = 0
    revisadas = 0
    sin_tiempo = False
    portal_caido = False

    def _revisar(entrada):
        ficha = compra_agil_api._obtener_ficha(entrada["codigo"])
        if not ficha:
            return entrada["codigo"], None
        filas = compra_agil_api._procesar_ficha(ficha)
        return entrada["codigo"], (filas[0] if filas else None)

    def _aplicar(entrada, fila):
        """Guarda lo que dijo el portal. -> True si algo cambió de estado."""
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
        datos_nube.actualizar_seguimiento(entrada["codigo"], nuevos)
        return cambio

    # Se va por lotes para poder mirar el reloj entre uno y otro. Con todo
    # lanzado de golpe no habría dónde cortar: `as_completed` sólo termina
    # cuando terminan las 600.
    for i in range(0, len(por_revisar), LOTE_REVISION):
        if corte is not None and time.monotonic() >= corte:
            sin_tiempo = True
            break

        if avisar:
            avisar(revisadas, len(por_revisar))
        lote = por_revisar[i:i + LOTE_REVISION]
        fallidas_lote = 0
        with ThreadPoolExecutor(max_workers=HILOS_REVISION) as pool:
            futuros = {pool.submit(_revisar, v): v for v in lote}
            for futuro in as_completed(futuros):
                entrada = futuros[futuro]
                revisadas += 1
                try:
                    _codigo, fila = futuro.result()
                except Exception:
                    fallidas += 1
                    continue

                if fila is None:
                    # El portal no la sirvió. No se toca nada: puede ser un fallo
                    # pasajero, y darla por cerrada la escondería de la pantalla.
                    fallidas += 1
                    fallidas_lote += 1
                    continue

                try:
                    if _aplicar(entrada, fila):
                        cambios += 1
                except datos_nube.ErrorNube:
                    fallidas += 1

        # Un lote entero fallido con el primero es el portal caido, no mala
        # suerte: insistir 55 minutos contra timeouts no recupera nada y deja al
        # resto de las empresas sin su turno. Se corta y se reintenta en la
        # proxima corrida.
        if i == 0 and fallidas_lote == len(lote):
            log(f"  {nombre}: el portal no sirvió ninguna de las primeras "
                f"{len(lote)} fichas; se deja para la próxima corrida.")
            portal_caido = True
            break

    pendientes = len(por_revisar) - revisadas
    cola = (f", {pendientes} para la próxima corrida" if sin_tiempo and pendientes > 0 else "")
    log(f"  {nombre}: revisadas {revisadas} de {len(por_revisar)}, {cambios} con cambio "
        f"de estado, {len(ya_cerradas)} cerradas por fecha, {fallidas} que el portal "
        f"no sirvió{cola}.")
    return {"revisadas": revisadas, "cerradas": len(ya_cerradas), "cambios": cambios,
            "fallidas": fallidas, "pendientes": max(0, pendientes),
            "sin_tiempo": sin_tiempo, "portal_caido": portal_caido}
