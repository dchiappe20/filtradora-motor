# -*- coding: utf-8 -*-
"""
barrido_compra_agil.py — Barrido automático de Compra Ágil (headless, GitHub Actions).

Es el ÚNICO camino por el que entran datos de Compra Ágil: la app ya no descarga
nada, sólo lee de la nube. Corre tres veces al día, todas programadas:

  · 01:00 (madrugada)  → modo `completo`: recorre los últimos DIAS_VENTANA días.
                         Es el único que vuelve sobre los días viejos, así que es
                         el que detecta los pasos a 2do llamado y los cierres.
  · 10:00 y 16:00      → modo `dia`: lista SÓLO lo publicado hoy. Además suelta
                         de la tabla lo que ya tiene el 2do cierre vencido, que
                         se sabe por la fecha guardada sin preguntarle al portal.

Los datos de Compra Ágil son públicos e idénticos para todas las empresas, así
que se guarda UNA sola copia compartida (`empresa_id` NULL) que todas leen. Lo
que sí es por empresa es el ESTADO de la descarga (`descarga_estado`), que cada
app sondea para mostrar el avance y refrescarse sola al terminar.

Uso:  python barrido_compra_agil.py [completo|dia]     (por defecto: completo)

Códigos de salida: 0 si el barrido terminó (aunque sea parcial por tiempo o
porque el portal dejó caer páginas, mientras algo se haya bajado); 1 si falló o
si el listado no respondió y la corrida acabó sin traer nada.
"""
import os
import sys
import time
from datetime import datetime

import auth
import compra_agil_api
import datos_nube
import seguimiento_compra_agil

_MODULO = "compra_agil"

# Cuánto puede durar cada modo antes de cortarse solo y guardar lo que lleve.
# GitHub Actions mata cualquier trabajo a las 6 horas (360 min) por mucho que se
# suba `timeout-minutes`, así que el corte va con margen para que el propio
# script decida cuándo parar, en vez de que lo maten a mitad de un lote.
LIMITE_MINUTOS = {"completo": 300, "dia": 100}

# Minutos reservados para el SEGUIMIENTO, después de la descarga. Van aparte a
# propósito: el 2026-08-19 la descarga del día se llevó sus 100 minutos y el job
# murió a los 120 en mitad del seguimiento, sin terminar ninguna empresa. El
# reparto tiene que estar escrito, no ser lo que sobre.
#
# El `timeout-minutes` del workflow tiene que ser MAYOR que la suma de los dos
# más el arranque (checkout + pip ≈ 3 min), o GitHub corta igual.
MINUTOS_SEGUIMIENTO = {"completo": 0, "dia": 55}

_ultimo_estado = {"t": 0.0}  # throttle de escrituras de estado a la nube


def _log(mensaje, progreso=None):
    """Callback: log a stdout + estado en `descarga_estado` (throttled) de la
    empresa en curso (según EMPRESA_ID), para que su app lo vea si está mirando."""
    marca = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if progreso is not None:
        print(f"[{marca}] {mensaje} ({progreso:.0f}%)", flush=True)
    else:
        print(f"[{marca}] {mensaje}", flush=True)
    ahora = time.time()
    if ahora - _ultimo_estado["t"] >= 4:
        _ultimo_estado["t"] = ahora
        try:
            datos_nube.escribir_estado_descarga(_MODULO, "corriendo", mensaje)
        except Exception:
            pass


def _empresas_objetivo():
    """[(empresa_id, nombre)] de las empresas ACTIVA con la app 'filt' ACTIVA.
    Vía función SECURITY DEFINER en core (la service_role no tiene grants directos)."""
    resp = auth.supabase.schema("core").rpc(
        "empresas_de_app", {"p_codigo_app": auth.CODIGO_APP}).execute()
    datos = resp.data or []
    return [(str(e["id"]), e.get("nombre", "")) for e in datos if e.get("id")]


def _fijar_empresa(empresa_id, nombre):
    os.environ["EMPRESA_ID"] = empresa_id
    os.environ["EMPRESA_NOMBRE"] = nombre or ""


def _avisar_a_todas(empresas, estado, detalle):
    """Deja el mismo estado de descarga en todas las empresas. Es lo único que
    sigue siendo por empresa: los datos ya son una copia compartida."""
    for empresa_id, nombre in empresas:
        _fijar_empresa(empresa_id, nombre)
        try:
            datos_nube.escribir_estado_descarga(_MODULO, estado, detalle)
        except Exception as e:
            print(f"  (no se pudo avisar a {nombre}: {e})", flush=True)


def _filtrar_y_seguir(empresas, modo):
    """Filtra la copia compartida para cada empresa y, en los barridos del día,
    revisa además el estado de lo que ya venía siguiendo. -> texto de resumen.

    Va después de la descarga y nunca tumba el barrido: los datos crudos ya
    están guardados, así que un fallo aquí es "las apps ven lo de antes", no
    "se perdió la corrida". Cada empresa se aísla de las demás por lo mismo.

    La copia cruda se lee UNA vez y se pasa a todas: es la misma para todo el
    mundo y son ~15 MB, así que leerla por empresa multiplicaría el egress sin
    traer un solo dato nuevo.
    """
    from helpers import id_corto

    try:
        df_crudo = datos_nube.leer_tabla(compra_agil_api.TABLA_NUBE)
    except Exception as e:
        print(f"  ERROR al leer la copia compartida para filtrar: {e}", flush=True)
        return ""

    if df_crudo is None or df_crudo.empty:
        print("  No hay datos de Compra Ágil que filtrar todavía.", flush=True)
        return ""

    # El motor de filtrado espera texto en todas las columnas, como hacía la app
    # antes de llamarlo. Sin esto, un NaN acabaría comparándose como "nan".
    df_crudo = df_crudo.fillna("")
    for col in df_crudo.columns:
        df_crudo[col] = df_crudo[col].astype(str)

    escribir = lambda m: print(m, flush=True)
    total_cotizaciones = 0
    total_cambios = 0
    total_pendientes = 0
    con_error = 0

    # --- FASE 1: filtrar TODAS -----------------------------------------------
    # Va primero y sin reloj porque es lo esencial y lo barato: sin filtrar, la
    # app no ve las cotizaciones nuevas. Son ~1-2 minutos por empresa.
    print(f"Filtrando para {len(empresas)} empresa(s)...", flush=True)
    for empresa_id, nombre in empresas:
        try:
            res = seguimiento_compra_agil.filtrar_para_empresa(
                empresa_id, nombre, df_crudo=df_crudo, log=escribir)
            total_cotizaciones += res.get("cotizaciones", 0)
        except Exception as e:
            con_error += 1
            print(f"  ERROR al filtrar para {nombre} ({id_corto(empresa_id)}): {e}",
                  flush=True)
            # Una empresa rota no puede dejar sin barrido a las demás.

    # --- FASE 2: seguir el estado, con lo que quede de tiempo -----------------
    # Sólo en los barridos del día: el completo de la madrugada acaba de releer
    # la ventana entera, así que el estado de la tabla es de hace un momento.
    if modo != "dia":
        return f"Seguimiento: {total_cotizaciones} cotizaciones filtradas."

    presupuesto = MINUTOS_SEGUIMIENTO.get(modo, 0) * 60
    if presupuesto <= 0:
        return f"Seguimiento: {total_cotizaciones} cotizaciones filtradas."

    # El tiempo se reparte por igual. Con el orden por `ultima_revision` que usa
    # `seguimiento_vigentes`, cada corrida ataca lo más rezagado de cada empresa,
    # así que lo que hoy no cabe entra mañana: nada se queda sin revisar nunca.
    fin = time.monotonic() + presupuesto
    print(f"Revisando el estado de lo seguido ({MINUTOS_SEGUIMIENTO[modo]} min "
          f"para {len(empresas)} empresa(s))...", flush=True)

    for i, (empresa_id, nombre) in enumerate(empresas):
        restantes = len(empresas) - i
        corte = min(fin, time.monotonic() + (fin - time.monotonic()) / restantes)
        if time.monotonic() >= fin:
            print(f"  Sin tiempo para {nombre} ({id_corto(empresa_id)}); "
                  "le toca en la próxima corrida.", flush=True)
            continue
        try:
            res_seg = seguimiento_compra_agil.revisar_estado(
                empresa_id, nombre, corte=corte, log=escribir)
            total_cambios += res_seg.get("cambios", 0)
            total_pendientes += res_seg.get("pendientes", 0)
        except Exception as e:
            con_error += 1
            print(f"  ERROR al revisar el estado de {nombre} "
                  f"({id_corto(empresa_id)}): {e}", flush=True)

    partes = [f"{total_cotizaciones} cotizaciones filtradas",
              f"{total_cambios} con cambio de estado"]
    if total_pendientes:
        partes.append(f"{total_pendientes} sin revisar (siguen en la próxima)")
    if con_error:
        partes.append(f"{con_error} empresa(s) con error (ver el log)")
    return "Seguimiento: " + ", ".join(partes) + "."


def main():
    modo = (sys.argv[1] if len(sys.argv) > 1 else "completo").strip().lower()
    if modo not in LIMITE_MINUTOS:
        print(f"ERROR: modo '{modo}' desconocido (usa 'completo' o 'dia').", flush=True)
        return 1

    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    try:
        empresas = _empresas_objetivo()
    except Exception as e:
        print(f"ERROR al listar empresas desde core: {e}", flush=True)
        return 1

    if not empresas:
        print(f"No hay empresas activas con la app '{auth.CODIGO_APP}'. Nada que barrer.", flush=True)
        return 0

    if modo == "completo":
        print(f"Barrido COMPLETO de Compra Ágil (últimos {compra_agil_api.DIAS_VENTANA} "
              f"días) — copia compartida para {len(empresas)} empresa(s).", flush=True)
    else:
        print(f"Barrido DEL DÍA de Compra Ágil (sólo lo publicado hoy) — "
              f"copia compartida para {len(empresas)} empresa(s).", flush=True)

    _ultimo_estado["t"] = 0.0
    try:
        resultado = compra_agil_api.gestionar_descarga_ultimas(
            callback_estado=_log, limite_minutos=LIMITE_MINUTOS[modo],
            incremental=(modo == "dia"))
    except Exception as e:
        print(f"  ERROR en el barrido: {e}", flush=True)
        _avisar_a_todas(empresas, "error", str(e))
        return 1

    etiqueta = "Barrido nocturno" if modo == "completo" else "Barrido del día"
    descargadas = resultado.get("descargadas", 0)
    dias_fallidos = resultado.get("dias_fallidos") or []
    paginas_perdidas = resultado.get("paginas_perdidas", 0)
    fichas_fallidas = resultado.get("fichas_fallidas", 0)

    # Resumen siempre, salga como salga: es lo único que queda en el log de
    # Actions cuando alguien va a mirar por qué falta algo.
    print(f"{etiqueta}: {descargadas} cotizaciones guardadas; "
          f"{paginas_perdidas} página(s) del listado perdida(s); "
          f"{fichas_fallidas} ficha(s) que el portal no sirvió.", flush=True)

    # Una corrida que no pudo listar Y no trajo nada no hizo su trabajo: no puede
    # quedar en verde. Pasaba justo eso el 2026-08-12 a las 16:00 —se cayó la
    # primera página, salió «Sin novedades» y el job terminó bien— y desde fuera
    # era indistinguible de un día tranquilo.
    if dias_fallidos and not descargadas:
        detalle = (f"{etiqueta} fallido: el portal no respondió al listar "
                   f"{len(dias_fallidos)} día(s) y no se bajó nada.")
        _avisar_a_todas(empresas, "error", detalle)
        print(f"ERROR: {detalle}", flush=True)
        return 1

    if not resultado.get("completo"):
        pendientes = resultado.get("pendientes", 0)
        # Sin pendientes contadas es que el tiempo se acabó durante el LISTADO,
        # así que ni se sabe cuántas cotizaciones quedaron sin mirar.
        detalle = (f"{etiqueta} parcial: quedan {pendientes} cotizaciones, "
                   "se completan en la próxima corrida." if pendientes else
                   f"{etiqueta} parcial: se acabó el tiempo antes de mirarlo todo, "
                   "sigue en la próxima corrida.")
    elif dias_fallidos:
        # Sí trajo datos, así que la app debe refrescarse ('listo' y no 'error'),
        # pero lo que hay está incompleto y conviene que se sepa.
        detalle = (f"{etiqueta} incompleto: el portal dejó caer {paginas_perdidas} "
                   f"página(s) del listado (~{paginas_perdidas * 50} cotizaciones "
                   "sin mirar). Se recuperan en el próximo barrido.")
    elif fichas_fallidas:
        detalle = (f"{etiqueta} completado: {descargadas} cotizaciones. El portal no "
                   f"sirvió la ficha de {fichas_fallidas}; se reintentan en el próximo.")
    else:
        detalle = f"{etiqueta} completado."

    # Con los datos ya en la nube, cada empresa se lleva su parte.
    resumen_seg = _filtrar_y_seguir(empresas, modo)
    if resumen_seg:
        detalle = f"{detalle} {resumen_seg}"

    # 'listo' y no 'error': lo descargado ya está en la nube y es utilizable.
    _avisar_a_todas(empresas, "listo", detalle)
    print(detalle, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
