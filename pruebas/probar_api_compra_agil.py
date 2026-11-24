# -*- coding: utf-8 -*-
"""Prueba de la API oficial de Compra Ágil (v2, beta): sincronización por cambios."""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

BASE = "https://api2.mercadopublico.cl/v2/compra-agil"
TICKET = os.environ.get("MP_TICKET", "").strip()
if not TICKET: sys.exit("Falta MP_TICKET")
H = {"ticket": TICKET, "Accept": "application/json"}

def pedir(nombre, params):
    t0 = time.time()
    r = requests.get(BASE, headers=H, params=params, timeout=60)
    time.sleep(0.6)
    try: js = r.json()
    except Exception: js = {}
    pay = js.get("payload") or {}
    items = pay.get("items") or []
    pag = pay.get("paginacion") or {}
    print(f"[{nombre}] HTTP {r.status_code} {time.time()-t0:.1f}s total={pag.get('total_resultados')} params={params}")
    if r.status_code != 200: print("   ", json.dumps(js.get("errors"), ensure_ascii=False)[:300])
    for it in items[:3]:
        f = it.get("fechas") or {}
        print(f"    {it.get('codigo')} cambio={f.get('fecha_ultimo_cambio')} pub={f.get('fecha_publicacion')} llamado={(it.get('convocatoria') or {}).get('estado_convocatoria')} estado={(it.get('estado') or {}).get('codigo')}")
    return items

ahora = datetime.now(timezone.utc); cl = ahora.astimezone(ZoneInfo("America/Santiago"))
print("UTC", ahora.isoformat(), "· Chile", cl.isoformat())
pedir("orden por cambio, sin filtros", {"ordenar_por": "FechaUltimaModificacion", "tamano_pagina": 10})
pedir("orden por publicación", {"ordenar_por": "FechaPublicacion", "tamano_pagina": 10})
for etiqueta, v in [("UTC Z", (ahora - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ("Chile sin zona", (cl - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")),
                    ("Chile con offset", (cl - timedelta(hours=2)).isoformat(timespec="seconds")),
                    ("solo fecha", cl.strftime("%Y-%m-%d"))]:
    pedir(f"cambio_desde {etiqueta}", {"cambio_desde": v, "tamano_pagina": 10})
for ms in (3600000, 6*3600000, 24*3600000):
    pedir(f"ttl {ms//3600000}h", {"ttl_cambio_ms": ms, "tamano_pagina": 10})
pedir("publicado_desde hoy (fecha)", {"publicado_desde": cl.strftime("%Y-%m-%d"), "tamano_pagina": 10})
pedir("publicado_desde 2h Chile", {"publicado_desde": (cl - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"), "tamano_pagina": 10})
pedir("estado publicada", {"estado": "publicada", "tamano_pagina": 10})
pedir("estado cerrada", {"estado": "cerrada", "tamano_pagina": 10})
