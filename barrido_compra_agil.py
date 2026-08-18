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

_MODULO = "compra_agil"

# Cuánto puede durar cada modo antes de cortarse solo y guardar lo que lleve.
# GitHub Actions mata cualquier trabajo a las 6 horas (360 min) por mucho que se
# suba `timeout-minutes`, así que el corte va con margen para que el propio
# script decida cuándo parar, en vez de que lo maten a mitad de un lote.
LIMITE_MINUTOS = {"completo": 300, "dia": 100}

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

    # 'listo' y no 'error': lo descargado ya está en la nube y es utilizable.
    _avisar_a_todas(empresas, "listo", detalle)
    print(detalle, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
