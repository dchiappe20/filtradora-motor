# -*- coding: utf-8 -*-
"""
descargar_licitaciones_rango.py — Descarga de Licitaciones por rango (headless).

Lo dispara la app (botón "Descargar datos") vía GitHub Actions con las fechas como
inputs. Hace el mismo trabajo que la descarga local —pero en el servidor, con las
peticiones en paralelo y fuera de la IP del usuario— y REEMPLAZA la tabla
`licitaciones` en Supabase con el rango pedido. Además va reportando el avance en
la tabla `descarga_estado` para que la app lo muestre y sepa cuándo terminó.

Fechas (formato dd-mm-yyyy) desde variables de entorno FECHA_INICIO / FECHA_FIN
(o como argumentos posicionales). Sale 0 si terminó bien, 1 si hubo error.
"""
import os
import sys
import time
from datetime import datetime

import auth
import api_client
import datos_nube

_MODULO = "licitaciones"
_ultimo_estado = {"t": 0.0}  # throttle de escrituras de estado a la nube


class _DescargaCancelada(Exception):
    """El usuario canceló la descarga (bandera `cancelar` en la nube)."""


def _reportar(mensaje, progreso=None):
    """Callback: imprime al log y refleja el avance en `descarga_estado` (throttled).
    Además, en cada ventana revisa si se pidió cancelar; si es así, corta la descarga
    y —clave— NO vuelve a escribir 'corriendo' para no pisar el 'cancelado'."""
    print(mensaje, flush=True)
    ahora = time.time()
    if ahora - _ultimo_estado["t"] >= 4:
        _ultimo_estado["t"] = ahora
        if datos_nube.cancelacion_solicitada(_MODULO):
            raise _DescargaCancelada()
        try:
            datos_nube.escribir_estado_descarga(_MODULO, "corriendo", mensaje)
        except Exception:
            pass  # el estado es informativo; un fallo no debe cortar la descarga


def main():
    fecha_inicio = os.environ.get("FECHA_INICIO") or (sys.argv[1] if len(sys.argv) > 1 else "")
    fecha_fin = os.environ.get("FECHA_FIN") or (sys.argv[2] if len(sys.argv) > 2 else "")

    if not fecha_inicio or not fecha_fin:
        print("ERROR: faltan FECHA_INICIO / FECHA_FIN (dd-mm-yyyy).", flush=True)
        return 1
    try:
        datetime.strptime(fecha_inicio, "%d-%m-%Y")
        datetime.strptime(fecha_fin, "%d-%m-%Y")
    except ValueError:
        print(f"ERROR: fechas con formato inválido: '{fecha_inicio}' / '{fecha_fin}'.", flush=True)
        return 1

    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    if not os.environ.get("EMPRESA_ID", "").strip():
        print("ERROR: falta EMPRESA_ID (no se sabe en qué empresa guardar).", flush=True)
        return 1

    rango = f"{fecha_inicio} a {fecha_fin}"
    print(f"Descargando Licitaciones del rango {rango}...", flush=True)

    # NO se limpia la bandera aquí a propósito: si el usuario canceló durante el
    # arranque del runner (checkout/pip), la bandera ya está puesta y hay que
    # respetarla. La limpia la app ANTES de disparar un run nuevo.

    # Estado inicial DENTRO de try: si falla aquí (p. ej. permisos/empresa), igual
    # salimos con error visible en el log, sin dejar la app esperando eternamente.
    try:
        datos_nube.escribir_estado_descarga(_MODULO, "corriendo", f"Iniciando rango {rango}...")
    except Exception as e:
        print(f"ERROR al registrar el estado inicial: {e}", flush=True)
        return 1

    try:
        api_client.gestionar_descarga_rango(fecha_inicio, fecha_fin, callback_estado=_reportar)
    except _DescargaCancelada:
        print("Cancelada por el usuario.", flush=True)
        try:
            datos_nube.escribir_estado_descarga(_MODULO, "cancelado", "Cancelada por el usuario.")
            datos_nube.limpiar_cancelacion(_MODULO)
        except Exception:
            pass
        return 0
    except Exception as e:
        print(f"ERROR durante la descarga: {e}", flush=True)
        try:
            datos_nube.escribir_estado_descarga(_MODULO, "error", str(e))
        except Exception:
            pass
        return 1

    datos_nube.escribir_estado_descarga(_MODULO, "listo", f"Rango {rango} descargado.")
    print("Descarga de Licitaciones completada y subida a la nube.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
