# -*- coding: utf-8 -*-
"""
empresa_config.py — Configuración que cambia según la empresa del usuario.

Por ahora, el ticket de la API de Mercado Público. Cada empresa necesita el
suyo: la API limita las peticiones POR TICKET, así que si dos empresas
compartieran uno, las descargas de una agotarían la cuota de la otra.

El ticket se busca en el entorno como `MP_TICKET_<EMPRESA>`, donde <EMPRESA>
sale del nombre en el registro central, normalizado:

    "Amilab"        -> MP_TICKET_AMILAB
    "utem"          -> MP_TICKET_UTEM
    "Clínica Ñuñoa" -> MP_TICKET_CLINICA_NUNOA

Si no hay uno específico se usa `MP_TICKET`, que sirve de valor común mientras
no se hayan pedido tickets separados.
"""
import os
import re
import unicodedata

import env_loader  # noqa: F401  — garantiza que el .env esté en os.environ
from helpers import leer_sesion


class TicketNoConfigurado(Exception):
    """No hay ticket de Mercado Público para la empresa actual."""


def slug_empresa(nombre) -> str:
    """Convierte el nombre de una empresa en sufijo de variable de entorno."""
    if not nombre:
        return ""

    texto = unicodedata.normalize("NFKD", str(nombre))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^A-Za-z0-9]+", "_", texto).strip("_")
    return texto.upper()


def nombre_empresa_actual() -> str:
    """Empresa de la sesión iniciada (o de la variable de entorno EMPRESA_NOMBRE
    cuando corre headless en GitHub Actions, donde no hay sesión)."""
    return os.environ.get("EMPRESA_NOMBRE", "").strip() or leer_sesion().get("empresa", "")


def variable_ticket(nombre_empresa=None) -> str:
    """Nombre de la variable de entorno que le toca a una empresa."""
    slug = slug_empresa(nombre_empresa if nombre_empresa is not None else nombre_empresa_actual())
    return f"MP_TICKET_{slug}" if slug else "MP_TICKET"


def ticket_mercado_publico(nombre_empresa=None) -> str:
    """Ticket de la empresa indicada (o la de la sesión).

    Lanza TicketNoConfigurado si no hay ninguno, en vez de devolver cadena
    vacía: una descarga con ticket vacío falla más adelante con un error de la
    API mucho más difícil de entender.
    """
    nombre = nombre_empresa if nombre_empresa is not None else nombre_empresa_actual()
    slug = slug_empresa(nombre)

    if slug:
        especifico = os.environ.get(f"MP_TICKET_{slug}", "").strip()
        if especifico:
            return especifico

    generico = os.environ.get("MP_TICKET", "").strip()
    if generico:
        return generico

    raise TicketNoConfigurado(
        f"Falta el ticket de Mercado Público para «{nombre or 'esta empresa'}». "
        f"Define {variable_ticket(nombre)} en el archivo .env "
        "(o MP_TICKET como valor común)."
    )


def hay_ticket(nombre_empresa=None) -> bool:
    try:
        ticket_mercado_publico(nombre_empresa)
        return True
    except TicketNoConfigurado:
        return False
