import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import os

import datos_nube
from empresa_config import ticket_mercado_publico

URL_API = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"

# Tabla en Supabase donde viven los datos (reemplaza al Excel local)
TABLA_NUBE = "licitaciones"

# Peticiones en paralelo al bajar el detalle de cada licitación. A diferencia del
# buscador de Compra Ágil, esta es la API OFICIAL (api.mercadopublico.cl) y limita
# fuerte por ticket: se midió que 6 hilos pierden ~25% de licitaciones, pero 3 hilos
# con backoff exponencial no pierden ninguna. NO subir de 3.
MAX_WORKERS_LICIT = 3

def _get_appdata_dir() -> str:
    path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Amilab")
    os.makedirs(path, exist_ok=True)
    return path
 
def estado_licitacion(codigo):
    """Estado actual de una licitación, para el barrido de foros:
        'publicada'  -> aún no cierra (sin foro de aclaración todavía)
        'cerrada'    -> en evaluación (el detalle viene vacío) -> hay que revisar el foro
        'resuelta'   -> adjudicada/desierta/revocada/etc. -> egresa de la lista
        'desconocido'-> no se pudo determinar (no se toca la lista por las dudas)
    """
    espera = 2
    for _ in range(4):
        try:
            resp = requests.get(URL_API,
                                params={"codigo": codigo, "ticket": ticket_mercado_publico()},
                                timeout=15)
            if resp.status_code == 200:
                datos = resp.json()
                if "Mensaje" in datos and not datos.get("Listado"):
                    time.sleep(espera); espera = min(espera * 2, 20); continue
                listado = datos.get("Listado") or []
                if not listado:
                    return "desconocido"
                ce = listado[0].get("CodigoEstado")
                if ce == 5:
                    return "publicada"
                if ce in (6, None):       # 6 = Cerrada (en evaluación); None = detalle oculto
                    return "cerrada"
                if ce in (7, 8, 15, 18, 19, 20):  # desierta/adjudicada/revocada/... ya salió
                    return "resuelta"
                return "desconocido"      # estado inesperado: no egresar por las dudas
            elif resp.status_code in [429, 500, 502, 503, 504]:
                time.sleep(espera); espera = min(espera * 2, 20)
            else:
                return "desconocido"
        except Exception:
            time.sleep(espera); espera = min(espera * 2, 20)
    return "desconocido"


def get_fechas_rango(fecha_inicio_str, fecha_fin_str):
    f_inicio = datetime.strptime(fecha_inicio_str, "%d-%m-%Y")
    f_fin = datetime.strptime(fecha_fin_str, "%d-%m-%Y")
    fechas = []
    while f_inicio <= f_fin:
        fechas.append(f_inicio.strftime("%d-%m-%Y"))
        f_inicio += timedelta(days=1)
    return fechas
 
def comprador_de(codigo):
    """Organismo que publicó esa licitación. -> (rut, nombre, error)

    Una sola consulta, para dar de alta a un cliente a partir del ID de una de
    sus licitaciones (ver `compradores.registrar_desde_codigo`). No guarda nada:
    lo que interesa es quién compra, no la licitación.
    """
    try:
        resp = requests.get(URL_API,
                            params={"codigo": codigo, "ticket": ticket_mercado_publico()},
                            timeout=30)
    except Exception as e:
        return "", "", f"No se pudo consultar Mercado Público: {e}"

    if resp.status_code != 200:
        return "", "", f"Mercado Público respondió {resp.status_code}. Inténtalo de nuevo."

    try:
        datos = resp.json()
    except Exception:
        return "", "", "Mercado Público devolvió una respuesta ilegible."

    listado = datos.get("Listado") or []
    if not listado:
        # La API contesta 200 con un 'Mensaje' cuando el código no existe o
        # cuando el ticket está saturado; se distinguen mirando si hay listado.
        aviso = str(datos.get("Mensaje") or "").strip()
        return "", "", (aviso or f"Mercado Público no encontró la licitación {codigo}.")

    comprador = (listado[0].get("Comprador") or {})
    return (comprador.get("RutUnidad", ""),
            comprador.get("NombreOrganismo", ""), "")


def procesar_licitacion(codigo, fecha_exacta_str):
    filas = []
    # Backoff exponencial y varios reintentos: la API oficial limita fuerte por
    # ticket ("demasiadas consultas"), sobre todo con varias peticiones a la vez.
    # Se midió que con esperas crecientes y 3 hilos NO se pierden licitaciones.
    espera = 2
    for _ in range(6):
        try:
            resp = requests.get(URL_API,
                                params={"codigo": codigo, "ticket": ticket_mercado_publico()},
                                timeout=15)
            if resp.status_code == 200:
                datos = resp.json()
                if "Mensaje" in datos and not datos.get("Listado"):
                    time.sleep(espera)
                    espera = min(espera * 2, 20)
                    continue
                    
                detalles = datos.get("Listado", [])
                if not detalles: return filas, "VACIO"
                    
                detalle = detalles[0]
                fechas = detalle.get("Fechas", {})
                
                f_pub = str(fechas.get("FechaPublicacion", ""))
                if not f_pub.startswith(fecha_exacta_str):
                    return filas, "OTRA_FECHA"
                
                info_comprador = detalle.get("Comprador", {}) or {}
                comprador = info_comprador.get("NombreOrganismo", "Desconocido")
                # `RutUnidad` es el RUT del organismo comprador. Se guarda para
                # poder decir de quién es cada licitación sin comparar nombres,
                # que vienen con mayúsculas, tildes y espacios a su antojo (lo
                # usa el módulo de clientes seguidos).
                rut_comprador = info_comprador.get("RutUnidad", "")
                f_cierre = fechas.get("FechaCierre", "")
                
                items_data = detalle.get("Items")
                if isinstance(items_data, dict):
                    lista_items = items_data.get("Listado")
                    if isinstance(lista_items, dict): lista_items = [lista_items] 
                    elif not lista_items: lista_items = []
                        
                    _vacios = {"", "sin descripcion", "sin descripción", "n/a", "none"}
                    for item in lista_items:
                        desc_real = str(item.get('Descripcion', '')).strip().replace("_x000D_", "")
                        nombre_generico = str(item.get('NombreProducto', '')).strip().replace("_x000D_", "")

                        if desc_real and desc_real.lower() not in _vacios:
                            desc_final = desc_real
                        else:
                            desc_final = nombre_generico

                        # Campo solo para filtrado: NombreProducto + Descripcion concatenados
                        partes_filtro = [p for p in (nombre_generico, desc_real) if p and p.lower() not in _vacios]
                        texto_filtrado = " ".join(partes_filtro)

                        filas.append({
                            "Numero Adquisición": codigo,
                            "Nombre Adquisición": detalle.get("Nombre", ""),
                            "Organismo": comprador,
                            "RUT Organismo": rut_comprador,
                            "Fecha Publicación": f_pub,
                            "Fecha Cierre": f_cierre,
                            "Cantidad": item.get("Cantidad", 0),
                            "Descripción Producto": desc_final,
                            "Texto Filtrado": texto_filtrado,
                        })
                return filas, "OK"
                
            elif resp.status_code in [429, 500, 502, 503, 504]:
                time.sleep(espera)
                espera = min(espera * 2, 20)
            else:
                return filas, "ERROR_API"
                
        except Exception:
            time.sleep(espera)
            espera = min(espera * 2, 20)

    return filas, "FALLO_TIMEOUT"
 
def descargar_un_dia(fecha_str_ddmmyyyy, codigos_cacheados, callback_estado=None):
    fecha_busqueda = fecha_str_ddmmyyyy.replace("-", "")
    fecha_exacta_str = f"{fecha_busqueda[4:8]}-{fecha_busqueda[2:4]}-{fecha_busqueda[0:2]}"
    
    if callback_estado: callback_estado(f"[{fecha_str_ddmmyyyy}] Preparando...", 0)
    
    intentos_lista = 0
    licitaciones_crudas = None
    
    while intentos_lista < 3:
        try:
            resp = requests.get(URL_API,
                                params={"fecha": fecha_busqueda, "ticket": ticket_mercado_publico()},
                                timeout=15)
            if resp.status_code == 200:
                datos = resp.json()
                licitaciones_crudas = datos.get("Listado", [])
                break 
            elif resp.status_code in [429, 500, 502, 503, 504]:
                time.sleep(2)
                intentos_lista += 1
            else:
                return pd.DataFrame(), False
        except Exception:
            time.sleep(2)
            intentos_lista += 1
            
    if licitaciones_crudas is None:
        return pd.DataFrame(), False
 
    licitaciones_publicadas = [lic for lic in licitaciones_crudas if lic.get("CodigoEstado") == 5]
    
    if not licitaciones_publicadas:
        if callback_estado: callback_estado(f"[{fecha_str_ddmmyyyy}] Sin datos nuevos", 100)
        return pd.DataFrame(), True 
        
    codigos_del_dia = [lic.get("CodigoExterno") for lic in licitaciones_publicadas if lic.get("CodigoExterno")]
    codigos_faltantes = [c for c in codigos_del_dia if str(c) not in codigos_cacheados]
    
    total = len(codigos_faltantes)
    
    if not codigos_faltantes:
        if callback_estado: callback_estado(f"[{fecha_str_ddmmyyyy}] Completado", 100)
        return pd.DataFrame(), True 
        
    if callback_estado: 
        callback_estado(f"[{fecha_str_ddmmyyyy}] 0 de {total}", 0)
 
    filas_para_tabla = []
    stats = {"OK": 0}
    completados = 0

    # Detalle de cada licitación EN PARALELO (3 hilos; ver MAX_WORKERS_LICIT). Las
    # peticiones corren en workers; el acumulado de filas se hace en este hilo.
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_LICIT)
    try:
        futuros = {executor.submit(procesar_licitacion, codigo, fecha_exacta_str): codigo
                   for codigo in codigos_faltantes}
        for fut in as_completed(futuros):
            filas_licitacion, estado = fut.result()
            filas_para_tabla.extend(filas_licitacion)
            completados += 1
            if estado == "OK":
                stats["OK"] += 1
            if callback_estado:
                progreso = (completados / total) * 100
                callback_estado(f"[{fecha_str_ddmmyyyy}] {completados} de {total}", progreso)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return pd.DataFrame(filas_para_tabla), True
 
def gestionar_descarga_rango(fecha_inicio_str, fecha_fin_str, callback_estado=None):
    fechas_solicitadas = get_fechas_rango(fecha_inicio_str, fecha_fin_str)
    # Cada descarga REEMPLAZA el caché: el Excel resultante contiene SOLO el rango
    # pedido; no se arrastran las licitaciones de descargas anteriores.
    codigos_cacheados = set()  # solo evita duplicados dentro de este mismo rango
    
    hubo_exito = False
            
    dfs_nuevos = []
    for fecha in fechas_solicitadas:
        df_dia, exito = descargar_un_dia(fecha, codigos_cacheados, callback_estado)
        if exito:
            hubo_exito = True
            if not df_dia.empty:
                dfs_nuevos.append(df_dia)
                nuevos_ids = df_dia["Numero Adquisición"].astype(str).tolist()
                codigos_cacheados.update(nuevos_ids)
        else:
            if callback_estado: callback_estado(f"⚠️ Servidor caído al buscar el {fecha}.", None)
            time.sleep(2)
            
    df_final = pd.concat(dfs_nuevos, ignore_index=True) if dfs_nuevos else pd.DataFrame()

    if callback_estado: callback_estado("Subiendo datos a la nube...", 100)

    columnas_completas = [
        "Numero Adquisición", "Nombre Adquisición", "Organismo", "RUT Organismo",
        "Fecha Publicación", "Fecha Cierre", "Cantidad",
        "Descripción Producto", "Texto Filtrado",
    ]
    if not df_final.empty:
        df_final = df_final.drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"])
        datos_nube.reemplazar_tabla(TABLA_NUBE, df_final)
    elif hubo_exito:
        # Rango consultado OK pero sin licitaciones publicadas: la tabla queda vacía
        # (no se conservan descargas anteriores). Si TODO falló, no se toca la tabla
        # para no perder datos por un error transitorio del servidor.
        datos_nube.reemplazar_tabla(TABLA_NUBE, pd.DataFrame(columns=columnas_completas))
            
    if os.path.exists("meta_api.json"):
        try: os.remove("meta_api.json")
        except Exception: pass
            
    if callback_estado: callback_estado("¡Descarga completada y guardada!", 100)
    return True