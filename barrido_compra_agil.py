# -*- coding: utf-8 -*-
"""
barrido_compra_agil.py — Barrido automático de Compra Ágil (headless, GitHub Actions).

Es el ÚNICO camino por el que entran datos de Compra Ágil: la app ya no descarga
nada, sólo lee de la nube. Corre en cuatro RANURAS al día, todas programadas:

  · 01:00 (madrugada)  → modo `completo`, ranura `noche`: recorre los últimos
                         DIAS_VENTANA días y filtra para TODAS las empresas. Es
                         el único que vuelve sobre los días viejos, así que es el
                         que detecta los pasos a 2do llamado y los cierres. NO
                         hace seguimiento (MINUTOS_SEGUIMIENTO['completo'] = 0):
                         acaba de releer los 30 días, así que el estado que deja
                         es de hace un momento.
  · 10:00 / 12:00 / 15:00 → modo `dia`, ranuras `manana` / `mediodia` / `tarde`:
                         listan SÓLO lo publicado hoy, filtran y ADEMÁS revisan
                         en el portal el estado de las cotizaciones ya seguidas.
                         Sueltan también lo que tiene el 2do cierre vencido, que
                         se sabe por la fecha guardada sin preguntar nada.

EL NOCTURNO ES DE TODOS. No depende del plan: sin él ninguna empresa tendría al
día su conjunto filtrado. Lo que `barridos_dia` reparte son los DIURNOS, que es
donde está la diferencia real entre un plan y otro: cuántas veces al día se
vuelve a mirar lo publicado y se confirma el estado de lo que se sigue.

Los datos de Compra Ágil son públicos e idénticos para todas las empresas, así
que se guarda UNA sola copia compartida (`empresa_id` NULL) que todas leen: esa
descarga se hace siempre y beneficia a todo el mundo, pague lo que pague. Lo que
sí es por empresa —y por tanto lo que el plan limita— es el FILTRADO, que deja
el resultado en `compra_agil_seguimiento`, y el SEGUIMIENTO de estado. Un plan
con `barridos_dia: 1` sigue leyendo la ventana completa de 30 días; lo que tiene
es que su conjunto filtrado se refresca una vez al día en vez de tres.

También es por empresa el ESTADO de la descarga (`descarga_estado`), que cada
app sondea para mostrar el avance y refrescarse sola al terminar. Se avisa sólo
a quien le toca esta ranura: anunciarle un barrido a quien no va a ver ningún
dato nuevo es peor que no decirle nada.

Uso:  python barrido_compra_agil.py [completo|dia] [ranura]

      La ranura por defecto es `noche` para `completo` y `todas` para `dia`.
      `todas` no mira el plan y filtra a todo el mundo: es lo que corresponde
      cuando alguien lanza el barrido a mano desde Actions, y además deja que
      un workflow todavía sin actualizar se comporte como antes.

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

# EL NOCTURNO NO SE REPARTE. Es de todos, esté en el plan que esté: recorre los
# 30 días de la ventana y es el único que vuelve sobre los días viejos, así que
# sin él ninguna empresa tendría al día su conjunto filtrado. `barridos_dia`
# cuenta los DIURNOS, que es lo que de verdad separa a un plan de otro.
RANURA_SIEMPRE = "noche"

# Los diurnos, y a quién alcanza cada combinación.
#
# El plan dice CUÁNTOS diurnos al día tiene una empresa; esta tabla dice CUÁLES,
# que es lo que el motor necesita saber.
#
#   3  las tres pasadas: mañana, mediodía y tarde
#   2  las dos de siempre, que es como funcionaba antes de los planes
#   1  sólo el mediodía: un único refresco al filo de la jornada recoge lo
#      publicado por la mañana y deja la tarde entera para reaccionar. A las
#      10:00 se habría perdido casi todo el día; a las 15:00 llegaría tarde
#      para preparar una oferta.
RANURAS_POR_PLAN = {
    1: ("mediodia",),
    2: ("manana", "tarde"),
    3: ("manana", "mediodia", "tarde"),
}

RANURAS_VALIDAS = ("noche", "manana", "mediodia", "tarde", "todas")

# La única ranura que puede saltarse entera cuando no le toca a nadie.
#
# Las otras no: aunque no hubiera a quién filtrar, su DESCARGA alimenta la copia
# compartida que leen todas las empresas. La del mediodía es la excepción porque
# las de las 10:00 y las 15:00 ya cubren el mismo día, así que mientras no haya
# ningún cliente que la tenga, esa corrida no aporta nada y sí gasta minutos de
# Actions.
RANURAS_OMITIBLES = ("mediodia",)

# Cuánto del total pesa cada fase, para que la barra de la app avance MONÓTONA.
# Una que vuelve a cero tres veces no informa: desconcierta.
#
# Los tramos salen de lo que dura cada cosa de verdad. En el barrido del día la
# descarga tiene 100 minutos de presupuesto y el seguimiento 55, y el filtrado
# son un par de minutos por empresa; en el nocturno no hay seguimiento, así que
# su tramo se lo reparten las otras dos.
PESOS = {
    "completo": {"descarga": (0, 85), "filtrado": (85, 100), "seguimiento": (100, 100)},
    "dia":      {"descarga": (0, 60), "filtrado": (60, 80),  "seguimiento": (80, 100)},
}

# A quién se le cuenta el avance, en qué fase va y qué es lo último que se le
# dijo al cliente. Lo fija `main()` antes de empezar; `_log` lo lee sin tener
# que recibirlo por parámetro, porque quien lo llama es `compra_agil_api`, que
# no sabe nada de empresas ni de fases.
#
# `publico` se guarda porque la mayoría de los mensajes del motor no son para el
# cliente: cuando llega uno de ésos hay que refrescar la fila igual (si no, a
# los 180 s la app da la descarga por colgada) pero SIN cambiar el texto.
_avance = {"modo": "dia", "fase": "descarga", "empresas": [],
           "publico": "Barrido en curso..."}

_ultimo_estado = {"t": 0.0}  # throttle de escrituras de estado a la nube


def _fijar_fase(fase, empresas=None):
    _avance["fase"] = fase
    if empresas is not None:
        _avance["empresas"] = [e for e, _n in empresas]
    _ultimo_estado["t"] = 0.0   # que el cambio de fase se publique ya


def _progreso_global(progreso_de_fase):
    """El 0..100 de una fase, llevado al 0..100 del trabajo entero."""
    if progreso_de_fase is None:
        return None
    desde, hasta = PESOS.get(_avance["modo"], PESOS["dia"])[_avance["fase"]]
    fraccion = max(0.0, min(100.0, float(progreso_de_fase))) / 100.0
    return int(round(desde + (hasta - desde) * fraccion))


def _publicar(estado, detalle, progreso=None, forzar=False):
    """Deja el avance en la nube para todas las empresas de esta ronda.

    Va a todas y no a una porque la descarga es una copia compartida: su avance
    le interesa a cualquiera que tenga la pantalla abierta. Con el throttle de
    4 segundos son unas 20 escrituras por hora de barrido, todas en una sola
    petición.
    """
    if estado == "corriendo" and detalle:
        _avance["publico"] = detalle
    ahora = time.time()
    if not forzar and ahora - _ultimo_estado["t"] < 4:
        return
    _ultimo_estado["t"] = ahora
    try:
        datos_nube.escribir_estado_varias(
            _MODULO, _avance["empresas"], estado, detalle,
            progreso=progreso, fase=_avance["fase"])
    except Exception:
        pass  # informar del avance no puede tumbar el barrido


def _log(mensaje, progreso=None, publico=None):
    """Callback del motor: el detalle va al log de la corrida y sólo lo que le
    importa al cliente sube a `descarga_estado`.

    Iban los dos por el mismo sitio, y en la barra de la app aparecían cosas
    como «Reintentando 14 ficha(s) que el portal no sirvió» o el día del listado
    que tocaba: contabilidad de la máquina para quien sólo quiere saber si sus
    cotizaciones están al día. Ahora el motor dice aparte qué se puede enseñar
    (ver `compra_agil_api._avisar`) y sin eso se mantiene el texto anterior.

    El porcentaje sí es de los dos: ya venía calculado desde `compra_agil_api` y
    hasta hace poco se imprimía y se tiraba.
    """
    marca = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if progreso is not None:
        print(f"[{marca}] {mensaje} ({progreso:.0f}%)", flush=True)
    else:
        print(f"[{marca}] {mensaje}", flush=True)
    _publicar("corriendo", publico or _avance["publico"], _progreso_global(progreso))


def _empresas_objetivo():
    """Empresas ACTIVA con la app 'filt' ACTIVA, tal como las da core.

    Cada una trae `id`, `nombre` y —desde 28_barridos_por_plan.sql— `plan` y
    `limites`. Vía función SECURITY DEFINER en core (la service_role no tiene
    grants directos).
    """
    resp = auth.supabase.schema("core").rpc(
        "empresas_de_app", {"p_codigo_app": auth.CODIGO_APP}).execute()
    return [e for e in (resp.data or []) if e.get("id")]


def _barridos_del_plan(empresa):
    """Cuántos barridos al día tiene contratados. None es «sin tope».

    Devuelve None también cuando el dato no viene: una base sin
    28_barridos_por_plan.sql no manda `limites`, y ante la duda se filtra de
    más. Quedarse corto sería dejar a un cliente sin datos por un despliegue a
    medias.
    """
    limites = empresa.get("limites")
    if not isinstance(limites, dict):
        return None
    valor = limites.get("barridos_dia")  # cuenta DIURNOS; el nocturno va aparte
    if valor is None:
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _le_toca(empresa, ranura):
    """¿Se filtra a esta empresa en esta ranura?"""
    if ranura in ("todas", RANURA_SIEMPRE):
        return True          # el nocturno no depende del plan

    cuantos = _barridos_del_plan(empresa)
    if cuantos is None:
        return True          # sin tope: todos los diurnos
    if cuantos <= 0:
        return False         # sin diurnos; hoy no hay ningún plan así
    if cuantos >= 3:
        return ranura in RANURAS_POR_PLAN[3]
    return ranura in RANURAS_POR_PLAN[cuantos]


def _para_filtrar(empresas, ranura):
    """[(empresa_id, nombre)] de las que toca filtrar en esta ranura."""
    return [(str(e["id"]), e.get("nombre", ""))
            for e in empresas if _le_toca(e, ranura)]


def _fijar_empresa(empresa_id, nombre):
    os.environ["EMPRESA_ID"] = empresa_id
    os.environ["EMPRESA_NOMBRE"] = nombre or ""


def _avisar_a_todas(empresas, estado, detalle):
    """Deja el mismo estado, y el mismo final, en todas las empresas.

    El `progreso=None` no sobra: al terminar hay que BORRAR el porcentaje de la
    corrida, o la app se quedaría con una barra al 73% hasta el barrido
    siguiente.
    """
    ids = [e for e, _n in empresas]
    escritas = datos_nube.escribir_estado_varias(
        _MODULO, ids, estado, detalle, progreso=None, fase=None)
    if escritas != len(ids):
        print(f"  (no se pudo avisar a todas: {escritas} de {len(ids)})", flush=True)


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

    # Sin nadie a quien filtrar no hay por qué bajarse los ~15 MB de la copia
    # compartida. Pasa cuando la ranura no le toca a ninguna empresa y aun asi
    # la descarga se hizo, que es lo correcto: de ella comen las demas rondas.
    if not empresas:
        print("  Ninguna empresa toca en esta ranura: no hay nada que filtrar.", flush=True)
        return ""

    # El anuncio va ANTES de leer la copia compartida: son ~15 MB y ese rato la
    # barra se quedaría con el último mensaje de la descarga.
    _fijar_fase("filtrado")
    _publicar("corriendo", "Filtrando cotizaciones...", _progreso_global(0), forzar=True)

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
    for indice, (empresa_id, nombre) in enumerate(empresas):
        # El avance de esta fase es cuántas empresas van, no cuánto lleva cada
        # una: `filtrar_para_empresa` es una sola pasada y no informa por dentro.
        #
        # El texto NO nombra a la empresa que toca. La fila de estado se escribe
        # igual para todas —la descarga es una copia compartida— así que poner
        # ahí el nombre le enseñaba a cada cliente los de los demás.
        _publicar("corriendo", "Filtrando cotizaciones...",
                  _progreso_global(100 * indice / max(len(empresas), 1)),
                  forzar=True)
        try:
            res = seguimiento_compra_agil.filtrar_para_empresa(
                empresa_id, nombre, df_crudo=df_crudo,
                # La descarga COMPLETA de la madrugada recorre los 30 días, así
                # que su copia cruda manda y el seguimiento parte limpio. La del
                # DÍA sólo lista lo de hoy: ahí la cruda está vieja para todo lo
                # demás y no puede pisar lo que el seguimiento confirmó.
                preservar_estado=(modo == "dia"),
                log=escribir)
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

    _fijar_fase("seguimiento")
    for i, (empresa_id, nombre) in enumerate(empresas):
        # Mismo motivo que en el filtrado: el nombre de la empresa que toca no
        # puede acabar en la pantalla de las otras.
        _publicar("corriendo", "Confirmando el estado de tus cotizaciones...",
                  _progreso_global(100 * i / max(len(empresas), 1)),
                  forzar=True)
        restantes = len(empresas) - i
        corte = min(fin, time.monotonic() + (fin - time.monotonic()) / restantes)
        if time.monotonic() >= fin:
            print(f"  Sin tiempo para {nombre} ({id_corto(empresa_id)}); "
                  "le toca en la próxima corrida.", flush=True)
            continue
        def _avisar_seguimiento(revisadas, total, i=i):
            """La fila de estado tiene que seguir latiendo durante toda la fase.

            El avance mezcla las dos cosas que avanzan: por qué empresa va y por
            dónde va dentro de ella. El texto no nombra a ninguna: la fila es la
            misma para todas y ahí no puede salir el nombre de otra empresa.
            """
            dentro = (revisadas / total) if total else 1
            _publicar("corriendo", "Confirmando el estado de tus cotizaciones...",
                      _progreso_global(100 * (i + dentro) / max(len(empresas), 1)))

        try:
            res_seg = seguimiento_compra_agil.revisar_estado(
                empresa_id, nombre, corte=corte, log=escribir,
                avisar=_avisar_seguimiento)
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

    # Sin ranura explícita, el comportamiento es el de antes de los planes: el
    # nocturno es la ranura `noche` y el del día alcanza a todo el mundo. Así un
    # workflow todavía sin actualizar —viven en otro repositorio y no se
    # despliegan a la vez— no deja a nadie sin filtrar.
    por_defecto = "noche" if modo == "completo" else "todas"
    ranura = (sys.argv[2] if len(sys.argv) > 2 else por_defecto).strip().lower()
    if ranura not in RANURAS_VALIDAS:
        print(f"ERROR: ranura '{ranura}' desconocida "
              f"(usa {', '.join(RANURAS_VALIDAS)}).", flush=True)
        return 1

    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    try:
        todas = _empresas_objetivo()
    except Exception as e:
        print(f"ERROR al listar empresas desde core: {e}", flush=True)
        return 1

    if not todas:
        print(f"No hay empresas activas con la app '{auth.CODIGO_APP}'. Nada que barrer.", flush=True)
        return 0

    empresas = _para_filtrar(todas, ranura)

    fuera = [e.get("nombre", "") for e in todas
             if not _le_toca(e, ranura)]
    if fuera:
        print(f"Ranura '{ranura}': fuera de esta ronda por su plan "
              f"({len(fuera)}): {', '.join(fuera)}.", flush=True)

    # Una ranura sin nadie a quien filtrar sólo se salta si otra del mismo día
    # ya alimenta la copia compartida; si no, la descarga se hace igual porque
    # de ella comen todas las empresas.
    if not empresas and ranura in RANURAS_OMITIBLES:
        print(f"Ranura '{ranura}': ninguna empresa la tiene contratada y las "
              "otras rondas ya cubren la descarga del día. No se corre nada.",
              flush=True)
        return 0

    if modo == "completo":
        print(f"Barrido COMPLETO de Compra Ágil (últimos {compra_agil_api.DIAS_VENTANA} "
              f"días) — copia compartida; se filtra para {len(empresas)} de "
              f"{len(todas)} empresa(s).", flush=True)
    else:
        print(f"Barrido DEL DÍA de Compra Ágil (sólo lo publicado hoy), ranura "
              f"'{ranura}' — copia compartida; se filtra para {len(empresas)} de "
              f"{len(todas)} empresa(s).", flush=True)

    # A partir de aquí `_log` sabe a quién contarle el avance y en qué fase va.
    _avance["modo"] = modo
    _fijar_fase("descarga", empresas)
    _publicar("corriendo", "Empezando el barrido...", 0, forzar=True)

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
        # Que se caiga el LISTADO no dice nada sobre lo demás: las fichas son
        # otro endpoint del portal, y el filtrado no lo toca en absoluto (lee de
        # la nube). Así que se intenta igual antes de rendirse.
        #
        # Importa de verdad: el 2026-08-20 a las 10:00 el listado no respondió y
        # la corrida se cortó aquí, de modo que ese día nadie revisó el estado de
        # las cotizaciones ya seguidas hasta las 15:00. El portal estaba caído
        # para listar, pero las fichas se servían igual.
        print("El listado no respondió, pero el filtrado y el seguimiento no "
              "dependen de él: se intentan igual.", flush=True)
        resumen_seg = _filtrar_y_seguir(empresas, modo)

        detalle = (f"{etiqueta} fallido: el portal no respondió al listar "
                   f"{len(dias_fallidos)} día(s) y no se bajó nada.")
        if resumen_seg:
            detalle = f"{detalle} {resumen_seg}"
        _avisar_a_todas(empresas, "error", detalle)
        print(f"ERROR: {detalle}", flush=True)
        # Sigue siendo un fallo: no traer lo nuevo del día es no hacer el
        # trabajo, y tiene que verse rojo en Actions.
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
