# -*- coding: utf-8 -*-
"""
barrido_clientes.py — Lo que publican los compradores que la gente sigue.

Corre pegado a los barridos de Compra Ágil (los cuatro del día) y deja en
`clientes_seguimiento` lo que cada comprador seguido tiene VIVO: licitaciones
publicadas y compras ágiles en 1er o 2do llamado, más las compras ágiles que el
portal da por cerradas pero cuyo segundo cierre todavía no llega.

Las dos mitades cuestan cosas muy distintas:

  · COMPRA ÁGIL escala con los clientes. El buscador acepta `org_code`, así que
    cada comprador son dos consultas y unas pocas fichas. Cien clientes son
    unos minutos; por eso el tope por persona es 100 (36_clientes_barrido.sql).

  · LICITACIONES cuesta lo mismo se sigan 3 clientes o 300, porque la API
    oficial no deja preguntar por comprador: hay que mirar las 4.579 activas
    para saber de quién es cada una. Por eso existe `licitaciones_indice`: la
    ficha de cada licitación se pide UNA vez en su vida y después sólo se miran
    las nuevas del día (~1.000, unos 12 minutos a 1,42 fichas/s).

La primera corrida no cabe entera (54 min sólo en identificar las activas), así
que se corta por tiempo y sigue en la siguiente: lo ya identificado queda en el
índice y no se vuelve a pedir.

Uso:  python barrido_clientes.py [minutos]      (por defecto: 40)
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

import auth
import clientes_api
import datos_nube
import empresa_config

# Cuánto puede durar la corrida antes de cortarse sola. Va después del barrido
# de Compra Ágil dentro del mismo job, así que este número más el de aquél tiene
# que caber en el `timeout-minutes` del workflow.
MINUTOS_POR_DEFECTO = 40


def _log(mensaje):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {mensaje}", flush=True)


def _compradores_seguidos():
    """[(rut, seguidores)] de todo el mundo, sin repetir. Vía core."""
    resp = auth.supabase.schema("core").rpc("compradores_seguidos").execute()
    return [(str(f["rut"]), f.get("seguidores", 1)) for f in (resp.data or []) if f.get("rut")]


def _ticket():
    """Un ticket de Mercado Público con el que mirar las licitaciones.

    Los tickets son POR EMPRESA porque la API limita por ticket, pero lo que se
    baja aquí es público e igual para todas, así que basta uno. Se prefiere el
    común (`MP_TICKET`) y, si no lo hay, el de la primera empresa que tenga el
    suyo puesto.
    """
    try:
        return empresa_config.ticket_mercado_publico(nombre_empresa="")
    except empresa_config.TicketNoConfigurado:
        pass
    try:
        resp = auth.supabase.schema("core").rpc(
            "empresas_de_app", {"p_codigo_app": auth.CODIGO_APP}).execute()
    except Exception:
        return ""
    for empresa in (resp.data or []):
        nombre = empresa.get("nombre", "")
        if empresa_config.hay_ticket(nombre):
            _log(f"  (usando el ticket de {nombre} para las licitaciones)")
            return empresa_config.ticket_mercado_publico(nombre)
    return ""


# ---------------------------------------------------------------------------
# Compra Ágil
# ---------------------------------------------------------------------------

def _compra_agil(ruts, corte):
    """Lo vivo en Compra Ágil. -> {rut: [filas]} con SÓLO los que se pudieron
    mirar enteros: guardar una lista corta borraría de la pantalla cosas que sí
    están, así que quien falla conserva lo de la corrida anterior."""
    por_rut = {}
    for rut, seguidores in ruts:
        if time.monotonic() > corte:
            _log("  se acabó el tiempo en Compra Ágil; el resto va en la próxima")
            break
        org_code = clientes_api.org_code_de(rut)
        if not org_code:
            _log(f"  {clientes_api.formatear_rut(rut)}: el portal no lo reconoce")
            por_rut[rut] = []
            continue
        filas, completo = clientes_api.compra_agil_de(org_code, log=_log)
        if not completo:
            # Guardar una lista corta borraría de la pantalla cosas que sí
            # están; mejor dejar lo de la corrida anterior y reintentar.
            _log(f"  {clientes_api.formatear_rut(rut)}: consulta incompleta, se deja como estaba")
            continue
        por_rut[rut] = filas
        _log(f"  {clientes_api.formatear_rut(rut)}: {len(filas)} fila(s) de compra ágil "
             f"({seguidores} seguidor(es))")
    return por_rut


# ---------------------------------------------------------------------------
# Licitaciones
# ---------------------------------------------------------------------------

def _licitaciones(ruts_seguidos, ticket, corte):
    """Las licitaciones publicadas de esos compradores. -> ({rut: [filas]}, completo)

    Tres pasos: mirar qué hay activo, identificar lo que no conocíamos y bajar
    con productos sólo lo que es de un cliente seguido.

    `completo` es False si se acabó el tiempo antes de identificarlas todas. Con
    False no se puede guardar: faltarían licitaciones que sí existen y se
    borrarían de la pantalla.
    """
    activas = clientes_api.licitaciones_activas(ticket)
    if not activas:
        _log("  el portal no devolvió licitaciones activas")
        return {}, False

    indice = datos_nube.leer_indice_licitaciones()
    faltan = [c for c in activas if c not in indice]
    _log(f"  {len(activas)} activas; {len(faltan)} sin identificar")

    nuevas_identidades = []
    detalles_utiles = {}

    def _mirar(codigo):
        return codigo, clientes_api.ficha_licitacion(codigo, ticket)

    # De a lotes para poder mirar el reloj: la primera corrida son 4.579 fichas
    # y no caben en una sola pasada.
    LOTE = 60
    cortado = False
    for i in range(0, len(faltan), LOTE):
        if time.monotonic() > corte:
            cortado = True
            break
        with ThreadPoolExecutor(max_workers=clientes_api.HILOS_LICITACIONES) as pool:
            for codigo, detalle in pool.map(_mirar, faltan[i:i + LOTE]):
                if not detalle:
                    continue
                identidad = clientes_api.identidad_licitacion(detalle)
                nuevas_identidades.append(identidad)
                indice[codigo] = identidad["rut_organismo"]
                if identidad["rut_organismo"] in ruts_seguidos:
                    detalles_utiles[codigo] = detalle
        if len(nuevas_identidades) % 600 < LOTE:
            datos_nube.guardar_indice_licitaciones(nuevas_identidades)
            nuevas_identidades = []

    if nuevas_identidades:
        datos_nube.guardar_indice_licitaciones(nuevas_identidades)
    if cortado:
        _log("  se acabó el tiempo identificando licitaciones; sigue en la próxima")

    # Las que YA estaban identificadas y son de un cliente seguido: hay que
    # pedirles la ficha igual, porque el índice no guarda los productos.
    pendientes = [c for c in activas
                  if c not in detalles_utiles and indice.get(c) in ruts_seguidos]
    if pendientes:
        _log(f"  {len(pendientes)} licitación(es) de tus clientes por detallar")
        with ThreadPoolExecutor(max_workers=clientes_api.HILOS_LICITACIONES) as pool:
            for codigo, detalle in pool.map(_mirar, pendientes):
                if detalle:
                    detalles_utiles[codigo] = detalle

    por_rut = {}
    for detalle in detalles_utiles.values():
        for fila in clientes_api.filas_licitacion(detalle):
            por_rut.setdefault(fila["RUT Organismo"], []).append(fila)
    return por_rut, not cortado


# ---------------------------------------------------------------------------

def main():
    minutos = MINUTOS_POR_DEFECTO
    if len(sys.argv) > 1:
        try:
            minutos = max(5, int(sys.argv[1]))
        except ValueError:
            pass

    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    try:
        ruts = _compradores_seguidos()
    except Exception as e:
        print(f"ERROR al leer los compradores seguidos: {e}", flush=True)
        return 1

    if not ruts:
        _log("Nadie sigue a ningún comprador todavía. Nada que barrer.")
        return 0

    _log(f"Barrido de clientes: {len(ruts)} comprador(es) seguido(s), "
         f"{minutos} min de presupuesto.")
    empieza = time.monotonic()
    corte_total = empieza + minutos * 60
    ruts_seguidos = {r for r, _s in ruts}

    # Compra Ágil primero, con la mitad del reloj: es lo barato, lo que más
    # cambia en el día y lo que escala con el número de clientes. Lo que no
    # gaste se lo queda la otra mitad, que en la primera corrida lo necesita.
    _log("Compra Ágil por comprador...")
    por_rut = _compra_agil(ruts, corte=empieza + minutos * 30)

    _log("Licitaciones...")
    ticket = _ticket()
    licitaciones, licitaciones_ok = {}, False
    if not ticket:
        _log("  sin ticket de Mercado Público: esta parte se salta")
    else:
        try:
            licitaciones, licitaciones_ok = _licitaciones(
                ruts_seguidos, ticket, corte_total)
        except Exception as e:
            _log(f"  ERROR en licitaciones: {e}")

    # Se guarda por mitades y por comprador. Cada mitad reemplaza SÓLO sus
    # propias filas, así que una que se quede a medias no se lleva por delante
    # lo que la otra sí trajo, y un cliente que no se pudo mirar conserva lo de
    # la corrida anterior en vez de quedarse en blanco.
    columnas = list(datos_nube.MAPEO_CLIENTES.keys())

    def _guardar(rut, tipo, filas):
        try:
            datos_nube.guardar_cliente_tipo(
                rut, tipo, pd.DataFrame(filas, columns=columnas))
            return True
        except Exception as e:
            _log(f"  ERROR al guardar {clientes_api.formatear_rut(rut)} ({tipo}): {e}")
            return False

    guardados = sum(_guardar(rut, clientes_api.TIPO_COMPRA_AGIL, filas)
                    for rut, filas in por_rut.items())

    if licitaciones_ok:
        # A TODOS los seguidos, no sólo a los que trajeron algo: un cliente que
        # ya no tiene licitaciones abiertas tiene que quedarse sin ellas.
        for rut in ruts_seguidos:
            _guardar(rut, clientes_api.TIPO_LICITACION, licitaciones.get(rut, []))
    elif licitaciones:
        _log("  la vuelta de licitaciones quedó incompleta: no se guarda para no "
             "borrar lo que sí estaba")

    try:
        datos_nube.podar_indice_licitaciones()
    except Exception:
        pass

    total = sum(len(f) for f in por_rut.values())
    if licitaciones_ok:
        total += sum(len(f) for f in licitaciones.values())
    _log(f"Listo: {guardados} comprador(es) con compra ágil al día, {total} fila(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
