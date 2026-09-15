# -*- coding: utf-8 -*-
"""
barrido_llamados.py — El repaso del día: quién pasó a 2do llamado.

QUÉ PROBLEMA RESUELVE

Cuando vence el primer cierre de una Compra Ágil, el portal puede reabrirla en
SEGUNDO LLAMADO. Hasta ahora eso sólo lo veía el barrido nocturno, así que una
cotización que reabría a las 09:00 no volvía a la pantalla hasta la madrugada
siguiente: el cliente la veía como «Cerrada» y perdía el día entero para ofertar.

La función de Supabase `compra-agil-reciente` tampoco puede verlo. Lista lo
publicado HOY y sólo baja fichas de códigos que la base no tiene; una cotización
reabierta conserva su fecha de publicación original —medido el 2026-09-15— y ya
está guardada, así que nunca pasa por ahí.

CÓMO LO RESUELVE

Cuatro pasadas al día (08:00, 11:00, 14:00 y 17:00 de Chile) que recorren el
listado de los últimos `DIAS_REPASO` días pidiendo sólo las ABIERTAS. El llamado
viene en el propio listado (`estado_convocatoria`), así que se compara con lo
guardado y sólo se baja la ficha de lo que cambió. Una reapertura se ve en 1,5
horas en promedio, 3 en el peor caso.

Medido el 2026-09-15: los 10 días anteriores son 3.719 cotizaciones abiertas,
189 páginas de 20 — lo mismo que ya recorre el nocturno cada noche. Los días 6 a
10 sólo aportan 24 de esas páginas, así que pedir 10 días en vez de 5 sale casi
gratis y da margen para fines de semana largos.

QUÉ NO HACE, Y POR QUÉ

  · NO mira lo publicado hoy. Una cotización no puede reabrirse el mismo día en
    que se publicó (primero tiene que vencer su primer cierre, ~24 h después), y
    hoy es el día más caro del listado: 204 páginas de las 393 que habría.
    Además de lo de hoy ya se encarga la función de cada 5 minutos.

  · NO suelta nada de la tabla (`podar=False`). Soltar es cosa del nocturno, que
    es el único que recorre la ventana entera y sabe a qué estado pasó cada una
    —lo necesita el historial de clientes—. Además cada borrado obliga a las apps
    a bajarse la lista de códigos vivos (~1 MB) para saber qué quitar de su copia
    local, y hacerlo cuatro veces al día no compensa.

  · NO escribe en `descarga_estado`. Si se anunciara como un barrido en curso,
    `motor.disparar_compra_agil_reciente` se saltaría sus tandas durante los ~15
    minutos que dura, y lo recién publicado llegaría tarde cuatro veces al día.
    No hay riesgo de pisarse: este repaso no toca lo de hoy y una cotización
    reabierta nunca es del día, así que los dos nunca escriben el mismo código.

Uso:  python barrido_llamados.py

Códigos de salida: 0 si el repaso terminó (aunque el portal dejara caer algún
día); 1 si no se pudo ni empezar.
"""
import sys
import time

import auth
import compra_agil_api

# Cuántos días hacia atrás se repasan. Ver el cálculo de arriba: lo que cuesta
# está en los primeros días, los últimos son casi gratis.
DIAS_REPASO = 10

# Tope de la corrida. Una pasada mide 12-17 min; con 40 hay sitio para un día
# malo del portal y sigue cabiendo de sobra entre dos pasadas (3 h).
LIMITE_MINUTOS = 40


def _log(mensaje, progreso=None, publico=None):
    """El detalle va al log de la corrida. Aquí no hay canal para el cliente:
    este barrido no se anuncia en la app (ver la cabecera)."""
    marca = time.strftime("%H:%M:%S")
    porcentaje = f" ({progreso}%)" if progreso is not None else ""
    print(f"[{marca}] {mensaje}{porcentaje}", flush=True)


def main():
    if auth.supabase is None:
        print("ERROR: no hay cliente Supabase (credenciales o conexión).", flush=True)
        return 1

    print(f"Repaso de LLAMADOS de Compra Ágil (últimos {DIAS_REPASO} días, "
          "sin contar hoy) — copia compartida.", flush=True)

    try:
        resultado = compra_agil_api.gestionar_descarga_ultimas(
            callback_estado=_log,
            limite_minutos=LIMITE_MINUTOS,
            dias_atras=DIAS_REPASO,
            incluir_hoy=False,
            podar=False,
        )
    except Exception as e:
        print(f"  ERROR en el repaso: {e}", flush=True)
        return 1

    descargadas = resultado.get("descargadas", 0)
    dias_fallidos = resultado.get("dias_fallidos") or []
    paginas = resultado.get("paginas_perdidas", 0)
    fichas_fallidas = resultado.get("fichas_fallidas", 0)

    print(f"\nListo: {descargadas} cotización(es) actualizada(s) "
          f"(nuevas o con el llamado cambiado).", flush=True)
    if dias_fallidos:
        print(f"  ⚠️ El portal no respondió del todo en {len(dias_fallidos)} día(s) "
              f"({paginas} página(s) perdida(s)): {', '.join(dias_fallidos)}. "
              "Lo que falte se vuelve a mirar en la pasada siguiente y, en el peor "
              "caso, de madrugada.", flush=True)
    if fichas_fallidas:
        print(f"  ⚠️ {fichas_fallidas} ficha(s) que el portal no sirvió.", flush=True)

    # Que el portal se cayera no es un fallo NUESTRO: la pasada siguiente y el
    # nocturno lo cubren. Sólo se devuelve error si no se pudo ni empezar, que es
    # lo que conviene ver en rojo en Actions.
    return 0


if __name__ == "__main__":
    sys.exit(main())
