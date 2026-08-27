# -*- coding: utf-8 -*-
"""
clientes_api.py — Lo que publica un comprador, preguntándoselo al portal.

El módulo «Clientes» sigue a organismos compradores y enseña lo que tienen
VIVO: licitaciones publicadas y compras ágiles en 1er o 2do llamado, más las
compras ágiles que el portal da por cerradas pero cuyo segundo cierre todavía
no llega (ver `_segundo_llamado_vivo`).

Las dos mitades no se parecen en nada, y conviene saber por qué:

  · COMPRA ÁGIL es barata y exacta. El buscador acepta `org_code`, así que
    «todo lo de este comprador» es UNA consulta. El `org_code` no es el RUT: es
    un id interno que da el propio portal en `/filtros/organismo-comprador`.

  · LICITACIONES no se pueden pedir por comprador. La API oficial sólo lista
    por fecha o por estado, y su listado trae código, nombre, estado y cierre —
    no dice quién compra. Para saberlo hay que pedir la ficha de cada una:
    medido el 2026-08-27, 4.579 activas a 1,42 fichas/s con 3 hilos son 54
    minutos. Por eso el barrido mantiene un índice (`licitaciones_indice`) y
    sólo paga las nuevas de cada día, unas 1.000 (~12 min).

Todo lo de aquí es público y el mismo para todas las empresas; lo que es de
cada persona es a quién sigue, y eso vive en `core.marcas`.
"""
import time
import unicodedata

import requests

import compra_agil_api

# El buscador de Mercado Público, el mismo que ya usa Compra Ágil (comparte
# clave y cabeceras; ver `compra_agil_api`).
URL_BUSCADOR = "https://api.buscador.mercadopublico.cl"
URL_ORGANISMOS = URL_BUSCADOR + "/filtros/organismo-comprador"

# La API oficial de licitaciones. Necesita ticket, que es por empresa.
URL_LICITACIONES = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"

TIPO_COMPRA_AGIL = "compra_agil"
TIPO_LICITACION = "licitacion"

# Cuántos días atrás se miran las compras ágiles ya cerradas de un comprador
# buscando segundos llamados vivos.
#
# El segundo llamado abre al cerrar el primero y dura entre 1 y 5,5 días
# (medido). El primer cierre llega 1-4 días después de publicar. Diez días de
# ventana cubren el caso largo con margen y dejan la consulta en una página.
DIAS_CERRADAS = 10

# Hilos para las fichas de licitaciones. TRES, y no más: la API oficial limita
# por ticket y contesta 200 con un `Mensaje` y sin listado cuando te está
# frenando. Medido el 2026-08-27: con 3 hilos y espera creciente llegan las 40
# de 40; con 5 hilos se caen 33 de 36.
HILOS_LICITACIONES = 3

TIMEOUT = 40


def _texto_plano(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def normalizar_rut(texto) -> str:
    """'61.606.606-4' -> '616066064'. Es la llave con la que se cruza todo."""
    return "".join(c for c in str(texto or "").upper() if c.isdigit() or c == "K")


def formatear_rut(rut) -> str:
    """'616066064' -> '61.606.606-4'. Para mostrar, y para preguntarle al portal:
    el autocompletado por RUT sólo responde con los puntos puestos."""
    limpio = normalizar_rut(rut)
    if len(limpio) < 2:
        return limpio
    cuerpo, dv = limpio[:-1], limpio[-1]
    partes = []
    while len(cuerpo) > 3:
        partes.insert(0, cuerpo[-3:])
        cuerpo = cuerpo[:-3]
    partes.insert(0, cuerpo)
    return ".".join(partes) + "-" + dv


# ---------------------------------------------------------------------------
# Buscar compradores
# ---------------------------------------------------------------------------

def buscar_organismos(texto, limite=25):
    """Compradores que encajan con lo escrito. -> [{'id','nombre','rut'}]

    Es el autocompletado del propio portal, el que hay detrás del campo
    «Organismo comprador» de su buscador. Se usa en vez de armar el catálogo con
    nuestras tablas porque las nuestras sólo tienen a quien publicó algo en los
    últimos 30 días, y un cliente puede llevar meses sin comprar.

    Entiende dos formas y hay que mandarle cada una por su parámetro:
    `rut=61.606.606-4` CON puntos (sin ellos devuelve vacío) y `q=` para el
    nombre, que tolera trozos y no distingue tildes.
    """
    texto = str(texto or "").strip()
    if not texto:
        return []

    digitos = sum(1 for c in texto if c.isdigit())
    por_rut = digitos >= max(2, len(texto.replace(" ", "")) - 3)
    params = ({"rut": formatear_rut(texto)} if por_rut
              else {"q": texto, "org_class": 2})   # org_class 2 = comprador

    try:
        resp = requests.get(URL_ORGANISMOS, params=params,
                            headers=compra_agil_api._HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        datos = resp.json().get("payload") or []
    except Exception:
        return []

    # El portal devuelve el mismo organismo varias veces con nombres distintos
    # («HOSPITAL DE LA FAMILIA...» y «Hospital Santo Tomas de Limache»). Se deja
    # uno por RUT, el de nombre más largo, que es el oficial.
    por_rut_visto = {}
    for fila in datos:
        rut = normalizar_rut(fila.get("rut"))
        nombre = str(fila.get("text") or "").strip()
        if not rut or not nombre:
            continue
        actual = por_rut_visto.get(rut)
        if actual is None or len(nombre) > len(actual["nombre"]):
            por_rut_visto[rut] = {"id": fila.get("id"), "nombre": nombre, "rut": rut}

    orden = sorted(por_rut_visto.values(), key=lambda o: _texto_plano(o["nombre"]))
    return orden[:limite]


def org_code_de(rut):
    """El id interno con el que el buscador conoce a ese comprador, o None.

    Hace falta porque `org_code` NO acepta el RUT. Es una consulta más, así que
    quien recorra muchos compradores debería guardárselo.
    """
    for organismo in buscar_organismos(formatear_rut(rut)):
        if normalizar_rut(organismo["rut"]) == normalizar_rut(rut):
            return organismo["id"]
    return None


# ---------------------------------------------------------------------------
# Compra Ágil de un comprador
# ---------------------------------------------------------------------------

def _listar_compra_agil(org_code, status, date_from=None, date_to=None, pagina=1):
    params = {"page_number": pagina, "page_size": 50, "order_by": "recent",
              "org_code": org_code, "status": status}
    if date_from:
        params["date_from"] = date_from
        params["date_to"] = date_to or date_from
    for _ in range(3):
        try:
            resp = requests.get(compra_agil_api.API_URL, params=params,
                                headers=compra_agil_api._HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json().get("payload") or {}
            if resp.status_code in (403, 408, 429, 500, 502, 503, 504):
                time.sleep(3)
                continue
            return None
        except Exception:
            time.sleep(3)
    return None


def _todas_las_paginas(org_code, status, date_from=None, date_to=None, tope=40):
    """Los items de todas las páginas de esa consulta. -> (items, completo)"""
    primera = _listar_compra_agil(org_code, status, date_from, date_to)
    if primera is None:
        return [], False
    items = list(primera.get("resultados") or [])
    paginas = min(primera.get("pageCount", 1) or 1, tope)
    for pagina in range(2, paginas + 1):
        siguiente = _listar_compra_agil(org_code, status, date_from, date_to, pagina)
        if siguiente is None:
            return items, False
        items.extend(siguiente.get("resultados") or [])
    return items, True


def _segundo_llamado_vivo(ficha, ahora):
    """True si a esta compra ágil todavía se le puede ofertar en 2do llamado.

    El portal la da por 'Cerrada' en cuanto vence el primer cierre y NO siempre
    la reabre en su índice, aunque la ficha ya traiga la fecha del segundo
    (comprobado con 2170-456-COT26, que estuvo días así). Aquí sí se rescata
    —y no en el barrido general— porque son los clientes de alguien: mirar una
    docena de cerradas de un comprador es gratis, y hacerlo con las ~3.000
    diarias del país no lo era.
    """
    cierre2 = str(ficha.get("fecha_cierre_segundo_llamado") or "").strip()
    if not cierre2:
        return False
    try:
        return time.strptime(cierre2[:16], "%Y-%m-%d %H:%M") > ahora
    except Exception:
        return False


def compra_agil_de(org_code, hoy=None, log=print):
    """Lo que ese comprador tiene vivo en Compra Ágil. -> (filas, completo)

    `filas` va con las columnas de `clientes_seguimiento`, una por producto.

    Dos consultas y unas pocas fichas:
      · las PUBLICADAS (1er y 2do llamado), que son las que el portal admite;
      · las CERRADAS recientes, de las que se rescatan las que aún tienen el
        segundo cierre por delante.
    """
    from datetime import date, timedelta

    hoy = hoy or date.today()
    ahora = time.localtime()
    completo = True

    publicadas, ok = _todas_las_paginas(org_code, compra_agil_api.ESTADO_PUBLICADA)
    completo = completo and ok

    desde = (hoy - timedelta(days=DIAS_CERRADAS)).strftime("%Y-%m-%d")
    cerradas, ok = _todas_las_paginas(org_code, 3, desde, hoy.strftime("%Y-%m-%d"))
    completo = completo and ok

    filas = []
    for item in publicadas:
        fila = _fila_compra_agil(str(item.get("codigo", "")), ahora)
        if fila is None:
            completo = False
            continue
        filas.extend(fila)

    rescatadas = 0
    for item in cerradas:
        codigo = str(item.get("codigo", ""))
        ficha = compra_agil_api._obtener_ficha(codigo)
        if not ficha:
            completo = False
            continue
        if not _segundo_llamado_vivo(ficha, ahora):
            continue
        filas.extend(_filas_desde_ficha(ficha))
        rescatadas += 1

    if rescatadas:
        log(f"      {rescatadas} cerrada(s) rescatada(s) por tener 2do llamado vivo")
    return filas, completo


def _fila_compra_agil(codigo, ahora):
    ficha = compra_agil_api._obtener_ficha(codigo)
    if not ficha:
        return None
    return _filas_desde_ficha(ficha)


def _filas_desde_ficha(ficha):
    """Traduce la ficha del buscador a filas de `clientes_seguimiento`."""
    info = ficha.get("informacion_institucion") or {}
    cierre1 = ficha.get("fecha_cierre_primer_llamado") or ficha.get("fecha_cierre") or ""
    cierre2 = ficha.get("fecha_cierre_segundo_llamado") or ""
    llamado = compra_agil_api._LLAMADO_TEXTOS.get(ficha.get("estado_convocatoria"), "")

    # El cierre que le importa al vendedor es el que todavía no ha pasado. En un
    # segundo llamado ése es el segundo, aunque el portal siga enseñando el
    # primero en su listado.
    vigente = cierre1
    if cierre2 and _segundo_llamado_vivo(ficha, time.localtime()):
        vigente = cierre2
        llamado = compra_agil_api._LLAMADO_TEXTOS[2]

    base = {
        "RUT Organismo": normalizar_rut(info.get("rut_organismo_comprador")),
        "Organismo": info.get("organismo_comprador", ""),
        "Tipo": TIPO_COMPRA_AGIL,
        "Numero Adquisición": ficha.get("codigo", ""),
        "Nombre Adquisición": ficha.get("nombre", ""),
        "Fecha Publicación": ficha.get("fecha_publicacion", ""),
        "Fecha Cierre": vigente,
        "Fecha Cierre 1er Llamado": cierre1,
        "Fecha Cierre 2do Llamado": cierre2,
        "Llamado": llamado,
        "Estado": ficha.get("estado", ""),
    }

    productos = ficha.get("productos_solicitados") or []
    if not productos:
        productos = [{"nombre": "", "descripcion": ficha.get("descripcion", ""),
                      "cantidad": ""}]

    filas = []
    for prod in productos:
        nombre_prod = str(prod.get("nombre", "")).strip()
        desc = str(prod.get("descripcion", "")).strip().replace("_x000D_", "")
        fila = dict(base)
        fila.update({"Cantidad": str(prod.get("cantidad", "") or ""),
                     "Descripción Producto": desc or nombre_prod,
                     "Nombre Producto": nombre_prod})
        filas.append(fila)
    return filas


# ---------------------------------------------------------------------------
# Licitaciones
# ---------------------------------------------------------------------------

def licitaciones_activas(ticket):
    """Los códigos de todas las licitaciones publicadas ahora. -> [str]

    Una sola petición (4.579 el 2026-08-27, 0,3 s). No dice quién compra: para
    eso está `ficha_licitacion`.
    """
    try:
        resp = requests.get(URL_LICITACIONES, params={"estado": "activas", "ticket": ticket},
                            timeout=90)
        if resp.status_code != 200:
            return []
        return [str(i.get("CodigoExterno")) for i in (resp.json().get("Listado") or [])
                if i.get("CodigoExterno")]
    except Exception:
        return []


def ficha_licitacion(codigo, ticket, intentos=6):
    """El detalle de una licitación. -> dict de la API, o None.

    Insiste con espera creciente porque la API oficial contesta 200 con un
    `Mensaje` y sin listado cuando está limitando por ticket; eso no es un fallo
    definitivo, es «vuelve en un rato».
    """
    espera = 2
    for _ in range(intentos):
        try:
            datos = requests.get(URL_LICITACIONES,
                                 params={"codigo": codigo, "ticket": ticket},
                                 timeout=TIMEOUT).json()
            listado = datos.get("Listado") or []
            if listado:
                return listado[0]
        except Exception:
            pass
        time.sleep(espera)
        espera = min(espera * 2, 20)
    return None


def identidad_licitacion(detalle):
    """Lo que va al índice: de quién es y hasta cuándo. -> dict"""
    comprador = detalle.get("Comprador") or {}
    fechas = detalle.get("Fechas") or {}
    return {
        "numero_adquisicion": str(detalle.get("CodigoExterno") or ""),
        "rut_organismo": normalizar_rut(comprador.get("RutUnidad")),
        "organismo": comprador.get("NombreOrganismo", ""),
        "nombre_adquisicion": detalle.get("Nombre", ""),
        "fecha_publicacion": str(fechas.get("FechaPublicacion") or ""),
        "fecha_cierre": str(fechas.get("FechaCierre") or ""),
    }


def filas_licitacion(detalle):
    """Traduce el detalle de una licitación a filas de `clientes_seguimiento`."""
    identidad = identidad_licitacion(detalle)
    base = {
        "RUT Organismo": identidad["rut_organismo"],
        "Organismo": identidad["organismo"],
        "Tipo": TIPO_LICITACION,
        "Numero Adquisición": identidad["numero_adquisicion"],
        "Nombre Adquisición": identidad["nombre_adquisicion"],
        "Fecha Publicación": identidad["fecha_publicacion"],
        "Fecha Cierre": identidad["fecha_cierre"],
        # Una licitación no tiene llamados: las dos columnas se quedan vacías
        # para que la tabla del módulo pueda ser una sola con las dos fuentes.
        "Fecha Cierre 1er Llamado": "",
        "Fecha Cierre 2do Llamado": "",
        "Llamado": "",
        "Estado": "Publicada",
    }

    items = detalle.get("Items") or {}
    lista = items.get("Listado") if isinstance(items, dict) else None
    if isinstance(lista, dict):
        lista = [lista]
    if not lista:
        lista = [{}]

    vacios = {"", "sin descripcion", "sin descripción", "n/a", "none"}
    filas = []
    for item in lista:
        desc = str(item.get("Descripcion", "")).strip().replace("_x000D_", "")
        nombre_prod = str(item.get("NombreProducto", "")).strip().replace("_x000D_", "")
        fila = dict(base)
        fila.update({
            "Cantidad": str(item.get("Cantidad", "") or ""),
            "Descripción Producto": desc if desc.lower() not in vacios else nombre_prod,
            "Nombre Producto": nombre_prod,
        })
        filas.append(fila)
    return filas
