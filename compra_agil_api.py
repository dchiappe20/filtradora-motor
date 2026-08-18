# compra_agil_api.py
import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import os

import datos_nube

API_URL = "https://api.buscador.mercadopublico.cl/compra-agil"

# Esta clave NO es un secreto nuestro: es la que el propio buscador de Mercado
# Publico lleva en su front, visible en las herramientas de desarrollo de
# cualquier navegador. Puede quedar a la vista en un repo publico. Se deja
# igualmente sobreescribible por entorno para el dia que el portal la cambie:
# asi se arregla poniendo un Secret, sin recompilar ni publicar una version.
API_KEY = os.environ.get("MP_BUSCADOR_API_KEY", "e93089e4-437c-4723-b343-4fa20045e3bc")

_HEADERS = {
    "x-api-key": API_KEY,
    "Referer": "https://buscador.mercadopublico.cl/",
    "Origin": "https://buscador.mercadopublico.cl",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

URL_FICHA_COMPRA_AGIL = "https://buscador.mercadopublico.cl/ficha?code={codigo}"
URL_PERFIL_COMPRADOR = "https://comprador.mercadopublico.cl/ficha/{rut}"

_LLAMADO_TEXTOS = {1: "1er llamado", 2: "2do llamado"}

# El listado devuelve la cotización en cualquier estado: 'Publicada', 'Cerrada',
# 'Proveedor seleccionado', 'Cancelada' o 'Desierta'. Sólo la primera sirve: es
# la única a la que todavía se le puede cotizar.
#
# Filtrar por aquí importa mucho porque las Compra Ágil cierran en ~1 día.
# Medido: al día siguiente de publicarse sigue abierta el 98%, pero a los 3 días
# ya sólo el 14% (54% cerradas, 24% adjudicadas). Sin este filtro, de las
# ~33.000 de la ventana la mayoría son fichas que ya no se pueden aprovechar.
ESTADO_ABIERTA = "publicada"

# Valor que entiende la API para pedir sólo las abiertas (ver `_listar_pagina`).
ESTADO_PUBLICADA = 2

# Techo de seguridad de páginas a recorrer al listar cotizaciones de UN día (50
# ítems c/u). El listado se recorre día por día (no pidiendo la ventana entera)
# porque el portal corta la paginación profunda (403/429) al pasar de ~100 páginas
# seguidas; por día alcanza con ~85 páginas (~4.300 cotizaciones). El bucle se
# detiene solo al alcanzar el `pageCount|` real del día, así que este número solo
# evita un bucle infinito si la API se comporta mal. 300 deja margen holgado.
MAX_PAGINAS_LISTADO = 300

# Cuánto se espera una página del listado antes de darla por perdida.
#
# Eran 20 s, y con eso los barridos del día no traían NADA: medido el 2026-08-11
# a las 18:00, el portal contesta 200 OK pero tarda entre 12 y 26 s por página
# (media 17,6 s), así que la mitad de las peticiones se abortaban justo antes de
# llegar. Con 5 reintentos, cada día se perdía tras ~3 min de espera inútil. De
# madrugada contesta en menos de un segundo, por eso el barrido nocturno nunca
# lo notó. Con 60 s hay margen de sobra incluso desde los runners de EE.UU.
TIMEOUT_LISTADO = 60

# Reintentos por página. Bajaron de 5 a 3 al subir el timeout: con 60 s de
# espera, agotar 5 intentos serían 5 minutos tirados en una sola página.
INTENTOS_LISTADO = 3

# Rondas de RESCATE sobre las páginas que el portal dejó caer.
#
# Esto no es lo mismo que `INTENTOS_LISTADO`, que insiste en el acto: es volver a
# por ellas al terminar el día, cuando la ráfaga de errores ya pasó. Hace falta
# porque el portal falla A RACHAS. Medido el 2026-08-12: el barrido de las 10:00
# perdió páginas y se trajo 600 de las ~1.200 publicadas esa mañana, y el de las
# 16:00 se quedó sin la página 1 y terminó con «Sin novedades» habiendo bajado
# CERO. Antes esas cotizaciones no se volvían a pedir hasta la corrida siguiente.
#
# Son 5 porque con 3 no bastaba: probado contra el portal real esa misma tarde,
# la página 1 sólo contestó al cuarto intento y las 11 páginas que se cayeron
# tardaron las tres rondas en volver. El coste de rondas de más lo acota el
# `corte` por tiempo, no este número.
RONDAS_RESCATE = 5

# Espera entre rondas de rescate. Insistir al instante no sirve de nada cuando el
# portal está saturado: lo que lo arregla es dejarlo respirar.
ESPERA_RESCATE = 20

# Peticiones en paralelo al listar páginas y bajar fichas. El cuello de botella es
# la latencia de la API (no CPU ni ancho de banda), sobre todo desde servidores
# lejanos (p. ej. runners de GitHub en EE.UU., donde el barrido secuencial no cabía
# en 1 hora). Se midió que 6 hilos concurrentes NO disparan bloqueos (403/429) del
# portal y aceleran ~6x. Subir este número arriesga rate-limit; bajarlo va más lento.
MAX_WORKERS = 6

# Hilos de la segunda pasada sobre las fichas que fallaron. Menos que MAX_WORKERS
# a propósito: parte de esos fallos son del propio paralelismo, y pedirlas de
# nuevo con menos presión recupera unas cuantas.
MAX_WORKERS_RESCATE = 3

# Cuánto se espera una ficha. Eran 15 s, heredados de cuando el portal iba fino,
# y descartaban fichas buenas: medido el 2026-08-12, una ficha válida tardó 29 s
# en contestar. Por encima de eso no hay nada que rascar — el API Gateway del
# portal corta en seco a los ~29 s con `504 Endpoint request timed out`—, así que
# 40 s cubre todo lo recuperable sin quedarse esperando lo que nunca va a llegar.
TIMEOUT_FICHA = 40

# Intentos por ficha en la pasada normal. Los 504 del gateway suelen repetirse,
# así que insistir más aquí es tiempo tirado: lo que rescata algo es la segunda
# pasada del final, ya con el portal más descargado.
INTENTOS_FICHA = 3

# Cada cuántas fichas se sube lo descargado. Guardar sobre la marcha es lo que
# permite que una corrida cortada (por tiempo o por un fallo) no se pierda: lo
# ya subido deja de pedirse, así que la siguiente continúa donde quedó. La
# primera población baja ~30.000 fichas y no cabe en una sola corrida.
LOTE_GUARDADO = 500

# Días hacia atrás que abarca el barrido COMPLETO (el nocturno): se listan todos
# y se conserva en la tabla lo publicado dentro de esa ventana.
#
# Eran 10. Ampliarlo a 30 sale casi gratis porque el listado se pide con
# `status=2` (sólo las abiertas) y a esa altura ya no queda casi nada abierto:
# un día de hace 8 jornadas son 2 páginas, y los de 10-30 días atrás traen aún
# menos. Tampoco engorda la tabla, que sólo guarda lo que sigue abierto. A
# cambio, no se pierden las pocas cotizaciones de plazo largo que antes se
# soltaban al cumplir 10 días aunque siguieran vivas.
DIAS_VENTANA = 30


def _get_appdata_dir() -> str:
    path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Amilab")
    os.makedirs(path, exist_ok=True)
    return path


# Tabla en Supabase donde viven los datos (reemplaza al Excel local)
TABLA_NUBE = "compra_agil"

COLUMNAS_COMPLETAS = [
    "Numero Adquisición", "Nombre Adquisición", "Organismo", "RUT Organismo",
    "Fecha Publicación", "Fecha Cierre", "Fecha Cierre 1er Llamado", "Fecha Cierre 2do Llamado",
    "Llamado", "Cantidad", "Descripción Producto", "Nombre Producto",
]


def _listar_pagina(date_from, date_to, page_number, page_size=50):
    # page_size está topado en 50 por la API (valores mayores devuelven 400), por
    # eso recorrer la ventana completa exige muchas páginas.
    #
    # `status=2` es el filtro de la API para 'Publicada' (el nombre no es obvio:
    # `estado`, `id_estado` y compañía los ignora en silencio y devuelve todo).
    # Pedir sólo las abiertas recorta muchísimo: en un día de hace 8 jornadas
    # baja de 77 páginas a 2 (−97%), porque casi todo lo de esos días ya cerró.
    # Comprobado que no se pierde ninguna abierta al filtrar.
    params = {
        "page_number": page_number,
        "page_size": page_size,
        "order_by": "recent",
        "status": ESTADO_PUBLICADA,
        "date_from": date_from,
        "date_to": date_to,
    }
    espera = 3
    for _ in range(INTENTOS_LISTADO):
        try:
            resp = requests.get(API_URL, params=params, headers=_HEADERS,
                                timeout=TIMEOUT_LISTADO)
            if resp.status_code == 200:
                return resp.json().get("payload") or {}
            # 403/429: el portal limita la paginación rápida/profunda. 5xx/408:
            # error transitorio. En ambos casos reintentamos con espera creciente.
            if resp.status_code in (403, 408, 429, 500, 502, 503, 504):
                time.sleep(espera)
                espera = min(espera * 2, 30)
                continue
            # Otros (p. ej. 400 por parámetros) no se arreglan reintentando.
            return None
        except Exception:
            time.sleep(espera)
            espera = min(espera * 2, 30)
    return None


def _obtener_ficha(codigo):
    params = {"action": "ficha", "code": codigo}
    intentos = 0
    while intentos < INTENTOS_FICHA:
        try:
            resp = requests.get(API_URL, params=params, headers=_HEADERS,
                                timeout=TIMEOUT_FICHA)
            if resp.status_code == 200:
                datos = resp.json()
                if datos.get("success") == "OK":
                    return datos.get("payload")
                return None
            elif resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2)
                intentos += 1
            else:
                return None
        except Exception:
            time.sleep(2)
            intentos += 1
    return None


def _procesar_ficha(ficha):
    codigo = ficha.get("codigo", "")
    nombre = ficha.get("nombre", "")
    descripcion_general = ficha.get("descripcion", "")
    info = ficha.get("informacion_institucion") or {}
    organismo = info.get("organismo_comprador", "")
    rut_organismo = info.get("rut_organismo_comprador", "")
    f_pub = ficha.get("fecha_publicacion", "")
    f_cierre1 = ficha.get("fecha_cierre_primer_llamado") or ficha.get("fecha_cierre", "")
    f_cierre2 = ficha.get("fecha_cierre_segundo_llamado") or ""
    llamado = _LLAMADO_TEXTOS.get(ficha.get("estado_convocatoria"), "")

    productos = ficha.get("productos_solicitados") or []
    if not productos:
        productos = [{"nombre": "", "descripcion": descripcion_general, "cantidad": ""}]

    filas = []
    for prod in productos:
        nombre_prod = str(prod.get("nombre", "")).strip()
        desc_prod = str(prod.get("descripcion", "")).strip().replace("_x000D_", "")
        desc_final = desc_prod if desc_prod else nombre_prod

        filas.append({
            "Numero Adquisición": codigo,
            "Nombre Adquisición": nombre,
            "Organismo": organismo,
            "RUT Organismo": rut_organismo,
            "Fecha Publicación": f_pub,
            "Fecha Cierre": f_cierre1,
            "Fecha Cierre 1er Llamado": f_cierre1,
            "Fecha Cierre 2do Llamado": f_cierre2,
            "Llamado": llamado,
            "Cantidad": prod.get("cantidad", ""),
            "Descripción Producto": desc_final,
            # Sólo el nombre del producto: lo demás que llevaba el antiguo
            # «Texto Filtrado» ya son columnas y el filtrado las recorre todas.
            # Cuando hay descripción, el nombre no aparece en ningún otro sitio,
            # así que sin esto se perderían coincidencias.
            "Nombre Producto": nombre_prod,
        })
    return filas


def _codigos_fuera_de_ventana(df, date_from):
    """Códigos ya guardados que el portal no volverá a listar, y que por tanto
    hay que soltar para que la tabla no crezca sin fin.

    Se mira la fecha de publicación —el mismo criterio con el que se lista— con
    un día de margen para no soltar los del borde. Lo que sigue dentro de la
    ventana se conserva aunque su llamado ya haya cerrado: si se borrara, el
    listado lo devolvería mañana y habría que volver a bajar la ficha."""
    if df is None or df.empty or "Fecha Publicación" not in df.columns:
        return set()
    limite = pd.Timestamp(date_from) - pd.Timedelta(days=1)
    f_pub = pd.to_datetime(df["Fecha Publicación"], errors="coerce")
    fuera = df[f_pub.notna() & (f_pub < limite)]
    return set(fuera["Numero Adquisición"].astype(str))


def _dias_a_listar(date_from, date_to, incremental):
    """Qué días de publicación hay que pedirle al portal.

    · Barrido COMPLETO (nocturno): la ventana entera, de `date_from` a hoy. Es
      el único que vuelve sobre los días viejos, y por eso el único que detecta
      los cambios de llamado y los cierres.

    · Barrido DEL DÍA (10:00 y 16:00): SÓLO hoy. Nada más. Su trabajo es traer
      lo que se publicó desde el barrido anterior, no repasar el pasado — eso
      cuesta minutos de portal y ya lo hace el nocturno. Lo que se resuelve
      solo, sin preguntarle nada al portal, se resuelve por fecha: ver
      `_codigos_segundo_cierre_vencido` y `compra_agil._llamado_visible`.
    """
    inicio = date_to if incremental else date_from
    return [inicio + timedelta(days=i) for i in range((date_to - inicio).days + 1)]


def _codigos_segundo_cierre_vencido(df_unicos, ahora):
    """Cotizaciones cuyo 2do llamado ya cerró: se sueltan de la tabla.

    No hace falta preguntarle al portal — con la fecha guardada basta. Cuando
    vence el segundo cierre la cotización se terminó del todo: ya no admite
    ofertas y no va a volver a cambiar. La pantalla igual las ocultaba al
    pintar; soltarlas aquí evita además que sigan ocupando la tabla."""
    if df_unicos is None or df_unicos.empty:
        return set()
    cierre2 = pd.to_datetime(df_unicos["Fecha Cierre 2do Llamado"], errors="coerce")
    vencidas = df_unicos[cierre2.notna() & (cierre2 < ahora)]
    return set(vencidas["Numero Adquisición"].astype(str))


def _codigos_ausentes(df_unicos, dias_podables, vistos, codigos_descargar):
    """Códigos guardados que ya no salen entre las abiertas del día en que se
    publicaron: cerraron, se adjudicaron, se cancelaron o quedaron desiertas.

    Como el listado se pide con `status=2`, no aparecer en él es la única señal
    de que una cotización dejó de admitir ofertas. Sólo vale para los días que se
    listaron ENTEROS y sin fallos (`dias_podables`): en un barrido incremental no
    se mira toda la ventana, y dar por cerrado lo que ni siquiera se preguntó
    borraría datos buenos."""
    if df_unicos is None or df_unicos.empty or not dias_podables:
        return set()
    dia_pub = pd.to_datetime(df_unicos["Fecha Publicación"], errors="coerce").dt.strftime("%Y-%m-%d")
    candidatos = df_unicos[dia_pub.isin(dias_podables)]
    if candidatos.empty:
        return set()
    return set(candidatos["Numero Adquisición"].astype(str)) - vistos - set(codigos_descargar)


def _procesar_pagina_listado(payload, vistos, llamado_por_codigo, codigos_descargar,
                             codigos_cerrados=None):
    """Recorre una página del listado y anota qué fichas hay que bajar.

    Se toman las cotizaciones ABIERTAS ('Publicada'), en 1er y en 2do llamado:
    las pymes también compiten en el primero, y filtrar por llamado es cosa de
    cada empresa en su pantalla.

    La API ya devuelve sólo abiertas (`status=2` en `_listar_pagina`), así que
    la comprobación del estado que hay aquí es una red de seguridad por si ese
    parámetro dejara de funcionar: lo que llegue cerrado se aparta en
    `codigos_cerrados` para soltarlo del caché en vez de darlo por bueno.

    De las abiertas sólo se baja la ficha de lo que falta o de lo que cambió de
    llamado desde la última vez, que es lo que mantiene corto el barrido.

    Muta las estructuras compartidas, así que debe llamarse SIEMPRE desde el
    hilo principal (nunca dentro de un worker)."""
    for item in (payload.get("resultados") or []):
        codigo = str(item.get("codigo", ""))
        if not codigo or codigo in vistos:
            continue
        vistos.add(codigo)

        if str(item.get("estado", "")).strip().lower() != ESTADO_ABIERTA:
            if codigos_cerrados is not None:
                codigos_cerrados.add(codigo)
            continue

        estado = _LLAMADO_TEXTOS.get(item.get("estado_convocatoria"), "")
        cacheado = llamado_por_codigo.get(codigo)
        if cacheado is None or (estado and cacheado != estado):
            codigos_descargar.append(codigo)


def _completadas_cancelable(futuros, cancel_event):
    """Itera los futuros a medida que terminan, pero revisa `cancel_event` cada 0.5s
    aunque ninguno haya terminado todavía. Así cancelar responde al instante aun si
    los workers están atascados en peticiones lentas o reintentando (con `as_completed`
    el hilo quedaba bloqueado esperando y la cancelación no se enteraba). Lanza
    InterruptedError al cancelar; el `finally` del llamador apaga el executor."""
    pendientes = set(futuros)
    while pendientes:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelado")
        terminadas, pendientes = wait(pendientes, timeout=0.5, return_when=FIRST_COMPLETED)
        for fut in terminadas:
            yield fut


def _pedir_paginas(dia_str, paginas, cancel_event, al_llegar):
    """Pide en paralelo esas páginas del listado de un día.

    `al_llegar(payload)` se llama EN ESTE HILO por cada página que contesta, así
    el procesado de las estructuras compartidas sigue siendo de un solo hilo (ver
    `_procesar_pagina_listado`).

    Devuelve la lista de páginas que NO contestaron, para poder volver a por
    ellas. Que una página se pierda no es raro ni inofensivo: son ~50
    cotizaciones que no se bajan."""
    fallidas = []
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futuros = {executor.submit(_listar_pagina, dia_str, dia_str, p): p
                   for p in paginas}
        for fut in _completadas_cancelable(futuros, cancel_event):
            payload = fut.result()
            if payload:
                al_llegar(payload)
            else:
                fallidas.append(futuros[fut])
    finally:
        # cancel_futures corta las páginas pendientes al instante si el usuario
        # cancela (el callback lanza InterruptedError) o ante cualquier error.
        executor.shutdown(wait=False, cancel_futures=True)
    return fallidas


def _agotado(corte):
    return corte is not None and time.monotonic() > corte


def _listar_dia(dia_str, cancel_event, al_llegar, callback_estado=None, corte=None):
    """Recorre el listado ENTERO de un día, insistiendo con lo que se caiga.

    `corte` es el instante (monotonic) en que hay que dejar de insistir. Sin él,
    con el portal caído del todo, las rondas de rescate se comerían el tiempo de
    la corrida y ni siquiera se llegaría a bajar las fichas de lo que sí se listó.

    Devuelve (paginas_perdidas, se_pudo_empezar):

      · `paginas_perdidas`: cuántas páginas no contestaron ni tras las rondas de
        rescate. Si es 0 el día se listó completo y se puede podar con él.
      · `se_pudo_empezar`: False si ni la página 1 contestó, en cuyo caso no se
        sabe siquiera cuántas páginas tenía el día y no se listó nada.

    La página 1 va aparte porque es la que revela `pageCount`: sin ella el día
    entero se pierde en silencio, que es justo lo que pasó el 2026-08-12 a las
    16:00."""
    payload = _listar_pagina(dia_str, dia_str, 1)
    for ronda in range(RONDAS_RESCATE):
        if payload or _agotado(corte):
            break
        if callback_estado:
            callback_estado(
                f"El portal no contesta con el día {dia_str}: reintentando "
                f"({ronda + 1}/{RONDAS_RESCATE})...", None)
        time.sleep(ESPERA_RESCATE)
        payload = _listar_pagina(dia_str, dia_str, 1)
    if not payload:
        return 0, False

    al_llegar(payload)
    page_count = min(payload.get("pageCount", 1), MAX_PAGINAS_LISTADO)
    pendientes = list(range(2, page_count + 1))

    # Páginas 2..page_count EN PARALELO (la latencia de la API domina, no la CPU),
    # y otras tantas rondas sobre las que se caigan.
    for ronda in range(RONDAS_RESCATE + 1):
        if not pendientes or (ronda and _agotado(corte)):
            break
        if ronda:
            if callback_estado:
                callback_estado(
                    f"Reintentando {len(pendientes)} página(s) que el portal dejó "
                    f"caer del día {dia_str} ({ronda}/{RONDAS_RESCATE})...", None)
            time.sleep(ESPERA_RESCATE)
        pendientes = _pedir_paginas(dia_str, pendientes, cancel_event, al_llegar)
    return len(pendientes), True


def gestionar_descarga_ultimas(callback_estado=None, cancel_event=None,
                               limite_minutos=None, incremental=False):
    """
    Descarga TODAS las cotizaciones de Compra Ágil publicadas en los últimos
    `DIAS_VENTANA` días, en 1er y en 2do llamado.

    Con `incremental=True` se lista SÓLO el día de hoy: es el barrido corto de
    media mañana y media tarde, y su único trabajo es traer lo recién publicado.
    No vuelve sobre los días anteriores —eso es del barrido completo de la
    madrugada, que es quien detecta los pasos de 1er a 2do llamado y los cierres.
    Lo que se puede resolver con la fecha ya guardada se resuelve igual en los
    dos modos, sin pedirle nada al portal:

      · vencido el 2do cierre, la cotización se suelta de la tabla;
      · vencido el 1er cierre, la pantalla la muestra como «Revisar en web»
        hasta que el nocturno confirme en qué quedó (`_llamado_visible`).

    Con `limite_minutos` la corrida se corta sola al agotarse ese tiempo, después
    de guardar lo que lleve. No es un capricho: GitHub Actions mata cualquier
    trabajo a las 6 horas, y la primera población (~30.000 fichas a ~50/min) no
    cabe ahí. Cortando a tiempo y guardando por lotes, lo descargado queda en la
    nube y la corrida siguiente sigue donde ésta lo dejó, hasta completarla en
    dos o tres noches. De régimen sobra con una.

    Devuelve {"completo": bool, "descargadas": int, "pendientes": int,
    "dias_fallidos": [str], "paginas_perdidas": int, "fichas_fallidas": int}.

    `descargadas` son las fichas que de verdad llegaron y se guardaron, no los
    intentos: antes se contaban también las que el portal no sirvió y el barrido
    informaba «600 de 600» habiendo dejado 538 en la tabla. Los tres últimos
    campos son para que quien llame sepa si el barrido pudo hacer su trabajo: con
    `dias_fallidos` no vacío, lo que hay en la nube está incompleto.

    Antes sólo se bajaban las de 2do llamado, y eso dejaba fuera a las pymes que
    compiten en el primero. Ahora se guarda todo y es cada empresa la que filtra
    en su pantalla (el selector «Llamado»).

    El estado viene en el propio listado (`estado_convocatoria`), así que se sabe
    ANTES de pedir la ficha si algo cambió: sólo se bajan las fichas nuevas y las
    de cotizaciones que pasaron de un llamado a otro. Lo ya guardado se conserva.

    El caché guarda el llamado TAL CUAL lo dice el portal. Que un primer cierre
    ya haya pasado no se anota aquí: eso lo decide la pantalla al pintar, con la
    hora de ese momento (ver `compra_agil._llamado_visible`).
    """
    # La hora es la de Chile: los runners de GitHub corren en UTC, así que los
    # workflows fijan TZ=America/Santiago. Si no, «hoy» sería el día siguiente
    # durante toda la madrugada chilena.
    ahora = datetime.now()
    date_to = ahora.date()
    date_from = (ahora - timedelta(days=DIAS_VENTANA)).date()

    # El corte se calcula ya, no al empezar a bajar fichas: el listado también
    # tarda —y más aún ahora que insiste con las páginas que se caen—, así que si
    # quedara fuera del presupuesto, un portal atascado podría comerse la corrida
    # entera reintentando y no dejar tiempo para bajar nada de lo que sí listó.
    corte = (time.monotonic() + limite_minutos * 60) if limite_minutos else None

    if callback_estado:
        callback_estado("Leyendo datos existentes en la nube...", 0)

    try:
        df_cache = datos_nube.leer_tabla(TABLA_NUBE)
    except datos_nube.ErrorNube:
        df_cache = pd.DataFrame(columns=COLUMNAS_COMPLETAS)

    # Una fila por cotización (la tabla trae una por producto): es lo que se usa
    # para saber qué hay guardado, qué días revisar y qué soltar.
    unicos = None
    llamado_por_codigo = {}
    if not df_cache.empty and "Numero Adquisición" in df_cache.columns:
        unicos = df_cache.drop_duplicates(subset=["Numero Adquisición"])
        # Mapa codigo -> llamado que ya tenemos guardado, para no volver a pedir
        # fichas que no han cambiado.
        llamado_por_codigo = dict(zip(
            unicos["Numero Adquisición"].astype(str),
            unicos["Llamado"].astype(str).str.strip(),
        ))

    # Enumerar el listado, día por día, y quedarnos con lo que falta por bajar
    # (nuevo, o que cambió de llamado). El estado viene en el propio listado,
    # sin necesidad de bajar la ficha.
    # Se recorre por día (en vez de pedir la ventana entera de una) porque el
    # portal corta la paginación al llegar a páginas altas; por día la paginación
    # reinicia en 1 y nunca llega a esa zona. Si un día falla, se sigue con los
    # demás en vez de abortar toda la descarga.
    dias = _dias_a_listar(date_from, date_to, incremental)
    codigos_descargar = []
    vistos = set()
    codigos_cerrados = set()   # ya no admiten cotización: se sueltan del caché
    dias_fallidos = []
    dias_ok = []               # listados enteros y sin fallos
    paginas_perdidas = 0       # páginas que no contestaron ni tras el rescate
    listado_cortado = False    # se acabó el tiempo antes de mirar todos los días

    if callback_estado:
        callback_estado(
            f"Revisando qué hay de nuevo ({len(dias)} día(s), "
            f"desde el {dias[0].strftime('%d-%m')})...", 0)

    for idx_dia, dia in enumerate(dias, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelado")
        if _agotado(corte):
            # Mejor parar y bajar las fichas de lo ya listado que seguir listando
            # días que luego no daría tiempo a descargar. Los que quedan se miran
            # en la próxima corrida.
            listado_cortado = True
            break
        dia_str = dia.strftime("%Y-%m-%d")
        if callback_estado:
            callback_estado(
                f"Revisando cotizaciones... día {dia_str} "
                f"({idx_dia}/{len(dias)}) ({len(codigos_descargar)} por descargar)", 0)

        def _al_llegar(payload, dia_str=dia_str, idx_dia=idx_dia):
            _procesar_pagina_listado(payload, vistos, llamado_por_codigo,
                                     codigos_descargar, codigos_cerrados)
            if callback_estado:
                callback_estado(
                    f"Revisando cotizaciones... día {dia_str} "
                    f"({idx_dia}/{len(dias)}) ({len(codigos_descargar)} por descargar)", 0)

        perdidas, se_pudo = _listar_dia(dia_str, cancel_event, _al_llegar,
                                        callback_estado, corte)
        paginas_perdidas += perdidas
        if not se_pudo or perdidas:
            dias_fallidos.append(dia_str)
            # Si además se acabó el tiempo, lo que falta no es culpa del portal:
            # es que dejamos de insistir. La corrida no puede darse por completa.
            listado_cortado = listado_cortado or _agotado(corte)
        else:
            dias_ok.append(dia_str)

    if dias_fallidos and callback_estado:
        callback_estado(
            f"⚠️ El portal no respondió al listar {len(dias_fallidos)} día(s) "
            f"({paginas_perdidas} página(s) perdida(s), ~{paginas_perdidas * 50} "
            f"cotizaciones sin mirar): {', '.join(dias_fallidos)}", None)

    total = len(codigos_descargar)

    # Lo que ya no se puede cotizar se suelta. Son tres grupos:
    #
    #  · las que salieron de la ventana de retención (el portal ya no las lista);
    #  · las que tienen el 2do cierre vencido: se sabe por la fecha guardada, sin
    #    preguntarle nada al portal, así que también lo hace el barrido del día;
    #  · las que siguen en la ventana pero YA NO aparecen entre las abiertas del
    #    día en que se publicaron: como el listado se pide con `status=2`, no
    #    salir en él significa que cerró, se adjudicó, se canceló o quedó desierta.
    #
    # Lo tercero sólo vale para los días que se listaron sin fallos: borrar lo que
    # no se preguntó sería dar por cerrado lo que quizá sigue abierto. En la
    # práctica es cosa del barrido nocturno, que es el que recorre todos los días;
    # el del día sólo mira hoy y de ahí no poda nada (`dias_ok[1:]` queda vacío),
    # porque una cotización publicada al filo de la medianoche puede aparecer
    # listada en el día anterior, que no se recorrió.
    a_soltar = _codigos_fuera_de_ventana(df_cache, date_from) | codigos_cerrados
    a_soltar |= _codigos_segundo_cierre_vencido(unicos, pd.Timestamp.now())
    a_soltar |= _codigos_ausentes(unicos, set(dias_ok[1:]), vistos, codigos_descargar)

    if total == 0:
        if callback_estado:
            # «Sin novedades» sólo se puede decir si de verdad se pudo mirar. Con
            # el listado caído no se sabe si hay novedades o no, y darlo por bueno
            # es lo que hizo que la corrida de las 16:00 del 2026-08-12 acabara en
            # verde sin haber bajado nada.
            callback_estado(
                "No se pudo comprobar si hay novedades: el portal no respondió al listar."
                if dias_fallidos else "Sin novedades: no hay cotizaciones nuevas.", 100)
        if a_soltar:
            datos_nube.sincronizar_tabla(
                TABLA_NUBE, pd.DataFrame(columns=COLUMNAS_COMPLETAS), a_soltar)
        return {"completo": not listado_cortado, "descargadas": 0, "pendientes": 0,
                "dias_fallidos": list(dias_fallidos), "paginas_perdidas": paginas_perdidas,
                "fichas_fallidas": 0}

    # Fichas EN PARALELO por el mismo motivo (latencia). Solo se bajan las de los
    # códigos que faltan (nuevos o recién pasados a 2do llamado), así que en el
    # barrido nocturno de régimen son pocas; la 1ª corrida (caché vacío) es la pesada.
    # Soltar lo caducado ya: no depende de las fichas que faltan por bajar.
    if a_soltar:
        datos_nube.sincronizar_tabla(
            TABLA_NUBE, pd.DataFrame(columns=COLUMNAS_COMPLETAS), a_soltar)

    filas_lote, codigos_lote = [], []
    descargadas = 0            # fichas que de verdad llegaron y se guardan
    procesados = 0             # intentos resueltos (llegaran o no): sólo para el avance
    sin_ficha = []             # el portal no las sirvió: se reintentan al final

    def _guardar_lote():
        """Sube lo que se lleva descargado. Guardar sobre la marcha es lo que
        hace que una corrida cortada no se pierda: lo ya subido no se vuelve a
        pedir, así que la siguiente sigue donde quedó."""
        if not codigos_lote:
            return
        df = pd.DataFrame(filas_lote, columns=COLUMNAS_COMPLETAS)
        if not df.empty:
            df = df.drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"])
        datos_nube.sincronizar_tabla(TABLA_NUBE, df, list(codigos_lote))
        filas_lote.clear()
        codigos_lote.clear()

    def _bajar_fichas(codigos, hilos, etiqueta):
        """Baja las fichas de esos códigos y las va guardando por lotes.

        Devuelve (se_acabo_el_tiempo, los_que_no_trajeron_ficha).

        Un código sólo entra en el lote si SU FICHA LLEGÓ. Es importante: el lote
        se le pasa a `sincronizar_tabla` como lista de códigos a borrar, así que
        meter ahí uno fallido borraría de la tabla lo que ya había sin poner nada
        en su sitio — una cotización que pasa a 2do llamado y cuya ficha da 504
        desaparecería de la pantalla hasta el barrido siguiente."""
        nonlocal descargadas, procesados
        fallidos = []
        agotado = False
        executor = ThreadPoolExecutor(max_workers=hilos)
        try:
            futuros = {executor.submit(_obtener_ficha, c): c for c in codigos}
            for fut in _completadas_cancelable(futuros, cancel_event):
                codigo = futuros[fut]
                ficha = fut.result()
                if ficha:
                    codigos_lote.append(codigo)
                    filas_lote.extend(_procesar_ficha(ficha))
                    descargadas += 1
                else:
                    fallidos.append(codigo)
                procesados += 1

                if len(codigos_lote) >= LOTE_GUARDADO:
                    _guardar_lote()

                if callback_estado:
                    callback_estado(f"{etiqueta} {procesados} de {total}...",
                                    (procesados / total) * 100)

                # Se corta por tiempo ANTES de que lo mate el runner: así el lote en
                # curso se guarda y mañana se retoma en vez de perderlo todo.
                if corte and time.monotonic() > corte:
                    agotado = True
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return agotado, fallidos

    agotado, sin_ficha = _bajar_fichas(
        codigos_descargar, MAX_WORKERS, "Descargando cotizaciones")
    # Si el listado se cortó por tiempo, la corrida ya está incompleta aunque
    # todas las fichas de lo que sí se listó hayan llegado: quedan días sin mirar.
    incompleto = agotado or listado_cortado
    _guardar_lote()

    # Segunda pasada sobre las fichas que el portal no sirvió, con menos hilos.
    # Parte de esos fallos son del propio paralelismo y se recuperan pidiéndolas
    # con más calma; el resto son 504 del gateway (fichas que el portal tarda más
    # de ~29 s en generar) y ésas no hay forma de traerlas. Como no se guardaron,
    # tampoco quedan en caché: el barrido siguiente volverá a intentarlo.
    if sin_ficha and not incompleto:
        if callback_estado:
            callback_estado(
                f"Reintentando {len(sin_ficha)} ficha(s) que el portal no sirvió...", None)
        procesados -= len(sin_ficha)   # se vuelven a contar en la segunda pasada
        agotado, sin_ficha = _bajar_fichas(
            sin_ficha, MAX_WORKERS_RESCATE, "Reintentando cotizaciones")
        incompleto = agotado or listado_cortado
        _guardar_lote()

    if callback_estado:
        if incompleto:
            callback_estado(
                f"Se acabó el tiempo de esta corrida: {descargadas} de {total} "
                f"descargadas. El resto sigue en la próxima.", 100)
        elif sin_ficha:
            callback_estado(
                f"Descarga subida a la nube: {descargadas} de {total}. El portal no "
                f"sirvió la ficha de {len(sin_ficha)}; se reintentan en el próximo "
                f"barrido.", 100)
        else:
            callback_estado("¡Descarga completada y subida a la nube!", 100)

    return {"completo": not incompleto, "descargadas": descargadas,
            "pendientes": total - procesados,
            "dias_fallidos": list(dias_fallidos), "paginas_perdidas": paginas_perdidas,
            "fichas_fallidas": len(sin_ficha)}

