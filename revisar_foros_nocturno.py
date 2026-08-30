# -*- coding: utf-8 -*-
"""
revisar_foros_nocturno.py — Revisión nocturna de foros inversos, POR EMPRESA (headless).

Corre 1×/noche en GitHub Actions con la service_role. Para cada empresa con la app
'filt' activa, recorre su lista de licitaciones ofertadas (`foro_ofertadas`) y:

  - EGRESA (elimina de la lista) la licitación si lleva > 1 año o si dejó de estar
    CERRADA (adjudicada/desierta/revocada...), y en ese caso BORRA además sus
    menciones de la lista principal: en una licitación ya resuelta no queda nada
    que contestar a tiempo.
  - REVISA el foro de aclaración de las que siguen CERRADA (en evaluación),
    detectando el NOMBRE de la empresa (del registro central) y acumulando los
    hallazgos en foro_inverso_actuales.

El INGRESO (llenar `foro_ofertadas`) queda pendiente: mientras la lista esté vacía,
este runner no hace nada. La empresa se fija por iteración con EMPRESA_ID/EMPRESA_NOMBRE.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import auth
import api_client
import config
import foro_inverso
from helpers import id_corto

_UN_ANIO = timedelta(days=365)


def _empresas_objetivo():
    resp = auth.supabase.schema("core").rpc(
        "empresas_de_app", {"p_codigo_app": auth.CODIGO_APP}).execute()
    return [(str(e["id"]), e.get("nombre", "")) for e in (resp.data or []) if e.get("id")]


def _ofertadas(empresa_id):
    resp = (auth.supabase.table("foro_ofertadas")
            .select("codigo, fecha_alta").eq("empresa_id", empresa_id).execute())
    return resp.data or []


def _eliminar(empresa_id, codigos):
    for i in range(0, len(codigos), 100):
        try:
            (auth.supabase.table("foro_ofertadas").delete()
             .eq("empresa_id", empresa_id).in_("codigo", codigos[i:i + 100]).execute())
        except Exception as e:
            print(f"  aviso: no se pudo eliminar un lote: {e}", flush=True)


def _vencida(fecha_alta_iso) -> bool:
    try:
        f = datetime.fromisoformat(str(fecha_alta_iso).replace("Z", "+00:00"))
        if f.tzinfo is None:
            f = f.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - f) > _UN_ANIO
    except Exception:
        return False  # ante la duda, no egresar por antigüedad


def _revisar_empresa(empresa_id, nombre):
    os.environ["EMPRESA_ID"] = empresa_id
    os.environ["EMPRESA_NOMBRE"] = nombre or ""

    filas = _ofertadas(empresa_id)
    if not filas:
        print(f"  {nombre}: sin ofertadas en la lista.", flush=True)
        return
    variantes = foro_inverso.variantes_de(nombre)
    if not variantes:
        print(f"  {nombre}: sin nombre para detectar; se omite.", flush=True)
        return

    sesion = foro_inverso.crear_sesion_publica()
    renovar_cada = max(50, config.FORO_INVERSO_RENOVAR_SESION_CADA)
    a_eliminar, a_olvidar, resultados, revisados = [], [], [], []
    n = 0

    for fila in filas:
        codigo = str(fila.get("codigo") or "").strip()
        if not codigo:
            continue

        # Egreso por antigüedad (> 1 año en la lista).
        if _vencida(fila.get("fecha_alta")):
            a_eliminar.append(codigo)
            continue

        est = api_client.estado_licitacion(codigo)
        if est in ("publicada", "desconocido"):
            continue  # aún no cierra / no se pudo saber: se conserva, sin revisar foro

        # SÓLO LAS CERRADAS (en evaluación) son relevantes. Una adjudicada,
        # desierta o revocada se egresa SIN revisar y además se lleva sus
        # menciones de la lista principal.
        #
        # Antes se le daba una última pasada «por si acaso», y eso hacía justo lo
        # contrario de lo que se quiere: volvía a registrar sus preguntas con la
        # fecha de hoy, así que una licitación ya adjudicada seguía apareciendo
        # entre lo pendiente otros 30 días, hasta que la poda por antigüedad se
        # la llevaba. Lo que se pregunta en el foro de una adjudicada ya no se
        # puede contestar a tiempo: no es trabajo pendiente, es ruido.
        if est == "resuelta":
            a_eliminar.append(codigo)
            a_olvidar.append(codigo)
            continue

        n += 1
        if n > 1 and (n - 1) % renovar_cada == 0:
            sesion = foro_inverso.crear_sesion_publica()
        resultados.append(foro_inverso.revisar_codigo(sesion, codigo, variantes))
        revisados.append(codigo)
        time.sleep(config.FORO_INVERSO_PAUSA_SEGUNDOS)

    if resultados:
        foro_inverso.registrar_revision(resultados)
    if revisados:
        try:
            ahora = datetime.now(timezone.utc).isoformat()
            for i in range(0, len(revisados), 100):
                (auth.supabase.table("foro_ofertadas").update({"ultima_revision": ahora})
                 .eq("empresa_id", empresa_id).in_("codigo", revisados[i:i + 100]).execute())
        except Exception:
            pass
    # Primero las menciones y después la lista: si se cae en medio, lo peor que
    # queda es una licitación egresada cuyas menciones se limpian mañana, y no
    # unas menciones huérfanas de algo que ya nadie vuelve a mirar.
    if a_olvidar:
        foro_inverso.olvidar_hallazgos(a_olvidar)
    if a_eliminar:
        _eliminar(empresa_id, a_eliminar)

    con_mencion = sum(1 for r in resultados if r.get("hallazgos"))
    print(f"  {nombre}: revisadas {len(revisados)}, con mención {con_mencion}, "
          f"egresadas {len(a_eliminar)} ({len(a_olvidar)} ya resueltas, sin revisar).",
          flush=True)


def main():
    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    try:
        empresas = _empresas_objetivo()
    except Exception as e:
        print(f"ERROR al listar empresas desde core: {e}", flush=True)
        return 1

    if not empresas:
        print(f"No hay empresas activas con la app '{auth.CODIGO_APP}'.", flush=True)
        return 0

    print(f"Revisión nocturna de foros inversos para {len(empresas)} empresa(s).", flush=True)
    hubo_error = False
    for empresa_id, nombre in empresas:
        # Id corto, no el UUID entero: este log es publico (ver helpers.id_corto).
        print(f"=== {nombre} ({id_corto(empresa_id)}) ===", flush=True)
        try:
            _revisar_empresa(empresa_id, nombre)
        except Exception as e:
            hubo_error = True
            print(f"  ERROR en {nombre}: {e}", flush=True)

    print("Revisión nocturna de foros terminada.", flush=True)
    return 1 if hubo_error else 0


if __name__ == "__main__":
    sys.exit(main())
