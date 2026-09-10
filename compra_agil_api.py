# compra_agil_api.py
import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading
import time
import os

import datos_nube
import historial_clientes

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

# Cuántas cotizaciones se piden por página del listado.
#
# ERA 50 —el máximo que acepta la API— y ése fue el problema. El portal tarda
# ~0,55 s por ítem, así que una página de 50 se va a ~28 s… y su propio API
# Gateway corta en seco a los 29 con `504 Endpoint request timed out`. O sea que
# el valor máximo estaba JUSTO encima del techo: cada página era una moneda al
# aire, y en cuanto el portal se cargaba un poco salían todas cruz.
#
# Medido contra el portal el 2026-09-09 a mediodía, misma consulta y mismas
# cabeceras que usa este módulo:
#
#   page_size=50 -> 6 de 8 peticiones con 504 (cortadas a 29,3-29,7 s)
#   page_size=40 -> 1 de 3 bien (un 502 y un 504)
#   page_size=30 -> 3 de 3 bien, 17-22 s
#   page_size=20 -> 5 de 5 bien, 13-18 s
#   page_size=10 -> 1 de 1 bien, 5,7 s
#
# No es paginación profunda: con 50 falla igual la página 1 que la 55, y con 20
# va bien hasta la 137. Es el tamaño de la página y nada más.
#
# 20 y no 30 porque el margen importa más que el número de peticiones: el trabajo
# total es el mismo (el portal sirve los ítems que sirve) y lo que se gana con
# páginas grandes son menos viajes, no menos segundos. Con 20 quedan ~14 s contra
# un techo de 29, sitio de sobra para que un mal día del portal no lo tire.
PAGINA_LISTADO = 20

# Suelo del troceo adaptativo (ver `_listar_pagina`). Por debajo de esto, partir
# la página deja de compensar: son el doble de viajes para ahorrar segundos que
# ya no sobran.
PAGINA_LISTADO_MINIMA = 5

# Techo de seguridad de páginas a recorrer al listar cotizaciones de UN día. El
# listado se recorre día por día (no pidiendo la ventana entera) porque el portal
# corta la paginación profunda (403/429) al pasar de ~100 páginas seguidas; por
# día son ~4.300 cotizaciones en el peor caso. El bucle se detiene solo al
# alcanzar el `pageCount` real del día, así que este número solo evita un bucle
# infinito si la API se comporta mal.
#
# Eran 300 cuando las páginas traían 50. Con 20 hacen falta 2,5 veces más para
# cubrir lo mismo (4.300 / 20 = 215), así que sube a 500 para conservar el margen
# que había: dejarlo en 300 habría convertido el techo de seguridad en un tope
# real que corta días cargados por la mitad.
MAX_PAGINAS_LISTADO = 500

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

# Cuánto se insiste, como mucho, con el listado de UN día.
#
# Hasta ahora sólo existía el corte global de la corrida (100 min en el barrido
# del día), y eso dejaba que un solo día se comiera el presupuesto de todo lo
# demás. Son DOS topes porque son dos situaciones que no se parecen en nada:
#
#   · SEGUNDOS_PRIMERA_PAGINA acota insistir con la página 1. Si no llega, el día
#     entero se pierde igual, así que repetirla no rescata nada. El 2026-09-09
#     fueron 12 min 28 s tirados ahí —medido: 6 llamadas x (3 intentos x 29 s +
#     21 s de esperas) + 5 x 20 s entre rondas—, y esos 12 minutos salieron del
#     presupuesto del seguimiento, que ese día acabó con 508 cotizaciones sin
#     revisar. Con 3 min caben tres ciclos completos de reintento (~57 s cada
#     uno) más el troceo adaptativo; si en eso no contestó, no va a contestar.
#
#   · SEGUNDOS_POR_DIA_LISTADO acota el día completo, rescates incluidos. Aquí
#     insistir SÍ rescata: son cotizaciones que se traen. Medido el 2026-09-09
#     listando el día entero con el tamaño nuevo: 149 páginas, 2.872
#     cotizaciones, 0 perdidas, 9 min 12 s — y eso recuperando 21 peticiones que
#     el portal devolvió con 504. O sea que un tope de 5 min habría cortado los
#     rescates de un día que acabó saliendo COMPLETO. 15 deja margen sobre ese
#     peor caso medido sin dejar que un día se lleve la corrida entera.
SEGUNDOS_PRIMERA_PAGINA = 180
SEGUNDOS_POR_DIA_LISTADO = 900

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


# Tabla en Supabase donde viven los datos (reemplaza al Excel local)
TABLA_NUBE = "compra_agil"

COLUMNAS_COMPLETAS = [
    "Numero Adquisición", "Nombre Adquisición", "Organismo", "RUT Organismo",
    "Fecha Publicación", "Fecha Cierre", "Fecha Cierre 1er Llamado", "Fecha Cierre 2do Llamado",
    "Llamado", "Cantidad", "Descripción Producto", "Nombre Producto",
]


# ---------------------------------------------------------------------------
# Por qué se cayó una petición
#
# EL AGUJERO QUE TAPA
#
# `_listar_pagina` tenía un `except Exception: time.sleep(espera)` y un `return
# None` sin decir nada. El 2026-09-09 el barrido del mediodía hizo 18 peticiones
# fallidas seguidas y terminó en rojo con «el portal no respondió al listar 1
# día(s)»: ni una sola línea del log decía que las 18 eran `504 Endpoint request
# timed out`. Averiguarlo costó ir a golpear el portal a mano.
#
# ESTO NO SE LE ENSEÑA AL CLIENTE
#
# Todo lo de aquí sale por `print`, o sea al log de GitHub Actions y a nada más.
# NO pasa por `callback_estado`, que es el canal que acaba en `descarga_estado` y
# de ahí en la barra de la app. El cliente sigue viendo «Descargando N
# cotizaciones...»; que el portal devuelva 504 es cosa nuestra, no suya.
#
# El contador es de módulo y con candado porque las páginas se piden desde varios
# hilos (`MAX_WORKERS`). Se vacía al empezar cada día, y los días se recorren de
# uno en uno, así que el resumen que se imprime es siempre el del día que acaba.
# ---------------------------------------------------------------------------

_fallos_lock = threading.Lock()
_fallos = {}


def _anotar_fallo(causa):
    with _fallos_lock:
        _fallos[causa] = _fallos.get(causa, 0) + 1


def _vaciar_fallos():
    with _fallos_lock:
        _fallos.clear()


def _resumen_fallos():
    """«504 Endpoint request timed out x16, sin respuesta en 60s x2», o ''."""
    with _fallos_lock:
        if not _fallos:
            return ""
        partes = sorted(_fallos.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{causa} x{veces}" for causa, veces in partes)


def _diag(mensaje):
    """Al log de la corrida y a ningún otro sitio. Ver el bloque de arriba."""
    print(f"[portal] {mensaje}", flush=True)


def _es_de_tamano(causa):
    """¿El fallo huele a «esta página era demasiado grande para el gateway»?

    Sólo entonces tiene sentido partirla: un 403 o un 429 son el portal diciendo
    que le estamos pidiendo DEMASIADO, y trocear haría el doble de viajes, que es
    exactamente lo contrario de lo que hace falta."""
    return ("504" in causa) or ("408" in causa) or causa.startswith("sin respuesta")


def _listar_pagina(date_from, date_to, page_number, page_size=None):
    # page_size está topado en 50 por la API (valores mayores devuelven 400), por
    # eso recorrer la ventana completa exige muchas páginas.
    #
    # `status=2` es el filtro de la API para 'Publicada' (el nombre no es obvio:
    # `estado`, `id_estado` y compañía los ignora en silencio y devuelve todo).
    # Pedir sólo las abiertas recorta muchísimo: en un día de hace 8 jornadas
    # baja de 77 páginas a 2 (−97%), porque casi todo lo de esos días ya cerró.
    # Comprobado que no se pierde ninguna abierta al filtrar.
    page_size = page_size or PAGINA_LISTADO
    params = {
        "page_number": page_number,
        "page_size": page_size,
        "order_by": "recent",
        "status": ESTADO_PUBLICADA,
        "date_from": date_from,
        "date_to": date_to,
    }
    espera = 3
    causa = "sin intentos"
    for _ in range(INTENTOS_LISTADO):
        try:
            resp = requests.get(API_URL, params=params, headers=_HEADERS,
                                timeout=TIMEOUT_LISTADO)
            if resp.status_code == 200:
                return resp.json().get("payload") or {}
            # El texto del cuerpo entra en la causa porque es donde el portal
            # dice qué le pasó: los 504 del gateway llegan con
            # `{"message": "Endpoint request timed out"}`, y saber eso es la
            # diferencia entre «el portal falla» y «le estamos pidiendo páginas
            # que no le caben».
            causa = f"HTTP {resp.status_code} {_motivo(resp)}".strip()
            # 403/429: el portal limita la paginación rápida/profunda. 5xx/408:
            # error transitorio. En ambos casos reintentamos con espera creciente.
            if resp.status_code in (403, 408, 429, 500, 502, 503, 504):
                _anotar_fallo(causa)
                time.sleep(espera)
                espera = min(espera * 2, 30)
                continue
            # Otros (p. ej. 400 por parámetros) no se arreglan reintentando.
            _anotar_fallo(f"{causa} (no se reintenta)")
            return None
        except requests.Timeout:
            causa = f"sin respuesta en {TIMEOUT_LISTADO}s"
            _anotar_fallo(causa)
            time.sleep(espera)
            espera = min(espera * 2, 30)
        except Exception as e:
            causa = type(e).__name__
            _anotar_fallo(causa)
            time.sleep(espera)
            espera = min(espera * 2, 30)

    # ---- Troceo adaptativo -------------------------------------------------
    #
    # Agotados los intentos. Si lo que falló huele a página demasiado grande, no
    # tiene ningún sentido volver a pedir LA MISMA: ya sabemos que no cabe en el
    # gateway. Se pide partida en dos, que es lo que sí cabe.
    #
    # La equivalencia es exacta porque la API pagina por desplazamiento: la
    # página N de tamaño S son los mismos ítems que las páginas 2N-1 y 2N de
    # tamaño S/2. Por eso sólo se parte con tamaños pares.
    if (_es_de_tamano(causa) and page_size > PAGINA_LISTADO_MINIMA
            and page_size % 2 == 0):
        return _listar_pagina_partida(date_from, date_to, page_number, page_size)
    return None


def _motivo(resp):
    """El `message` que trae el error, recortado. Vacío si no hay nada legible."""
    try:
        texto = (resp.json() or {}).get("message") or ""
    except Exception:
        texto = (resp.text or "")[:80]
    return str(texto).strip()[:80]


def _listar_pagina_partida(date_from, date_to, page_number, page_size):
    """La misma página, pedida en dos mitades.

    Devuelve un payload con la MISMA forma que devolvería la petición entera, y
    ahí está el detalle que importa: `pageCount` se recalcula a la escala que
    pidió quien llama. El del trozo es el doble (hay el doble de páginas cuando
    son la mitad de grandes), y devolverlo tal cual haría que `_listar_dia`
    recorriera el doble de páginas, la mitad de ellas vacías.
    """
    mitad = page_size // 2
    primera = page_number * 2 - 1

    trozos = []
    for p in (primera, primera + 1):
        trozo = _listar_pagina(date_from, date_to, p, mitad)
        # La segunda mitad puede venir vacía si la página era la última del día,
        # y eso es correcto; lo que no vale es que falle, porque entonces
        # faltarían cotizaciones sin que nadie se entere.
        if trozo is None:
            return None
        trozos.append(trozo)

    resultados = []
    for t in trozos:
        resultados.extend(t.get("resultados") or [])

    total = next((t.get("resultCount") for t in trozos
                  if isinstance(t.get("resultCount"), (int, float))), None)
    if total is None:
        paginas = trozos[0].get("pageCount")
    else:
        paginas = -(-int(total) // page_size)   # techo de la división

    _diag(f"página {page_number} de {page_size} no cabía en el portal: "
          f"servida en 2 de {mitad} ({len(resultados)} cotizaciones).")

    return {
        "resultCount": total,
        "pageCount": paginas,
        "page": page_number,
        "pageSize": page_size,
        "resultados": resultados,
    }


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
                             codigos_cerrados=None, estados_vistos=None):
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

        estado = str(item.get("estado", "")).strip().lower()
        if estado != ESTADO_ABIERTA:
            if codigos_cerrados is not None:
                codigos_cerrados.add(codigo)
            # El estado de verdad, con el nombre que le pone el portal. Es el
            # único sitio donde se sabe: en cuanto la cotización sale de lo
            # publicado deja de aparecer en el listado (`status=2`) y ya no hay
            # a quién preguntarle si cerró, se adjudicó o quedó desierta.
            if estados_vistos is not None and estado:
                estados_vistos[codigo] = estado
            continue

        llamado = _LLAMADO_TEXTOS.get(item.get("estado_convocatoria"), "")
        cacheado = llamado_por_codigo.get(codigo)
        if cacheado is None or (llamado and cacheado != llamado):
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
    16:00.

    Este día tiene además su propio tope de tiempo (`SEGUNDOS_POR_DIA_LISTADO`)
    por encima del `corte` de la corrida: sin él, un día que no responde se comía
    el presupuesto de todo lo demás insistiendo con la misma petición."""
    # El día empieza con el contador de fallos limpio para que el resumen que se
    # imprima al final sea el suyo y no arrastre el del día anterior.
    _vaciar_fallos()

    ahora = time.monotonic()
    limite_dia = ahora + SEGUNDOS_POR_DIA_LISTADO
    corte_dia = limite_dia if corte is None else min(corte, limite_dia)
    # La página 1 se rinde antes que el resto: ver el comentario de las dos
    # constantes. Insistir con ella no rescata nada, insistir con las demás sí.
    corte_p1 = min(corte_dia, ahora + SEGUNDOS_PRIMERA_PAGINA)

    payload = _listar_pagina(dia_str, dia_str, 1)
    for ronda in range(RONDAS_RESCATE):
        if payload or _agotado(corte_p1):
            break
        if callback_estado:
            callback_estado(
                f"El portal no contesta con el día {dia_str}: reintentando "
                f"({ronda + 1}/{RONDAS_RESCATE})...", None)
        time.sleep(ESPERA_RESCATE)
        payload = _listar_pagina(dia_str, dia_str, 1)

    if not payload:
        # El único sitio donde queda escrito POR QUÉ se perdió el día entero.
        _diag(f"día {dia_str}: la página 1 nunca llegó — {_resumen_fallos() or 'sin detalle'}")
        return 0, False

    al_llegar(payload)
    page_count = min(payload.get("pageCount", 1), MAX_PAGINAS_LISTADO)
    pendientes = list(range(2, page_count + 1))

    # Páginas 2..page_count EN PARALELO (la latencia de la API domina, no la CPU),
    # y otras tantas rondas sobre las que se caigan.
    for ronda in range(RONDAS_RESCATE + 1):
        if not pendientes or (ronda and _agotado(corte_dia)):
            break
        if ronda:
            if callback_estado:
                callback_estado(
                    f"Reintentando {len(pendientes)} página(s) que el portal dejó "
                    f"caer del día {dia_str} ({ronda}/{RONDAS_RESCATE})...", None)
            time.sleep(ESPERA_RESCATE)
        pendientes = _pedir_paginas(dia_str, pendientes, cancel_event, al_llegar)

    resumen = _resumen_fallos()
    if resumen:
        _diag(f"día {dia_str}: {page_count} página(s) de {PAGINA_LISTADO}, "
              f"{len(pendientes)} sin recuperar — {resumen}")
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
      · vencido el 1er cierre, la pantalla la muestra como «Por confirmar»
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

    `callback_estado(mensaje, progreso, publico)`: `mensaje` es el detalle
    técnico para el log y `publico` lo poco que tiene sentido enseñarle al
    cliente (ver `_avisar`). Un callback de dos parámetros sigue funcionando.

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

    def _avisar(mensaje, progreso=None, publico=None):
        """Deja constancia de dónde va el barrido, en dos canales distintos.

        `mensaje` es para el log de la corrida, con todo el detalle: días,
        reintentos, páginas que se cayeron. `publico` es lo que ve el cliente en
        la barra de la app, y va aparte a propósito — «Reintentando 14 ficha(s)
        que el portal no sirvió» es ruido de la máquina para quien sólo quiere
        saber si sus cotizaciones están al día. Sin `publico`, la app sigue
        mostrando lo último que se le dijo.
        """
        if callback_estado:
            callback_estado(mensaje, progreso, publico)

    _avisar("Leyendo datos existentes en la nube...", 0,
            "Buscando cotizaciones nuevas...")

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
    # {código: estado} para las que el listado sí dijo a qué pasaron ('cerrada',
    # 'adjudicada', 'desierta'…). Es lo que se guarda en el historial del cliente
    # en vez de suponer; ver `historial_clientes.ESTADO_POR_DEFECTO`.
    estados_vistos = {}
    dias_fallidos = []
    dias_ok = []               # listados enteros y sin fallos
    paginas_perdidas = 0       # páginas que no contestaron ni tras el rescate
    listado_cortado = False    # se acabó el tiempo antes de mirar todos los días

    def _publico_listado():
        """Lo que ve el cliente mientras se recorre el listado: cuántas
        cotizaciones se le van a bajar. El día que se está mirando y las
        páginas que se cayeron son cosa del log."""
        pendientes = len(codigos_descargar)
        return (f"Descargando {pendientes} cotizaciones..." if pendientes
                else "Buscando cotizaciones nuevas...")

    _avisar(f"Revisando qué hay de nuevo ({len(dias)} día(s), "
            f"desde el {dias[0].strftime('%d-%m')})...", 0, _publico_listado())

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
        _avisar(f"Revisando cotizaciones... día {dia_str} "
                f"({idx_dia}/{len(dias)}) ({len(codigos_descargar)} por descargar)",
                0, _publico_listado())

        def _al_llegar(payload, dia_str=dia_str, idx_dia=idx_dia):
            _procesar_pagina_listado(payload, vistos, llamado_por_codigo,
                                     codigos_descargar, codigos_cerrados,
                                     estados_vistos)
            _avisar(f"Revisando cotizaciones... día {dia_str} "
                    f"({idx_dia}/{len(dias)}) ({len(codigos_descargar)} por descargar)",
                    0, _publico_listado())

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

    if dias_fallidos:
        _avisar(f"⚠️ El portal no respondió al listar {len(dias_fallidos)} día(s) "
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

    # Antes de soltarlas, las de un comprador que alguien sigue se copian a su
    # historial. Es la última vez que se tienen esos datos: en cuanto salgan de
    # `compra_agil` no hay de dónde volver a sacarlos, porque el listado se pide
    # con `status=2` y el portal ya no las devuelve.
    #
    # Va aquí y no dentro de `sincronizar_tabla` a propósito: esa función es
    # genérica y no tiene por qué saber qué es un cliente favorito. Aquí sí se
    # sabe, y además se tiene `estados_vistos`, que es lo único que dice a qué
    # estado pasó cada una en vez de suponerlo.
    if a_soltar:
        historial_clientes.archivar(df_cache, a_soltar,
                                    historial_clientes.TIPO_COMPRA_AGIL,
                                    estados=estados_vistos)

    if total == 0:
        # «Sin novedades» sólo se puede decir si de verdad se pudo mirar. Con
        # el listado caído no se sabe si hay novedades o no, y darlo por bueno
        # es lo que hizo que la corrida de las 16:00 del 2026-08-12 acabara en
        # verde sin haber bajado nada.
        _avisar(
            "No se pudo comprobar si hay novedades: el portal no respondió al listar."
            if dias_fallidos else "Sin novedades: no hay cotizaciones nuevas.", 100,
            None if dias_fallidos else "Sin cotizaciones nuevas.")
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
    tope_avance = 0            # el mismo número, pero sin retrocesos (ver abajo)
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
        nonlocal descargadas, procesados, tope_avance
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

                # El cliente ve siempre lo mismo, vaya por la primera pasada o
                # por el rescate: para él son sus cotizaciones, no dos vueltas.
                #
                # Y no retrocede. Al entrar el rescate, `procesados` vuelve
                # atrás a propósito —las fichas que fallaron se recuentan— y la
                # barra bajaba de 60% a 54% a la vista del cliente. El número
                # exacto sigue en el log; a la pantalla va el máximo alcanzado.
                tope_avance = max(tope_avance, procesados)
                _avisar(f"{etiqueta} {procesados} de {total}...",
                        (tope_avance / total) * 100,
                        f"Revisando cotizaciones {tope_avance} de {total}...")

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
        _avisar(f"Reintentando {len(sin_ficha)} ficha(s) que el portal no sirvió...",
                None)
        procesados -= len(sin_ficha)   # se vuelven a contar en la segunda pasada
        agotado, sin_ficha = _bajar_fichas(
            sin_ficha, MAX_WORKERS_RESCATE, "Reintentando cotizaciones")
        incompleto = agotado or listado_cortado
        _guardar_lote()

    # Sin texto público: lo que viene a continuación es el filtrado, y es él
    # quien anuncia en qué anda.
    if incompleto:
        _avisar(f"Se acabó el tiempo de esta corrida: {descargadas} de {total} "
                f"descargadas. El resto sigue en la próxima.", 100)
    elif sin_ficha:
        _avisar(f"Descarga subida a la nube: {descargadas} de {total}. El portal no "
                f"sirvió la ficha de {len(sin_ficha)}; se reintentan en el próximo "
                f"barrido.", 100)
    else:
        _avisar("¡Descarga completada y subida a la nube!", 100)

    return {"completo": not incompleto, "descargadas": descargadas,
            "pendientes": total - procesados,
            "dias_fallidos": list(dias_fallidos), "paginas_perdidas": paginas_perdidas,
            "fichas_fallidas": len(sin_ficha)}

