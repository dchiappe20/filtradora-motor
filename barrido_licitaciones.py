# -*- coding: utf-8 -*-
"""
barrido_licitaciones.py — Barrido nocturno de Licitaciones (headless, GitHub Actions).

El equivalente de `barrido_compra_agil.py` para las licitaciones de la API
oficial de Mercado Público. Corre una vez al día, de madrugada, y deja la tabla
`licitaciones` al día sin que nadie tenga que apretar nada.

QUÉ CAMBIA RESPECTO A LA DESCARGA POR RANGO

La descarga a mano seguía existiendo por una razón: era el ÚNICO camino por el
que entraban licitaciones. Eso tenía tres costes que este barrido quita:

  · Alguien tenía que acordarse, elegir un rango y esperar 40 minutos mirando
    una barra.
  · Cada descarga REEMPLAZABA la tabla, así que el rango que elegía una persona
    era el que veía toda su empresa.
  · Los datos eran de la última vez que alguien se acordó, no de anoche.

Ahora la tabla es una copia COMPARTIDA (empresa_id NULL) que llena este barrido
y que leen todas las empresas, igual que Compra Ágil. El botón de la app queda
para lo que el barrido no cubre: traer a mano un tramo antiguo (ver
`api_client.gestionar_descarga_rango`, que desde ahora sólo suma).

LA REGLA DE RETENCIÓN, QUE AQUÍ NO ES LA DE COMPRA ÁGIL

Una compra ágil cierra en ~1 día, así que soltar lo que sale de la ventana de 30
días no pierde nada. Una licitación puede estar abierta dos meses. Por eso aquí
NADA se borra por antigüedad: lo descargado se conserva mientras el portal lo
siga dando por 'Publicada', y el único motivo para soltarlo es que cambie de
estado. Los días de publicación de lo que sigue vivo fuera de la ventana se
vuelven a listar cada noche justo para poder vigilarlo (ver
`api_client._dias_del_barrido`).

EL TICKET

Uno solo para todo el barrido, el de Amilab, porque la API oficial limita POR
TICKET y ya no hay que repartir: la descarga es una sola y sirve a todas las
empresas. El workflow lo pasa como `MP_TICKET`, que es el valor común que usa
`empresa_config` cuando no hay una empresa en la sesión.

Uso:  python barrido_licitaciones.py

Códigos de salida: 0 si el barrido terminó (aunque sea parcial por tiempo,
mientras algo se haya podido mirar); 1 si falló o si el portal no respondió al
listar y la corrida acabó sin traer nada.
"""
import sys
import time
from datetime import datetime

import auth
import api_client
import datos_nube
import empresa_config

_MODULO = "licitaciones"

# Cuánto puede durar el barrido antes de cortarse solo y guardar lo que lleve.
# GitHub Actions mata cualquier trabajo a las 6 horas (360 min) por mucho que se
# suba `timeout-minutes`, así que el corte va con margen para que el propio
# script decida cuándo parar, en vez de que lo maten a mitad de un lote.
#
# De régimen sobra de largo: son las licitaciones publicadas hoy (~150-200
# fichas). Lo que no cabe en una corrida es la PRIMERA población —miles de
# fichas a 3 hilos—, y para eso está el guardado por lotes: se completa sola en
# dos o tres noches, porque lo ya subido deja de pedirse.
LIMITE_MINUTOS = 300

# A quién se le cuenta el avance y qué es lo último que se le dijo al cliente.
# La descarga es una copia compartida, así que su avance le interesa a todas las
# empresas que tengan la pantalla abierta.
_avance = {"empresas": [], "publico": "Barrido en curso..."}

_ultimo_estado = {"t": 0.0}   # throttle de escrituras de estado a la nube


def _publicar(estado, detalle, progreso=None, forzar=False):
    """Deja el avance en la nube para todas las empresas de esta ronda."""
    if estado == "corriendo" and detalle:
        _avance["publico"] = detalle
    ahora = time.time()
    if not forzar and ahora - _ultimo_estado["t"] < 4:
        return
    _ultimo_estado["t"] = ahora
    try:
        datos_nube.escribir_estado_varias(
            _MODULO, _avance["empresas"], estado, detalle,
            progreso=progreso, fase="descarga")
    except Exception:
        pass  # informar del avance no puede tumbar el barrido


def _log(mensaje, progreso=None, publico=None):
    """Callback del motor: el detalle va al log de la corrida y sólo lo que le
    importa al cliente sube a `descarga_estado`.

    Los dos canales van separados a propósito: «día 12-08-2026 (7/31)» es
    contabilidad de la máquina para quien sólo quiere saber si sus licitaciones
    están al día. Sin `publico` se mantiene el texto anterior, pero la fila se
    refresca igual (si no, a los 180 s la app da la descarga por colgada).
    """
    marca = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if progreso is not None:
        print(f"[{marca}] {mensaje} ({progreso:.0f}%)", flush=True)
    else:
        print(f"[{marca}] {mensaje}", flush=True)
    _publicar("corriendo", publico or _avance["publico"], progreso)


def _empresas_objetivo():
    """Empresas ACTIVA con la app 'filt' ACTIVA, tal como las da core.

    Vía función SECURITY DEFINER en core (la service_role no tiene grants
    directos). Aquí sólo se usan para saber a quién contarle el avance: la
    descarga es una y la misma para todas.
    """
    resp = auth.supabase.schema("core").rpc(
        "empresas_de_app", {"p_codigo_app": auth.CODIGO_APP}).execute()
    return [str(e["id"]) for e in (resp.data or []) if e.get("id")]


def _avisar_a_todas(estado, detalle):
    """Deja el mismo final en todas las empresas.

    El `progreso=None` no sobra: al terminar hay que BORRAR el porcentaje de la
    corrida, o la app se quedaría con una barra al 73% hasta el barrido
    siguiente.
    """
    ids = _avance["empresas"]
    if not ids:
        return
    escritas = datos_nube.escribir_estado_varias(
        _MODULO, ids, estado, detalle, progreso=None, fase=None)
    if escritas != len(ids):
        print(f"  (no se pudo avisar a todas: {escritas} de {len(ids)})", flush=True)


def main():
    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    # Se comprueba antes de empezar: sin ticket, el portal contesta a todo con un
    # 'Mensaje' y el barrido daría una vuelta entera para no traer nada.
    try:
        empresa_config.ticket_mercado_publico()
    except empresa_config.TicketNoConfigurado as e:
        print(f"ERROR: {e}", flush=True)
        return 1

    try:
        _avance["empresas"] = _empresas_objetivo()
    except Exception as e:
        print(f"ERROR al listar empresas desde core: {e}", flush=True)
        return 1

    if not _avance["empresas"]:
        print(f"No hay empresas activas con la app '{auth.CODIGO_APP}'. "
              "Se barre igual: la copia es compartida y estará lista para la "
              "primera que entre.", flush=True)

    print(f"Barrido NOCTURNO de Licitaciones (ventana de "
          f"{api_client.DIAS_VENTANA} días + los días de lo que sigue abierto) "
          f"— copia compartida para {len(_avance['empresas'])} empresa(s).", flush=True)

    _publicar("corriendo", "Empezando el barrido...", 0, forzar=True)

    try:
        resultado = api_client.gestionar_barrido(
            callback_estado=_log, limite_minutos=LIMITE_MINUTOS)
    except Exception as e:
        print(f"  ERROR en el barrido: {e}", flush=True)
        _avisar_a_todas("error", str(e))
        return 1

    descargadas = resultado.get("descargadas", 0)
    soltadas = resultado.get("soltadas", 0)
    dias_fallidos = resultado.get("dias_fallidos") or []
    fichas_fallidas = resultado.get("fichas_fallidas", 0)

    # Resumen siempre, salga como salga: es lo único que queda en el log de
    # Actions cuando alguien va a mirar por qué falta algo.
    print(f"Barrido nocturno: {descargadas} licitación(es) nueva(s) guardada(s); "
          f"{soltadas} soltada(s) por cambio de estado; "
          f"{len(dias_fallidos)} día(s) que el portal no listó; "
          f"{fichas_fallidas} ficha(s) que el portal no sirvió.", flush=True)

    # Una corrida que no pudo listar Y no trajo nada no hizo su trabajo: no puede
    # quedar en verde. Desde fuera sería indistinguible de un día tranquilo.
    if dias_fallidos and not descargadas:
        detalle = (f"Barrido nocturno fallido: el portal no respondió al listar "
                   f"{len(dias_fallidos)} día(s) y no se bajó nada.")
        _avisar_a_todas("error", detalle)
        print(f"ERROR: {detalle}", flush=True)
        return 1

    if not resultado.get("completo"):
        pendientes = resultado.get("pendientes", 0)
        detalle = (f"Barrido nocturno parcial: quedan {pendientes} licitaciones, "
                   "se completan en la próxima corrida." if pendientes else
                   "Barrido nocturno parcial: se acabó el tiempo antes de mirarlo "
                   "todo, sigue en la próxima corrida.")
    elif dias_fallidos:
        # Sí trajo datos, así que la app debe refrescarse ('listo' y no 'error'),
        # pero lo que hay está incompleto y conviene que se sepa.
        detalle = (f"Barrido nocturno incompleto: el portal no listó "
                   f"{len(dias_fallidos)} día(s). Se recuperan en el próximo.")
    elif fichas_fallidas:
        detalle = (f"Barrido nocturno completado: {descargadas} licitaciones nuevas. "
                   f"El portal no sirvió la ficha de {fichas_fallidas}; se "
                   "reintentan en el próximo.")
    else:
        detalle = f"Barrido nocturno completado: {descargadas} licitaciones nuevas."

    _avisar_a_todas("listo", detalle)
    print(detalle, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
