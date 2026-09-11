import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import os

import datos_nube
import historial_clientes
from empresa_config import ticket_mercado_publico

URL_API = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"

# Tabla en Supabase donde viven los datos (reemplaza al Excel local)
TABLA_NUBE = "licitaciones"

# Peticiones en paralelo al bajar el detalle de cada licitación. A diferencia del
# buscador de Compra Ágil, esta es la API OFICIAL (api.mercadopublico.cl) y limita
# fuerte por ticket: se midió que 6 hilos pierden ~25% de licitaciones, pero 3 hilos
# con backoff exponencial no pierden ninguna. NO subir de 3.
MAX_WORKERS_LICIT = 3

# Días hacia atrás a los que el barrido nocturno le pide el listado al portal.
# NO es una ventana de retención: lo ya descargado no se suelta por cumplir días
# (ver `_dias_del_barrido`), sólo por dejar de estar Publicada.
DIAS_VENTANA = 30

# Techo de días EXTRA (fuera de la ventana) que se listan en una corrida para
# vigilar las licitaciones de plazo largo. Cada uno cuesta UNA petición, así que
# el número real —los días de publicación de lo que sigue abierto con más de 30
# días— cabe de sobra aquí; el tope sólo evita que una fecha corrupta convierta
# el barrido en miles de peticiones. Lo que quede fuera no se pierde: esa noche
# no se poda y se mira en la siguiente.
MAX_DIAS_EXTRA = 200

# Códigos guardados que un listado bueno no devolvió. No debería pasar nunca —se
# guardan con la misma fecha de publicación con la que se listaron—, así que se
# comprueban uno a uno con `estado_licitacion`; el tope evita que un fallo del
# portal se convierta en una tormenta de peticiones.
MAX_AUSENTES_A_VERIFICAR = 500

# Cada cuántas fichas se sube lo descargado. Guardar sobre la marcha es lo que
# permite que una corrida cortada (por tiempo o por un fallo) no se pierda: lo ya
# subido deja de pedirse, así que la siguiente sigue donde quedó. La primera
# población son miles de fichas y no cabe en una sola corrida.
LOTE_GUARDADO = 400

# Las columnas de la tabla, en orden y con los encabezados de siempre. Sirven
# para armar el DataFrame vacío que `sincronizar_tabla` espera cuando lo único
# que hay que hacer es borrar.
COLUMNAS_COMPLETAS = [
    "Numero Adquisición", "Nombre Adquisición", "Organismo", "RUT Organismo",
    "Fecha Publicación", "Fecha Cierre", "Cantidad",
    "Descripción Producto", "Texto Filtrado",
]


 
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
 
def fecha_iso_de(fecha_str_ddmmyyyy):
    """'05-08-2026' -> '2026-08-05'. La forma en que el portal fecha las fichas."""
    d = fecha_str_ddmmyyyy.replace("-", "")
    return f"{d[4:8]}-{d[2:4]}-{d[0:2]}"


def _listar_dia(fecha_str_ddmmyyyy):
    """El listado de un día tal como lo da el portal. -> (listado, se_pudo)

    Se devuelve CRUDO, con todos los estados. Quien llama decide qué hacer con
    cada uno: la descarga se queda sólo con las publicadas (`CodigoEstado == 5`),
    y el barrido nocturno necesita además las otras, porque que una licitación
    guardada aparezca aquí con otro estado es la única señal de que dejó de
    admitir ofertas.

    `se_pudo` distingue «el portal contestó y ese día no hay nada» de «el portal
    no contestó». Sin esa diferencia se daría por cerrado lo que ni se preguntó.
    """
    fecha_busqueda = fecha_str_ddmmyyyy.replace("-", "")
    for _ in range(3):
        try:
            resp = requests.get(URL_API,
                                params={"fecha": fecha_busqueda, "ticket": ticket_mercado_publico()},
                                timeout=15)
            if resp.status_code == 200:
                return resp.json().get("Listado", []) or [], True
            if resp.status_code in [429, 500, 502, 503, 504]:
                time.sleep(2)
                continue
            return [], False
        except Exception:
            time.sleep(2)
    return [], False


def descargar_un_dia(fecha_str_ddmmyyyy, codigos_cacheados, callback_estado=None):
    fecha_exacta_str = fecha_iso_de(fecha_str_ddmmyyyy)

    if callback_estado: callback_estado(f"[{fecha_str_ddmmyyyy}] Preparando...", 0)

    licitaciones_crudas, se_pudo = _listar_dia(fecha_str_ddmmyyyy)
    if not se_pudo:
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
 
def _agotado(corte):
    """¿Se acabó el presupuesto de tiempo de esta corrida?"""
    return corte is not None and time.monotonic() > corte


def _dias_del_barrido(unicos, date_from, date_to):
    """Qué días hay que pedirle al listado. -> (días 'dd-mm-yyyy', días omitidos)

    Son dos grupos, y por motivos distintos:

      · LA VENTANA: los últimos `DIAS_VENTANA` días. De ahí sale lo NUEVO.
      · LOS DÍAS DE LO YA GUARDADO que cayeron fuera de la ventana. Ésos no
        traen nada nuevo: se listan para VIGILAR si esas licitaciones siguen
        Publicadas, que es lo único que las suelta de la tabla.

    Que una licitación lleve 40 días abierta no la borra —el plazo lo pone el
    organismo, no nosotros—, así que la única forma de vigilarla es volver a
    listar el día en que se publicó. Cuesta UNA petición por día, dé ese día una
    licitación o doscientas, y como se pregunta por el día de publicación que ya
    está GUARDADO, el portal la devuelve donde se la espera.
    """
    dias = {date_from + timedelta(days=i)
            for i in range((date_to - date_from).days + 1)}

    extras = set()
    if unicos is not None and not unicos.empty and "Fecha Publicación" in unicos.columns:
        # ISO8601 a propósito: la API manda la hora con 0 a 3 decimales, y la
        # inferencia por defecto deja sin fecha las filas que no calzan con la
        # primera. Aquí eso no era cosmético: una licitación sin fecha no daba
        # día que vigilar, así que pasados los 30 días nadie volvía a mirar
        # su estado y se quedaba en la tabla para siempre.
        f_pub = pd.to_datetime(unicos["Fecha Publicación"], errors="coerce",
                               format="ISO8601")
        for fecha in pd.unique(f_pub.dropna().dt.date):
            if fecha < date_from or fecha > date_to:
                extras.add(fecha)

    omitidos = []
    if len(extras) > MAX_DIAS_EXTRA:
        # Se quedan los más recientes, que son los que más probablemente sigan
        # vivos. Los otros no se pierden: esta noche no se podan y se miran en
        # la siguiente, cuando la lista de extras ya haya bajado.
        en_orden = sorted(extras, reverse=True)
        omitidos = sorted(en_orden[MAX_DIAS_EXTRA:])
        extras = set(en_orden[:MAX_DIAS_EXTRA])

    return [d.strftime("%d-%m-%Y") for d in sorted(dias | extras)], omitidos


def _verificar_ausentes(codigos, avisar):
    """De esos códigos, los que el portal confirma que ya no están Publicados.

    Se llega aquí sólo con lo que un listado BUENO no devolvió, que no debería
    pasar: cada licitación se guarda con la misma fecha de publicación con la
    que se listó. Como es la excepción, se pregunta una por una en vez de dar
    nada por hecho — y sólo se suelta con una respuesta afirmativa del portal:
    un 'desconocido' deja la licitación donde está.
    """
    codigos = sorted(codigos)
    if not codigos:
        return set()

    if len(codigos) > MAX_AUSENTES_A_VERIFICAR:
        avisar(f"⚠️ {len(codigos)} licitaciones guardadas no salieron en su listado; "
               f"se comprueban {MAX_AUSENTES_A_VERIFICAR} y el resto en la próxima "
               "corrida (esto suele ser el portal fallando, no un cambio de estado).")
        codigos = codigos[:MAX_AUSENTES_A_VERIFICAR]

    fuera = set()
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_LICIT)
    try:
        futuros = {executor.submit(estado_licitacion, c): c for c in codigos}
        for fut in as_completed(futuros):
            if fut.result() in ("cerrada", "resuelta"):
                fuera.add(futuros[fut])
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    avisar(f"De {len(codigos)} licitación(es) que no salieron en su listado, el "
           f"portal confirma que {len(fuera)} ya no están publicadas.")
    return fuera


def gestionar_barrido(callback_estado=None, limite_minutos=None):
    """Barrido nocturno de licitaciones: incremental y sin borrar por antigüedad.

    Es el equivalente de `compra_agil_api.gestionar_descarga_ultimas`, con una
    diferencia de fondo: aquí NADA se suelta por cumplir días. Una licitación
    puede estar abierta dos meses —el plazo lo pone el organismo— y seguir
    siendo lo más valioso de la tabla. El ÚNICO motivo para soltarla es que el
    portal la muestre con un estado distinto de 'Publicada'; mientras siga
    publicada se conserva, tenga los días que tenga.

    De ahí salen las dos mitades del trabajo:

      · Los últimos `DIAS_VENTANA` días se listan para encontrar lo NUEVO.
      · Los días de publicación de lo ya guardado que quedó fuera de la ventana
        se listan para VIGILAR su estado. Una petición por día.

    Sólo se bajan las fichas de los códigos que no están ya en la tabla, así que
    de régimen son las del día; la primera población son miles y se completa en
    varias noches (se guarda por lotes y la siguiente sigue donde quedó).

    `callback_estado(mensaje, progreso, publico)`: `mensaje` es el detalle
    técnico para el log de la corrida y `publico` lo poco que tiene sentido
    enseñarle al cliente en la barra de la app. Sin `publico` se mantiene el
    texto anterior.
    """
    ahora = datetime.now()          # el workflow fija TZ=America/Santiago
    date_to = ahora.date()
    date_from = (ahora - timedelta(days=DIAS_VENTANA)).date()
    corte = (time.monotonic() + limite_minutos * 60) if limite_minutos else None

    def _avisar(mensaje, progreso=None, publico=None):
        if callback_estado:
            callback_estado(mensaje, progreso, publico)

    _avisar("Leyendo lo que ya hay en la nube...", 0, "Buscando licitaciones nuevas...")

    try:
        df_cache = datos_nube.leer_tabla(TABLA_NUBE)
    except datos_nube.ErrorNube as e:
        # Sin saber qué hay guardado no se puede ni podar ni evitar redescargas:
        # seguir sería vaciar la tabla o volver a bajarla entera.
        _avisar(f"ERROR: no se pudo leer la tabla de licitaciones ({e}).", None)
        raise

    unicos = None
    guardados = set()
    dia_de = {}                     # código -> día de publicación 'yyyy-mm-dd'
    if not df_cache.empty and "Numero Adquisición" in df_cache.columns:
        unicos = df_cache.drop_duplicates(subset=["Numero Adquisición"])
        guardados = set(unicos["Numero Adquisición"].astype(str))
        dia_de = dict(zip(unicos["Numero Adquisición"].astype(str),
                          unicos["Fecha Publicación"].astype(str).str[:10]))

    dias, dias_omitidos = _dias_del_barrido(unicos, date_from, date_to)
    if dias_omitidos:
        _avisar(f"⚠️ {len(dias_omitidos)} día(s) antiguo(s) no se vigilan esta noche "
                f"(tope de {MAX_DIAS_EXTRA}); se miran en la próxima.", None)

    _avisar(f"Revisando {len(dias)} día(s) del listado "
            f"({len(guardados)} licitaciones ya guardadas)...", 0,
            "Buscando licitaciones nuevas...")

    codigos_descargar = []          # [(código, día iso)] fichas que faltan
    a_soltar = set()                # el portal las muestra con otro estado
    publicadas_vistas = set()
    dias_ok = set()
    dias_fallidos = []
    listado_cortado = False

    def _publico_listado():
        pendientes = len(codigos_descargar)
        return (f"Descargando {pendientes} licitaciones..." if pendientes
                else "Buscando licitaciones nuevas...")

    for idx_dia, dia in enumerate(dias, start=1):
        if _agotado(corte):
            # Mejor parar y bajar las fichas de lo ya listado que seguir listando
            # días que luego no daría tiempo a descargar.
            listado_cortado = True
            break

        listado, se_pudo = _listar_dia(dia)
        if not se_pudo:
            dias_fallidos.append(dia)
            continue

        dia_iso = fecha_iso_de(dia)
        dias_ok.add(dia_iso)
        for lic in listado:
            codigo = str(lic.get("CodigoExterno") or "")
            if not codigo:
                continue
            if lic.get("CodigoEstado") == 5:
                publicadas_vistas.add(codigo)
                if codigo not in guardados:
                    codigos_descargar.append((codigo, dia_iso))
            elif codigo in guardados:
                # LA ÚNICA REGLA DE BORRADO: cambió de estado, así que ya no
                # admite ofertas y sale de la tabla.
                a_soltar.add(codigo)

        _avisar(f"Revisando licitaciones... día {dia} ({idx_dia}/{len(dias)}) "
                f"({len(codigos_descargar)} por descargar)", 0, _publico_listado())

    if dias_fallidos:
        _avisar(f"⚠️ El portal no respondió al listar {len(dias_fallidos)} día(s): "
                f"{', '.join(dias_fallidos)}. De esos días no se poda nada.", None)

    # Lo guardado de un día que SÍ se listó entero y aun así no apareció. No
    # debería ocurrir; se comprueba una por una antes de tocarla.
    ausentes = {c for c in guardados
                if dia_de.get(c) in dias_ok} - publicadas_vistas - a_soltar
    if ausentes:
        a_soltar |= _verificar_ausentes(ausentes, lambda m: _avisar(m, None))

    # Antes de soltarlas, al historial de quien siga a ese comprador: es la
    # última vez que estos datos existen. El estado que se anota es 'cerrada'
    # —lo único que consta es que dejó de estar publicada; decir 'adjudicada'
    # sería más informativo y sería mentira.
    if a_soltar:
        historial_clientes.archivar(
            df_cache, a_soltar, historial_clientes.TIPO_LICITACION,
            estado_defecto=historial_clientes.ESTADO_POR_DEFECTO)
        _avisar(f"Soltando {len(a_soltar)} licitación(es) que dejaron de estar "
                "publicadas.", None)
        datos_nube.sincronizar_tabla(
            TABLA_NUBE, pd.DataFrame(columns=COLUMNAS_COMPLETAS), a_soltar)

    total = len(codigos_descargar)
    if total == 0:
        # «Sin novedades» sólo se puede decir si de verdad se pudo mirar.
        _avisar(
            "No se pudo comprobar si hay novedades: el portal no respondió al listar."
            if dias_fallidos else "Sin novedades: no hay licitaciones nuevas.", 100,
            None if dias_fallidos else "Sin licitaciones nuevas.")
        return {"completo": not listado_cortado, "descargadas": 0, "pendientes": 0,
                "soltadas": len(a_soltar), "dias_fallidos": list(dias_fallidos),
                "fichas_fallidas": 0}

    # --- Fichas de lo nuevo, en paralelo (3 hilos; ver MAX_WORKERS_LICIT) -----
    filas_lote, codigos_lote = [], []
    descargadas = 0
    procesados = 0
    sin_ficha = []

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

    _avisar(f"Descargando {total} ficha(s) nueva(s)...", 0,
            f"Descargando {total} licitaciones...")

    agotado = False
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS_LICIT)
    try:
        futuros = {executor.submit(procesar_licitacion, codigo, dia_iso): codigo
                   for codigo, dia_iso in codigos_descargar}
        for fut in as_completed(futuros):
            codigo = futuros[fut]
            filas, estado = fut.result()
            if estado == "OK":
                # Un código sólo entra en el lote si SU FICHA LLEGÓ: el lote se
                # le pasa a `sincronizar_tabla` como lista a borrar, así que
                # meter ahí uno fallido borraría lo que había sin reponerlo.
                codigos_lote.append(codigo)
                filas_lote.extend(filas)
                descargadas += 1
            else:
                sin_ficha.append(codigo)
            procesados += 1

            if len(codigos_lote) >= LOTE_GUARDADO:
                _guardar_lote()

            _avisar(f"Descargando licitaciones {procesados} de {total}...",
                    (procesados / total) * 100,
                    f"Revisando licitaciones {procesados} de {total}...")

            # Se corta ANTES de que el runner lo mate: así el lote en curso se
            # guarda y mañana se retoma en vez de perderlo todo.
            if _agotado(corte):
                agotado = True
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    _guardar_lote()
    incompleto = agotado or listado_cortado

    if incompleto:
        _avisar(f"Se acabó el tiempo de esta corrida: {descargadas} de {total} "
                "descargadas. El resto sigue en la próxima.", 100)
    elif sin_ficha:
        _avisar(f"Descarga subida a la nube: {descargadas} de {total}. El portal no "
                f"sirvió la ficha de {len(sin_ficha)}; se reintentan en el próximo "
                "barrido.", 100)
    else:
        _avisar("¡Barrido completado y subido a la nube!", 100)

    return {"completo": not incompleto, "descargadas": descargadas,
            "pendientes": total - procesados, "soltadas": len(a_soltar),
            "dias_fallidos": list(dias_fallidos), "fichas_fallidas": len(sin_ficha)}


def gestionar_descarga_rango(fecha_inicio_str, fecha_fin_str, callback_estado=None):
    """Descarga a mano de un rango de fechas. SUMA a la tabla, no la reemplaza.

    Antes cada descarga REEMPLAZABA la tabla con sólo el rango pedido. Desde que
    la llena el barrido nocturno eso no puede seguir así: un rango histórico
    pedido a mano habría borrado los 30 días que el barrido mantiene al día, y
    además la tabla es ahora una copia COMPARTIDA por todas las empresas.

    Así que este botón quedó para lo que el barrido no cubre —traer un tramo
    antiguo— y sólo añade: lo que ya está guardado ni se vuelve a pedir ni se
    toca. Para soltar algo está el barrido, que es quien sabe qué sigue publicado.
    """
    fechas_solicitadas = get_fechas_rango(fecha_inicio_str, fecha_fin_str)

    # Lo que ya está en la nube no se vuelve a pedir. Sin esto, un rango que
    # pisara la ventana del barrido bajaría de nuevo miles de fichas para dejar
    # exactamente lo mismo que ya había.
    try:
        df_actual = datos_nube.leer_tabla(TABLA_NUBE)
        codigos_cacheados = (set(df_actual["Numero Adquisición"].astype(str))
                             if not df_actual.empty
                             and "Numero Adquisición" in df_actual.columns else set())
    except Exception as e:
        print(f"No se pudo leer lo ya guardado ({e}); se baja el rango entero.", flush=True)
        codigos_cacheados = set()

    dfs_nuevos = []
    for fecha in fechas_solicitadas:
        df_dia, exito = descargar_un_dia(fecha, codigos_cacheados, callback_estado)
        if exito:
            if not df_dia.empty:
                dfs_nuevos.append(df_dia)
                codigos_cacheados.update(df_dia["Numero Adquisición"].astype(str).tolist())
        else:
            if callback_estado: callback_estado(f"⚠️ Servidor caído al buscar el {fecha}.", None)
            time.sleep(2)

    df_final = pd.concat(dfs_nuevos, ignore_index=True) if dfs_nuevos else pd.DataFrame()

    if callback_estado: callback_estado("Subiendo datos a la nube...", 100)

    if not df_final.empty:
        df_final = df_final.drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"])
        # Los códigos del propio rango van como «a borrar» para que una
        # redescarga los reemplace en vez de duplicarlos.
        datos_nube.sincronizar_tabla(
            TABLA_NUBE, df_final,
            df_final["Numero Adquisición"].astype(str).unique().tolist())
    # Un rango sin licitaciones nuevas ya no vacía nada: no hay nada que subir.

    if os.path.exists("meta_api.json"):
        try: os.remove("meta_api.json")
        except Exception: pass

    if callback_estado: callback_estado("¡Descarga completada y guardada!", 100)
    return True
