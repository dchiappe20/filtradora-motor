# -*- coding: utf-8 -*-
"""Prueba de la API oficial de Compra Ágil (v2, beta) con el ticket del motor.

Sólo mide: tiempos, códigos HTTP, cantidades y nombres de campos. No imprime el
ticket (GitHub además lo enmascara) ni guarda nada en Supabase.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("API_V2", "https://api2.mercadopublico.cl/v2/compra-agil")
TICKET = os.environ.get("MP_TICKET", "").strip()
if not TICKET:
    print("Falta MP_TICKET"); sys.exit(1)
H = {"ticket": TICKET, "Accept": "application/json"}


def pedir(nombre, url, params=None):
    t0 = time.time()
    try:
        r = requests.get(url, headers=H, params=params, timeout=60)
    except Exception as e:
        print(f"[{nombre}] ERROR de red tras {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
        return None
    dt = time.time() - t0
    print(f"[{nombre}] HTTP {r.status_code} en {dt:.1f}s · {len(r.content)/1024:.0f} KB · params={params}")
    if r.status_code != 200:
        print("   cuerpo:", r.text[:400].replace(TICKET, "***"))
        return None
    try:
        return r.json()
    except Exception:
        print("   no es JSON:", r.text[:300])
        return None
    finally:
        time.sleep(0.6)


def claves(obj, pref="", prof=0, out=None):
    out = out if out is not None else []
    if prof > 3:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(f"{pref}{k}:{type(v).__name__}")
            claves(v, pref + k + ".", prof + 1, out)
    elif isinstance(obj, list) and obj:
        claves(obj[0], pref + "[]", prof + 1, out)
    return out


def lista_de(js):
    if isinstance(js, dict) and isinstance(js.get("payload"), dict):
        js = js["payload"]
    if isinstance(js, list):
        return js
    if isinstance(js, dict):
        for k in ("resultados", "items", "data", "compras", "listado", "Listado"):
            if isinstance(js.get(k), list):
                return js[k]
        for v in js.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


ahora = datetime.now(timezone.utc)
print("Hora UTC:", ahora.isoformat())

# 1. Lo publicado en las últimas 24 h
js = None
for desde in ((ahora - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              (ahora - timedelta(days=1)).strftime("%Y-%m-%d")):
    js = pedir("publicadas 24h", BASE, {"estado": "publicada", "publicado_desde": desde,
                                         "tamano_pagina": 50, "numero_pagina": 1})
    if js is not None:
        break
if js is not None:
    print("   claves de la respuesta:", ", ".join(claves({k: v for k, v in js.items() if not isinstance(v, list)}))[:900] if isinstance(js, dict) else type(js))
    items = lista_de(js)
    print(f"   ítems en la página: {len(items)}")
    if items:
        print("   campos de un ítem del listado:")
        print("   " + "\n   ".join(claves(items[0])[:80]))
        print("   ejemplo (recortado):", json.dumps(items[0], ensure_ascii=False)[:1500])

# 2. Sólo lo que cambió en la última hora (sincronización incremental)
js2 = pedir("cambios 1h", BASE, {"ttl_cambio_ms": 3600000, "tamano_pagina": 50})
if js2 is not None:
    print("   ítems:", len(lista_de(js2)), "· resumen:", json.dumps({k: v for k, v in js2.items() if not isinstance(v, (list, dict))}, ensure_ascii=False)[:400] if isinstance(js2, dict) else "")
    pag = (js2.get("payload") or {}).get("paginacion") if isinstance(js2, dict) else None
    print("   payload:", json.dumps(js2.get("payload"), ensure_ascii=False)[:300] if isinstance(js2, dict) else "")
    if pag: print("   paginación:", json.dumps(pag, ensure_ascii=False)[:300])

js2b = pedir("cambios desde 1h (cambio_desde)", BASE, {"cambio_desde": (ahora - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "tamano_pagina": 50})
if js2b is not None:
    print("   ítems:", len(lista_de(js2b)), "· paginación:", json.dumps((js2b.get("payload") or {}).get("paginacion"), ensure_ascii=False)[:200])
if js is not None:
    print("   paginación publicadas 24h:", json.dumps((js.get("payload") or {}).get("paginacion"), ensure_ascii=False)[:200])
js_tot = pedir("total publicadas", BASE, {"estado": "publicada", "tamano_pagina": 1})
if js_tot is not None:
    print("   paginación total publicadas:", json.dumps((js_tot.get("payload") or {}).get("paginacion"), ensure_ascii=False)[:200])

# 3. Búsqueda por palabra
js3 = pedir("q=reactivo", BASE, {"q": "reactivo", "estado": "publicada", "tamano_pagina": 50})
if js3 is not None:
    print("   ítems:", len(lista_de(js3)))
    pag = (js3.get("payload") or {}).get("paginacion") if isinstance(js3, dict) else None
    if pag: print("   paginación:", json.dumps(pag, ensure_ascii=False)[:300])

# 4. Detalle de una cotización (productos, cierres, llamado)
codigo = None
for fuente in (js, js2, js3):
    for it in lista_de(fuente or {}):
        codigo = it.get("codigo") or it.get("Codigo") or it.get("id")
        if codigo and (it.get("convocatoria") or {}).get("estado_convocatoria") == 2: break
        if codigo: break
    if codigo: break
if codigo:
    det = pedir("detalle", f"{BASE}/{codigo}")
    if det is not None:
        print("   campos del detalle:")
        print("   " + "\n   ".join(claves(det)[:120]))
        print("   ejemplo (recortado):", json.dumps(det, ensure_ascii=False)[:2500])

# 5. Velocidad: 5 páginas seguidas
tiempos = []
for p in range(1, 6):
    t0 = time.time()
    r = pedir(f"página {p}", BASE, {"estado": "publicada", "tamano_pagina": 50, "numero_pagina": p})
    tiempos.append(round(time.time() - t0, 1))
print("Tiempos de 5 páginas (s):", tiempos)
