# -*- coding: utf-8 -*-
"""
revisar_foros_nocturno.py — Revisión nocturna de foros inversos, POR EMPRESA (headless).

Corre 1×/noche en GitHub Actions con la service_role, disparado a las 05:00 de
Chile por pg_cron (ver `supabase/cron_barridos.sql`; el `schedule` del workflow
es sólo respaldo). Para cada empresa con la app 'filt' activa recorre su lista
de licitaciones ofertadas (`foro_ofertadas`) y, de cada una:

  - PIDE LA FICHA PÚBLICA, que en una sola petición da el título, el estado y si
    tiene sección de aclaraciones. No usa la API oficial ni, por tanto, ticket.
  - EGRESA la licitación si lleva > 1 año en la lista o si su estado ya no puede
    volver a «Cerrada» (adjudicada, desierta, revocada, cancelada), y en ese caso
    BORRA además sus menciones: ahí ya no queda nada que contestar a tiempo.
  - REVISA el foro SÓLO si está Cerrada (en evaluación), que es el único estado
    con foro inverso vivo, buscando los nombres que la empresa eligió (hasta 3,
    tabla `foro_nombres`; si no eligió ninguno, el del registro central).
  - DEJA EN LA TABLA lo que vio —título, estado, foro abierto y en qué quedó la
    revisión—, que es lo que la pantalla muestra como lista principal.

La empresa se fija por iteración con EMPRESA_ID/EMPRESA_NOMBRE.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import auth
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
    # Este registro es PÚBLICO (el repo lo es): la empresa va por su id corto,
    # nunca por su nombre ni con datos que la identifiquen.
    et = f"empresa {id_corto(empresa_id)}"
    os.environ["EMPRESA_ID"] = empresa_id
    os.environ["EMPRESA_NOMBRE"] = nombre or ""

    filas = _ofertadas(empresa_id)
    if not filas:
        print(f"  {et}: sin ofertadas en la lista.", flush=True)
        return

    # Con qué nombres se busca a la empresa lo decide ella (hasta 3, en
    # `foro_nombres`): la razón social de Mercado Público no siempre es el
    # nombre con el que está dada de alta aquí. Sin eso, la revisión no
    # encontraba nada y parecía que no había menciones.
    nombres = foro_inverso.nombres_a_revisar()
    variantes = foro_inverso.variantes_de(nombres)
    if not variantes:
        print(f"  {et}: sin nombre para detectar; se omite.", flush=True)
        return

    sesion = foro_inverso.crear_sesion_publica()
    renovar_cada = max(50, config.FORO_INVERSO_RENOVAR_SESION_CADA)
    a_eliminar, a_olvidar, resultados, revisados, fichas = [], [], [], [], []
    sin_cerrar = 0
    n = 0

    for fila in filas:
        codigo = str(fila.get("codigo") or "").strip()
        if not codigo:
            continue

        # Egreso por antigüedad (> 1 año en la lista).
        if _vencida(fila.get("fecha_alta")):
            a_eliminar.append(codigo)
            continue

        n += 1
        if n > 1 and (n - 1) % renovar_cada == 0:
            sesion = foro_inverso.crear_sesion_publica()

        # UNA petición: la ficha pública trae título, estado y si hay foro. Antes
        # el estado se le preguntaba a la API oficial, que exige el ticket de
        # Mercado Público — algo que sólo tiene Amilab.
        try:
            ficha = foro_inverso.datos_ficha(sesion, codigo)
        except Exception as e:
            print(f"    aviso: no se pudo leer la ficha de {codigo}: {e}", flush=True)
            continue   # se conserva en la lista y se reintenta mañana
        fichas.append(ficha)
        estado = ficha.get("estado") or ""

        # De adjudicada, desierta, revocada o cancelada NO se vuelve a «Cerrada»:
        # se egresa sin revisar y se lleva sus menciones. Lo que se pregunte en
        # el foro de una adjudicada ya no se puede contestar a tiempo: no es
        # trabajo pendiente, es ruido.
        if foro_inverso.es_irreversible(estado):
            a_eliminar.append(codigo)
            a_olvidar.append(codigo)
            continue

        # SÓLO las Cerradas (en evaluación) tienen foro inverso vivo. Una que
        # sigue Publicada se conserva y se mirará cuando cierre.
        if not foro_inverso.esta_cerrada(estado):
            sin_cerrar += 1
            time.sleep(config.FORO_INVERSO_PAUSA_SEGUNDOS)
            continue

        resultados.append(foro_inverso.revisar_con_ficha(sesion, ficha, variantes))
        revisados.append(codigo)
        time.sleep(config.FORO_INVERSO_PAUSA_SEGUNDOS)

    if resultados:
        foro_inverso.registrar_revision(resultados)

    # Lo que la pantalla muestra de cada fila. Va con TODAS las que siguen en la
    # lista, no sólo las revisadas: de una que aún no cierra también interesa ver
    # su título y su estado.
    egresadas = set(a_eliminar)
    vivas = [f for f in fichas if str(f.get("codigo") or "") not in egresadas]
    if vivas:
        foro_inverso.actualizar_ofertadas(vivas, resultados, nombres)

    # Primero las menciones y después la lista: si se cae en medio, lo peor que
    # queda es una licitación egresada cuyas menciones se limpian mañana, y no
    # unas menciones huérfanas de algo que ya nadie vuelve a mirar.
    if a_olvidar:
        foro_inverso.olvidar_hallazgos(a_olvidar)
    if a_eliminar:
        _eliminar(empresa_id, a_eliminar)

    con_mencion = sum(1 for r in resultados if r.get("hallazgos"))
    print(f"  {et}: revisadas {len(revisados)}, con mención {con_mencion}, "
          f"sin cerrar aún {sin_cerrar}, egresadas {len(a_eliminar)} "
          f"({len(a_olvidar)} ya resueltas, sin revisar).", flush=True)


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
        print(f"=== empresa {id_corto(empresa_id)} ===", flush=True)
        try:
            _revisar_empresa(empresa_id, nombre)
        except Exception as e:
            hubo_error = True
            print(f"  ERROR en empresa {id_corto(empresa_id)}: {e}", flush=True)

    print("Revisión nocturna de foros terminada.", flush=True)
    return 1 if hubo_error else 0


if __name__ == "__main__":
    sys.exit(main())
