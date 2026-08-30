# -*- coding: utf-8 -*-
"""
clientes_api.py — Buscar organismos compradores en Mercado Público.

El módulo «Clientes» sigue a compradores, y para eso hay que poder encontrarlos
por nombre o por RUT. Se le pregunta al portal y no a nuestras tablas: su
autocompletado —el que hay detrás del campo «Organismo comprador» de su
buscador— conoce a todos los organismos del Estado, mientras que las nuestras
sólo tienen a quien publicó algo en los últimos 30 días.

Lo que cada comprador PUBLICA no se pide aquí: sale de cruzar por RUT lo que la
app ya tiene descargado (`datos_nube.publicaciones_de_clientes`). Hubo un
barrido que iba al portal comprador por comprador —con `org_code` para Compra
Ágil y 4.579 fichas de licitación para saber de quién era cada una— y se quitó:
gastaba minutos de servidor para acabar enseñando lo mismo.
"""
import unicodedata

import requests

import compra_agil_api

# El buscador de Mercado Público, el mismo que ya usa Compra Ágil (comparte
# clave y cabeceras; ver `compra_agil_api`).
URL_BUSCADOR = "https://api.buscador.mercadopublico.cl"
URL_ORGANISMOS = URL_BUSCADOR + "/filtros/organismo-comprador"

TIPO_COMPRA_AGIL = "compra_agil"
TIPO_LICITACION = "licitacion"

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
