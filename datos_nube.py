# -*- coding: utf-8 -*-
"""
datos_nube.py — Persistencia de los datos descargados en Supabase.

Reemplaza a los Excel locales: al terminar una tanda de descarga, las filas se
suben a su tabla ("compra_agil" o "licitaciones") y el filtrado/visualización
lee siempre desde la tabla. Las columnas de las tablas son el espejo snake_case
de los encabezados que tenían los Excel, y aquí se traduce en ambos sentidos
para que el resto del código siga trabajando con los encabezados de siempre.
"""
import os
import re
import threading
from datetime import datetime, timezone

import pandas as pd

from auth import supabase, ejecutar_con_refresco
from helpers import leer_sesion

# Encabezado Excel -> columna en la tabla
MAPEO_COMPRA_AGIL = {
    "Numero Adquisición":        "numero_adquisicion",
    "Nombre Adquisición":        "nombre_adquisicion",
    "Organismo":                 "organismo",
    "RUT Organismo":             "rut_organismo",
    "Fecha Publicación":         "fecha_publicacion",
    "Fecha Cierre":              "fecha_cierre",
    "Fecha Cierre 1er Llamado":  "fecha_cierre_1er_llamado",
    "Fecha Cierre 2do Llamado":  "fecha_cierre_2do_llamado",
    "Llamado":                   "llamado",
    "Cantidad":                  "cantidad",
    "Descripción Producto":      "descripcion_producto",
    # Antes había un `texto_filtrado` que concatenaba nombre de la adquisición +
    # nombre del producto + descripción: 197 bytes por fila de los que sólo 27
    # eran información nueva. Se guarda únicamente esa parte; el resto ya son
    # columnas y el filtrado las recorre todas igual.
    "Nombre Producto":           "nombre_producto",
}

# Compra Ágil es pública e idéntica para todas las empresas: se guarda UNA sola
# copia, con empresa_id NULL, y todas la leen. Antes se duplicaba por empresa,
# que en la ventana de 10 días con todas las cotizaciones son ~62 MB por copia.
TABLAS_COMPARTIDAS = {"compra_agil"}


def es_compartida(nombre_tabla: str) -> bool:
    return nombre_tabla in TABLAS_COMPARTIDAS

MAPEO_LICITACIONES = {
    "Numero Adquisición":   "numero_adquisicion",
    "Nombre Adquisición":   "nombre_adquisicion",
    "Organismo":            "organismo",
    "Fecha Publicación":    "fecha_publicacion",
    "Fecha Cierre":         "fecha_cierre",
    "Cantidad":             "cantidad",
    "Descripción Producto": "descripcion_producto",
    "Texto Filtrado":       "texto_filtrado",
}

# El conjunto de Compra Ágil ya filtrado de cada empresa, con el seguimiento de
# su estado. Lleva las mismas columnas de la cotización que `compra_agil` —para
# que la pantalla no note la diferencia— más tres de auditoría (por qué entró,
# lo produce `processor.filtrar_licitaciones`) y tres de seguimiento.
TABLA_SEGUIMIENTO = "compra_agil_seguimiento"

MAPEO_SEGUIMIENTO = {
    **MAPEO_COMPRA_AGIL,
    "FILTRO_APLICADO":      "filtro_aplicado",
    "COLUMNA_ENCONTRADA":   "columna_encontrada",
    "DETALLE_COINCIDENCIA": "detalle_coincidencia",
    "Primera Detección":    "primera_deteccion",
    "Última Revisión":      "ultima_revision",
    "Estado Seguimiento":   "estado_seguimiento",
}

_MAPEOS = {
    "compra_agil": MAPEO_COMPRA_AGIL,
    "licitaciones": MAPEO_LICITACIONES,
    TABLA_SEGUIMIENTO: MAPEO_SEGUIMIENTO,
}

_TAM_LOTE_INSERT = 400   # filas por insert (payloads acotados)
_TAM_PAGINA_SELECT = 1000  # PostgREST pagina de a 1000

# Serializa subidas/borrados para que dos hilos no pisen la misma tabla a la vez
_lock_nube = threading.Lock()


class ErrorNube(Exception):
    """Error de comunicación con la base de datos en la nube."""


def _exec(construir_consulta):
    """Ejecuta una consulta a Supabase reintentando si el JWT expiró.

    `construir_consulta` es una función sin argumentos que ARMA y ejecuta la
    consulta (p. ej. `lambda: supabase.table(...).select(...).execute()`). Se
    reconstruye en cada intento para no reutilizar un builder ya consumido.
    """
    return ejecutar_con_refresco(construir_consulta)


def _verificar_cliente():
    if supabase is None:
        raise ErrorNube(
            "Sin conexión a la base de datos en la nube. "
            "Revisa tu conexión a internet y vuelve a intentarlo."
        )


def empresa_actual() -> str:
    """empresa_id de la sesión iniciada.

    Todas las tablas llevan empresa_id y RLS impide ver o tocar lo de otras.
    Aun así, las consultas lo filtran explícitamente: es lo que evita que un
    borrado masivo alcance filas ajenas si algún día una policy se relajara.

    En un runner headless (GitHub Actions) no hay sesión: la empresa llega por la
    variable de entorno EMPRESA_ID que el workflow inyecta desde el disparo.
    """
    env_id = os.environ.get("EMPRESA_ID", "").strip()
    if env_id:
        return env_id

    sesion = leer_sesion()
    empresa_id = sesion.get("empresa_id")
    if empresa_id:
        return empresa_id

    # Un super_admin sin empresa entra al panel pero no tiene dónde guardar lo
    # que descargue: el mensaje tiene que decir eso, no «vuelve a entrar».
    if sesion.get("rol") == "super_admin":
        raise ErrorNube(
            "Tu usuario es super_admin y no está asociado a ninguna empresa, así que "
            "no hay dónde guardar las descargas. Asígnate a una empresa desde el panel "
            "y vuelve a iniciar sesión."
        )

    raise ErrorNube(
        "No hay una empresa asociada a tu sesión. Cierra sesión y vuelve a entrar."
    )


def _limpiar_celda(v):
    if v is None or pd.isna(v):
        return ""
    return str(v)


def _a_filas(nombre_tabla, df, empresa_id):
    """DataFrame con encabezados de Excel -> filas listas para la tabla."""
    mapeo = _MAPEOS[nombre_tabla]
    filas = []
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            fila = {
                col_db: _limpiar_celda(row.get(col_excel, ""))
                for col_excel, col_db in mapeo.items()
            }
            fila["empresa_id"] = empresa_id
            filas.append(fila)
    return filas


def _filtro_empresa(consulta, nombre_tabla, empresa_id):
    """Acota la consulta a quien corresponda: la copia compartida (empresa_id
    NULL) o las filas de la empresa."""
    if es_compartida(nombre_tabla):
        return consulta.is_("empresa_id", "null")
    return consulta.eq("empresa_id", empresa_id)


def reemplazar_tabla(nombre_tabla: str, df: pd.DataFrame):
    """Reemplaza TODO el contenido de la tabla con las filas del DataFrame
    (que usa los encabezados estilo Excel). Se llama al terminar una tanda."""
    _verificar_cliente()
    # En las tablas compartidas no hay empresa: la copia es una sola.
    empresa_id = None if es_compartida(nombre_tabla) else empresa_actual()
    filas = _a_filas(nombre_tabla, df, empresa_id)

    with _lock_nube:
        try:
            # El borrado va acotado. Antes era `.neq("id", 0)`, que barría la
            # tabla entera: con varias empresas, eso habría borrado los datos de
            # todas las demás.
            _exec(lambda: _filtro_empresa(
                supabase.table(nombre_tabla).delete(), nombre_tabla, empresa_id).execute())
            for i in range(0, len(filas), _TAM_LOTE_INSERT):
                lote = filas[i:i + _TAM_LOTE_INSERT]
                _exec(lambda lote=lote: supabase.table(nombre_tabla).insert(lote).execute())
        except ErrorNube:
            raise
        except Exception as e:
            raise ErrorNube(f"No se pudieron subir los datos a la nube: {e}") from e


def sincronizar_tabla(nombre_tabla: str, df_nuevos: pd.DataFrame,
                      codigos_a_borrar, columna_codigo: str = "numero_adquisicion"):
    """Actualiza SÓLO lo que cambió, en vez de rehacer la tabla entera.

    Es lo que permite que la app se traiga después nada más que la diferencia:
    al reescribir sólo las filas tocadas, la marca `actualizado` del resto no se
    mueve. Con un borrado+alta completo, cada noche parecería que cambió todo.

    `codigos_a_borrar` son los códigos cuyas filas hay que quitar: los que se
    vuelven a bajar (para reemplazarlos) y los que salieron de la ventana.
    """
    _verificar_cliente()
    empresa_id = None if es_compartida(nombre_tabla) else empresa_actual()
    filas = _a_filas(nombre_tabla, df_nuevos, empresa_id)
    codigos = [str(c) for c in dict.fromkeys(codigos_a_borrar or []) if str(c).strip()]

    with _lock_nube:
        try:
            for i in range(0, len(codigos), 100):   # `in_` con listas acotadas
                lote = codigos[i:i + 100]
                _exec(lambda lote=lote: _filtro_empresa(
                    supabase.table(nombre_tabla).delete(), nombre_tabla, empresa_id)
                    .in_(columna_codigo, lote).execute())
            for i in range(0, len(filas), _TAM_LOTE_INSERT):
                lote = filas[i:i + _TAM_LOTE_INSERT]
                _exec(lambda lote=lote: supabase.table(nombre_tabla).insert(lote).execute())
        except ErrorNube:
            raise
        except Exception as e:
            raise ErrorNube(f"No se pudieron sincronizar los datos: {e}") from e


def leer_tabla(nombre_tabla: str, desde: str = None) -> pd.DataFrame:
    """Lee la tabla y la devuelve con los encabezados estilo Excel.

    Con `desde` (marca ISO de la última sincronización) trae SÓLO las filas
    tocadas después de ese momento. Es lo que evita bajarse los ~40 MB de Compra
    Ágil cada vez que alguien entra al módulo: tras la primera vez, cada día son
    unos pocos MB. La columna `actualizado` viene de `sql_compra_agil_compartida.sql`.
    """
    _verificar_cliente()
    mapeo = _MAPEOS[nombre_tabla]
    inverso = {v: k for k, v in mapeo.items()}
    empresa_id = empresa_actual() if not es_compartida(nombre_tabla) else None

    columnas = list(mapeo.values())
    if desde:
        columnas.append("actualizado")
    seleccion = ",".join(columnas)

    filas = []
    try:
        offset = 0
        while True:
            def _consulta():
                q = _filtro_empresa(
                    supabase.table(nombre_tabla).select(seleccion), nombre_tabla, empresa_id)
                if desde:
                    q = q.gt("actualizado", desde)
                return q.order("id").range(offset, offset + _TAM_PAGINA_SELECT - 1).execute()

            resp = _exec(_consulta)
            datos = resp.data or []
            filas.extend(datos)
            if len(datos) < _TAM_PAGINA_SELECT:
                break
            offset += _TAM_PAGINA_SELECT
    except Exception as e:
        raise ErrorNube(f"No se pudieron leer los datos de la nube: {e}") from e

    columnas_finales = list(mapeo.keys()) + (["actualizado"] if desde else [])
    if not filas:
        return pd.DataFrame(columns=columnas_finales)

    df = pd.DataFrame(filas).rename(columns=inverso)
    # Garantizar todas las columnas esperadas y en el orden de siempre
    for col in columnas_finales:
        if col not in df.columns:
            df[col] = ""
    return df[columnas_finales].fillna("")


def marca_mas_reciente(nombre_tabla: str):
    """Cuándo se tocó por última vez la tabla (ISO), o None.

    La app la consulta antes de sincronizar: si coincide con lo que ya tiene
    guardado, se ahorra la descarga entera. Es una sola fila de respuesta."""
    if supabase is None:
        return None
    try:
        empresa_id = empresa_actual() if not es_compartida(nombre_tabla) else None
        resp = _exec(lambda: _filtro_empresa(
            supabase.table(nombre_tabla).select("actualizado"), nombre_tabla, empresa_id)
            .order("actualizado", desc=True).limit(1).execute())
        datos = resp.data or []
        return datos[0].get("actualizado") if datos else None
    except Exception:
        return None


def codigos_vivos(nombre_tabla: str, columna_codigo: str = "numero_adquisicion"):
    """Los códigos que siguen en la tabla, o None si no se pudo preguntar.

    Trae UNA sola columna a propósito: sirve para que la copia local sepa qué se
    borró, y eso la sincronización incremental no puede contarlo (un borrado no
    deja fila que traer, así que `leer_tabla(desde=...)` nunca se entera). Medido
    en Compra Ágil con 25.000 filas: ~1 MB, frente a los ~40 MB de leerla entera.

    Devolver None y no un conjunto vacío ante un fallo es deliberado: con un
    vacío, quien llame creería que la nube no tiene nada y borraría su copia."""
    if supabase is None:
        return None
    try:
        empresa_id = empresa_actual() if not es_compartida(nombre_tabla) else None
        codigos, offset = set(), 0
        while True:
            def _consulta():
                return _filtro_empresa(
                    supabase.table(nombre_tabla).select(columna_codigo),
                    nombre_tabla, empresa_id).order("id").range(
                        offset, offset + _TAM_PAGINA_SELECT - 1).execute()

            datos = _exec(_consulta).data or []
            codigos.update(str(f[columna_codigo]) for f in datos)
            if len(datos) < _TAM_PAGINA_SELECT:
                return codigos
            offset += _TAM_PAGINA_SELECT
    except Exception:
        return None


# ===========================================================================
# Compra Ágil ya filtrada, con seguimiento de estado
#
# El barrido filtra una vez por empresa y deja aquí el resultado; la app lo lee
# y no filtra nada. Como el conjunto es chico (cientos de filas, no 38.000), el
# barrido diurno puede permitirse volver a preguntarle al portal por cada
# cotización y mantener su estado al día.
# ===========================================================================

def estado_seguimiento_previo() -> dict:
    """{numero_adquisicion: {...}} de lo que la empresa ya venía siguiendo.

    Devuelve por cada código: `primera_deteccion`, `ultima_revision`,
    `estado_seguimiento`, `llamado` y los dos cierres.

    Hace falta porque cada refiltrado REHACE el conjunto desde `compra_agil`, y
    esa tabla no siempre es la más fresca: la descarga diurna sólo lista lo
    publicado HOY, así que una cotización de hace días que pasó a 2do llamado
    esta mañana sigue figurando ahí como «1er llamado». Reconstruir a ciegas
    pisaba con ese dato viejo lo que el seguimiento acababa de confirmar
    preguntándole al portal, y el cambio de llamado no llegaba nunca a la
    pantalla: se perdía en el siguiente barrido, una y otra vez.
    """
    if supabase is None:
        return {}
    try:
        empresa_id = empresa_actual()
        previo, offset = {}, 0
        while True:
            def _consulta():
                return (supabase.table(TABLA_SEGUIMIENTO)
                        .select("numero_adquisicion, primera_deteccion, ultima_revision, "
                                "estado_seguimiento, llamado, "
                                "fecha_cierre_1er_llamado, fecha_cierre_2do_llamado")
                        .eq("empresa_id", empresa_id).order("id")
                        .range(offset, offset + _TAM_PAGINA_SELECT - 1).execute())

            datos = _exec(_consulta).data or []
            for fila in datos:
                codigo = str(fila.get("numero_adquisicion") or "")
                if codigo and codigo not in previo:
                    previo[codigo] = fila
            if len(datos) < _TAM_PAGINA_SELECT:
                return previo
            offset += _TAM_PAGINA_SELECT
    except Exception:
        return {}


def guardar_seguimiento(df: pd.DataFrame) -> int:
    """Reemplaza el conjunto filtrado de la empresa. Devuelve las filas escritas.

    No usa `_a_filas` porque las marcas de tiempo no admiten el "" que ése
    escribe para los vacíos: cuando no hay valor hay que OMITIR la columna, y
    entonces manda el default de la tabla (`now()` para `primera_deteccion`).
    """
    _verificar_cliente()
    empresa_id = empresa_actual()

    filas = []
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            fila = {"empresa_id": empresa_id}
            for col_excel, col_db in MAPEO_SEGUIMIENTO.items():
                valor = _limpiar_celda(row.get(col_excel, ""))
                # Las tres de tipo fecha/estado se omiten si vienen vacías.
                if col_db in ("primera_deteccion", "ultima_revision") and not valor:
                    continue
                if col_db == "estado_seguimiento" and not valor:
                    continue
                fila[col_db] = valor
            filas.append(fila)

    with _lock_nube:
        try:
            _exec(lambda: supabase.table(TABLA_SEGUIMIENTO).delete()
                  .eq("empresa_id", empresa_id).execute())
            for i in range(0, len(filas), _TAM_LOTE_INSERT):
                lote = filas[i:i + _TAM_LOTE_INSERT]
                _exec(lambda lote=lote: supabase.table(TABLA_SEGUIMIENTO)
                      .insert(lote).execute())
        except ErrorNube:
            raise
        except Exception as e:
            raise ErrorNube(f"No se pudo guardar el seguimiento: {e}") from e
    return len(filas)


def seguimiento_vigentes() -> list:
    """[{codigo, llamado, cierre1, cierre2}], una entrada por cotización viva.

    La tabla lleva una fila por producto; aquí se colapsa a una por cotización,
    que es el grano al que se le hace seguimiento.
    """
    _verificar_cliente()
    empresa_id = empresa_actual()
    porc, offset = {}, 0
    try:
        while True:
            def _consulta():
                # Orden por ULTIMA REVISION, lo más viejo primero y lo nunca
                # revisado antes que nada.
                #
                # Iba por `id`, y con un tope de revisiones por pasada eso
                # significaba mirar SIEMPRE las mismas primeras: las del final
                # de la lista no se actualizaban jamás. No era mala suerte, era
                # sistemático. Así cada corrida ataca lo más rezagado y todo
                # acaba pasando por el portal.
                return (supabase.table(TABLA_SEGUIMIENTO)
                        .select("numero_adquisicion, llamado, "
                                "fecha_cierre_1er_llamado, fecha_cierre_2do_llamado, "
                                "ultima_revision")
                        .eq("empresa_id", empresa_id)
                        .eq("estado_seguimiento", "vigente")
                        .order("ultima_revision", desc=False, nullsfirst=True)
                        .order("id")
                        .range(offset, offset + _TAM_PAGINA_SELECT - 1)
                        .execute())

            datos = _exec(_consulta).data or []
            for fila in datos:
                codigo = str(fila.get("numero_adquisicion") or "")
                if codigo and codigo not in porc:
                    porc[codigo] = {
                        "codigo": codigo,
                        "llamado": fila.get("llamado") or "",
                        "cierre1": fila.get("fecha_cierre_1er_llamado") or "",
                        "cierre2": fila.get("fecha_cierre_2do_llamado") or "",
                    }
            if len(datos) < _TAM_PAGINA_SELECT:
                break
            offset += _TAM_PAGINA_SELECT
    except Exception as e:
        raise ErrorNube(f"No se pudo leer el seguimiento: {e}") from e
    return list(porc.values())


def actualizar_seguimiento(codigo: str, campos: dict):
    """Refresca las columnas de UNA cotización seguida (todas sus filas)."""
    if not codigo or not campos:
        return
    _verificar_cliente()
    empresa_id = empresa_actual()
    datos = dict(campos)
    datos["actualizado"] = datetime.now(timezone.utc).isoformat()
    with _lock_nube:
        try:
            _exec(lambda: supabase.table(TABLA_SEGUIMIENTO).update(datos)
                  .eq("empresa_id", empresa_id)
                  .eq("numero_adquisicion", str(codigo)).execute())
        except Exception as e:
            raise ErrorNube(f"No se pudo actualizar el seguimiento: {e}") from e


def cerrar_seguimiento(codigos) -> int:
    """Marca como 'cerrada' las cotizaciones indicadas. Devuelve cuántas.

    No se borran: que una cotización se haya cerrado es justamente el final del
    seguimiento, y el usuario tiene que poder verlo. La poda por antigüedad la
    hace el refiltrado, que sólo conserva lo que sigue en la ventana.
    """
    codigos = [str(c) for c in dict.fromkeys(codigos or []) if str(c).strip()]
    if not codigos:
        return 0
    _verificar_cliente()
    empresa_id = empresa_actual()
    datos = {"estado_seguimiento": "cerrada",
             "ultima_revision": datetime.now(timezone.utc).isoformat(),
             "actualizado": datetime.now(timezone.utc).isoformat()}
    with _lock_nube:
        try:
            for i in range(0, len(codigos), 100):
                lote = codigos[i:i + 100]
                _exec(lambda lote=lote: supabase.table(TABLA_SEGUIMIENTO)
                      .update(datos).eq("empresa_id", empresa_id)
                      .in_("numero_adquisicion", lote).execute())
        except Exception as e:
            raise ErrorNube(f"No se pudieron cerrar las cotizaciones: {e}") from e
    return len(codigos)


def _fila_estado(empresa_id, modulo, estado, detalle, progreso, fase):
    """La fila de `descarga_estado` tal como se guarda. Un solo sitio."""
    fila = {
        "empresa_id": empresa_id,
        "modulo": modulo,
        "estado": estado,
        "detalle": detalle,
        "actualizado": datetime.now(timezone.utc).isoformat(),
    }
    # Se mandan aunque vayan en None: al terminar hay que BORRAR el porcentaje
    # de la corrida anterior, o la app pintaría una barra al 73% para siempre.
    fila["progreso"] = None if progreso is None else max(0, min(100, int(progreso)))
    fila["fase"] = fase

    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if run_id.isdigit():
        fila["run_id"] = int(run_id)
    return fila


def escribir_estado_descarga(modulo: str, estado: str, detalle: str = "",
                             progreso=None, fase: str = None):
    """Registra el estado de una descarga que corre en el servidor (GitHub Actions)
    en la tabla `descarga_estado`, para que la app pueda sondearlo. `estado` es uno
    de: 'corriendo', 'listo', 'error'. Un único registro por `modulo` (upsert).

    `progreso` (0..100) y `fase` son lo que deja a la app pintar una barra en vez
    de un texto. El porcentaje es del trabajo ENTERO, no de la fase: una barra
    que vuelve a cero tres veces no informa, desconcierta.

    Si corre dentro de un runner, deja anotado además el id de la corrida
    (`GITHUB_RUN_ID`, que Actions inyecta solo). Es lo que permite cancelarla:
    la Edge Function lo lee de aquí en vez de aceptarlo del cliente, así nadie
    puede cancelar la corrida de otra empresa mandando un id cualquiera."""
    _verificar_cliente()
    fila = _fila_estado(empresa_actual(), modulo, estado, detalle, progreso, fase)
    with _lock_nube:
        try:
            # El estado es por empresa: dos que descarguen a la vez no deben
            # pisarse el registro.
            _exec(lambda: supabase.table("descarga_estado").upsert(
                fila, on_conflict="empresa_id,modulo"
            ).execute())
        except Exception as e:
            raise ErrorNube(f"No se pudo escribir el estado de descarga: {e}") from e


def escribir_estado_varias(modulo: str, empresas, estado: str, detalle: str = "",
                           progreso=None, fase: str = None):
    """El mismo estado para varias empresas, en UNA petición.

    Existe por la descarga del barrido: es una copia compartida que sirve a
    todas a la vez, así que su avance le interesa a todas. Escribirlo empresa
    por empresa serían N peticiones cada cuatro segundos durante hora y media.

    `empresas` es un iterable de ids. Devuelve cuántas filas se escribieron.
    Nunca lanza: informar del avance no puede tumbar el barrido.
    """
    ids = [str(e) for e in (empresas or []) if e]
    if not ids or supabase is None:
        return 0

    filas = [_fila_estado(i, modulo, estado, detalle, progreso, fase) for i in ids]
    with _lock_nube:
        try:
            _exec(lambda: supabase.table("descarga_estado").upsert(
                filas, on_conflict="empresa_id,modulo"
            ).execute())
            return len(filas)
        except Exception as e:
            print(f"[estado] No se pudo escribir el avance: {e}", flush=True)
            return 0


def guardar_rango_descarga(modulo: str, desde: str, hasta: str):
    """Anota en la nube el rango de fechas de la última descarga de `modulo`.

    Así cualquier equipo de la empresa abre el módulo y ve el rango que se bajó
    por última vez, la haya lanzado quien la haya lanzado.

    Es best-effort a propósito, y va en su propia consulta: si la base todavía no
    tiene las columnas (falta correr `sql_rango_descarga.sql`), esto falla en
    silencio y la descarga sigue su curso normal."""
    if supabase is None:
        return
    try:
        with _lock_nube:
            _exec(lambda: supabase.table("descarga_estado")
                  .update({"rango_desde": str(desde), "rango_hasta": str(hasta)})
                  .eq("empresa_id", empresa_actual()).eq("modulo", modulo).execute())
    except Exception:
        pass


# El runner deja en `detalle` un texto tipo "Rango 02-08-2026 a 03-08-2026
# descargado.". Sirve de respaldo para las descargas anteriores a que existieran
# las columnas rango_desde/rango_hasta.
_RE_RANGO_DETALLE = re.compile(r"(\d{2}-\d{2}-\d{4})\D+(\d{2}-\d{2}-\d{4})")


def leer_rango_descarga(modulo: str):
    """(desde, hasta) de la última descarga registrada en la nube, o None si no
    hay conexión, no hay registro o la base aún no tiene esas columnas."""
    if supabase is None:
        return None
    try:
        resp = _exec(lambda: supabase.table("descarga_estado")
                     .select("rango_desde, rango_hasta, detalle")
                     .eq("empresa_id", empresa_actual()).eq("modulo", modulo)
                     .limit(1).execute())
        fila = (resp.data or [{}])[0] if resp.data else None
        if not fila:
            return None
        desde, hasta = fila.get("rango_desde"), fila.get("rango_hasta")
        if desde and hasta:
            return (desde, hasta)
        # Respaldo: deducirlo del texto de estado que dejó el servidor.
        m = _RE_RANGO_DETALLE.search(str(fila.get("detalle") or ""))
        return (m.group(1), m.group(2)) if m else None
    except Exception:
        return None


def solicitar_cancelacion(modulo: str):
    """Marca en la nube que se pidió cancelar la descarga de `modulo`, para que el
    equipo que la está corriendo (Compra Ágil local) se detenga solo. Usa `update`
    (no upsert) para tocar SOLO la columna `cancelar` sin pisar el resto."""
    _verificar_cliente()
    with _lock_nube:
        try:
            _exec(lambda: supabase.table("descarga_estado").update({"cancelar": True})
                  .eq("empresa_id", empresa_actual()).eq("modulo", modulo).execute())
        except Exception as e:
            raise ErrorNube(f"No se pudo solicitar la cancelación: {e}") from e


def limpiar_cancelacion(modulo: str):
    """Baja la bandera `cancelar` de `modulo` (al iniciar o terminar). No lanza."""
    if supabase is None:
        return
    try:
        with _lock_nube:
            _exec(lambda: supabase.table("descarga_estado").update({"cancelar": False})
                  .eq("empresa_id", empresa_actual()).eq("modulo", modulo).execute())
    except Exception:
        pass


def cancelacion_solicitada(modulo: str) -> bool:
    """Lee directo de la nube si hay una cancelación pendiente para `modulo`. La usa
    el runner headless (que no tiene la caché de estado_descargas) para detenerse solo."""
    if supabase is None:
        return False
    try:
        resp = _exec(lambda: supabase.table("descarga_estado").select("cancelar")
                     .eq("empresa_id", empresa_actual()).eq("modulo", modulo).limit(1).execute())
        return bool(resp.data and resp.data[0].get("cancelar"))
    except Exception:
        return False


def leer_estado_descarga(modulo: str):
    """Devuelve el registro de estado de `modulo` (dict con estado/detalle/actualizado)
    o None si no hay conexión ni fila. No lanza: la UI simplemente sigue sondeando."""
    if supabase is None:
        return None
    try:
        resp = _exec(lambda: supabase.table("descarga_estado").select("*")
                     .eq("empresa_id", empresa_actual())
                     .eq("modulo", modulo).limit(1).execute())
        return resp.data[0] if resp.data else None
    except Exception:
        return None


def leer_estados_descarga():
    """Lee la tabla `descarga_estado` de esta empresa (pocas filas) de una vez y la
    devuelve como {modulo: {estado, detalle, actualizado}}. Devuelve None si no hay
    conexión o falla la lectura (para que el sondeo distinga 'no pude leer' de 'no
    hay filas' y no borre la caché por un error transitorio)."""
    if supabase is None:
        return None
    try:
        resp = _exec(lambda: supabase.table("descarga_estado").select("*")
                     .eq("empresa_id", empresa_actual()).execute())
        return {r["modulo"]: r for r in (resp.data or []) if r.get("modulo")}
    except Exception:
        return None


# Tope de licitaciones que puede tener la lista a revisar. Revisar un foro es una
# visita al portal por licitación: sin tope, una lista larga convierte cada
# revisión en un barrido de horas (y en más carga de la que el portal tolera).
LIMITE_OFERTADAS = 300


def planificar_ofertadas(codigos):
    """Calcula qué pasaría al agregar `codigos`, SIN escribir nada.

    Lo usa la pantalla para poder avisar antes de tocar la lista. Devuelve:
        nuevos       códigos que aún no están en la lista (sin repetir, en el
                     orden en que llegaron)
        existentes   cuántos hay ya guardados
        total        cuántos habría si se agregaran todos
        excede       True si ese total pasa de LIMITE_OFERTADAS
        a_guardar    los que realmente se guardarían respetando el tope
        a_quitar     los guardados más antiguos que habría que soltar para dejar
                     sitio (vacío si no se excede)

    Al recortar mandan las más recientes: primero lo que se acaba de ingresar (en
    su orden: los exports de Mercado Público vienen de más nuevo a más viejo) y
    después lo ya guardado, de más reciente a más antiguo.
    """
    limpios = [c for c in dict.fromkeys(str(x).strip() for x in codigos) if c]

    guardadas = leer_ofertadas()  # ya viene de más reciente a más antigua
    ya = [str(f.get("codigo") or "").strip() for f in guardadas]
    ya = [c for c in ya if c]
    ya_set = set(ya)

    nuevos = [c for c in limpios if c not in ya_set]
    total = len(ya) + len(nuevos)

    if total <= LIMITE_OFERTADAS:
        return {"nuevos": nuevos, "existentes": len(ya), "total": total,
                "excede": False, "a_guardar": nuevos, "a_quitar": []}

    combinadas = nuevos + ya          # las nuevas cuentan como las más recientes
    conservadas = set(combinadas[:LIMITE_OFERTADAS])
    return {
        "nuevos": nuevos,
        "existentes": len(ya),
        "total": total,
        "excede": True,
        "a_guardar": [c for c in nuevos if c in conservadas],
        "a_quitar": [c for c in ya if c not in conservadas],
    }


def agregar_ofertadas(codigos) -> int:
    """Agrega códigos a la lista de ofertadas (`foro_ofertadas`) de la empresa,
    respetando el tope de LIMITE_OFERTADAS.

    Deduplica por (empresa_id, codigo). Si con lo nuevo se pasa del tope, se
    quedan las más recientes y se sueltan las más antiguas (ver
    `planificar_ofertadas`). Devuelve cuántos códigos quedaron guardados de los
    que se pidieron.
    """
    _verificar_cliente()
    plan = planificar_ofertadas(codigos)

    if plan["a_quitar"]:
        eliminar_ofertadas(plan["a_quitar"])

    a_guardar = plan["a_guardar"]
    if not a_guardar:
        return 0

    empresa_id = empresa_actual()
    filas = [{"empresa_id": empresa_id, "codigo": c} for c in a_guardar]
    with _lock_nube:
        try:
            _exec(lambda: supabase.table("foro_ofertadas")
                  .upsert(filas, on_conflict="empresa_id,codigo", ignore_duplicates=True)
                  .execute())
        except Exception as e:
            raise ErrorNube(f"No se pudieron guardar las licitaciones ofertadas: {e}") from e
    return len(a_guardar)


def leer_ofertadas() -> list:
    """Lista de ofertadas de la empresa de la sesión: [{codigo, fecha_alta,
    ultima_revision}], de la más reciente a la más antigua."""
    _verificar_cliente()
    empresa_id = empresa_actual()
    try:
        resp = _exec(lambda: supabase.table("foro_ofertadas")
                     .select("codigo, fecha_alta, ultima_revision")
                     .eq("empresa_id", empresa_id)
                     .order("fecha_alta", desc=True).execute())
        return resp.data or []
    except Exception as e:
        raise ErrorNube(f"No se pudieron leer las licitaciones ofertadas: {e}") from e


def eliminar_ofertadas(codigos):
    """Quita códigos de la lista de ofertadas de la empresa de la sesión."""
    _verificar_cliente()
    limpios = [str(c).strip() for c in codigos if str(c).strip()]
    if not limpios:
        return
    empresa_id = empresa_actual()
    with _lock_nube:
        try:
            for i in range(0, len(limpios), 100):
                lote = limpios[i:i + 100]
                _exec(lambda lote=lote: supabase.table("foro_ofertadas").delete()
                      .eq("empresa_id", empresa_id).in_("codigo", lote).execute())
        except Exception as e:
            raise ErrorNube(f"No se pudieron quitar de la lista: {e}") from e


# Revisiones manuales de foros permitidas por día y por empresa. La revisión
# visita el portal una vez por licitación; a mano y sin tope, un par de personas
# de la misma empresa pueden convertirlo en cientos de visitas seguidas. El
# barrido nocturno no cuenta: corre una vez y de madrugada.
LIMITE_REVISIONES_DIA = 2


def revisiones_manuales_hoy():
    """Cuántas revisiones manuales de foros lleva hoy la empresa.

    Devuelve None si no se puede saber (sin conexión, o falta correr
    `sql_foro_limite.sql`). Quien llama debe tratar None como «no sé» y dejar
    pasar: es preferible una revisión de más que bloquear a alguien por un
    problema de infraestructura.
    """
    if supabase is None:
        return None
    try:
        hoy = datetime.now(timezone.utc).date().isoformat()
        resp = _exec(lambda: supabase.table("foro_revisiones")
                     .select("id", count="exact")
                     .eq("empresa_id", empresa_actual())
                     .eq("fecha", hoy)
                     .limit(1).execute())
        return resp.count
    except Exception:
        return None


def registrar_revision_manual():
    """Anota una revisión manual de hoy. Best-effort: si falla, no se impide
    revisar (el tope es una cortesía con el portal, no una regla de negocio)."""
    if supabase is None:
        return
    try:
        fila = {"empresa_id": empresa_actual(),
                "fecha": datetime.now(timezone.utc).date().isoformat()}
        with _lock_nube:
            _exec(lambda: supabase.table("foro_revisiones").insert(fila).execute())
    except Exception:
        pass


def hay_datos(nombre_tabla: str) -> bool:
    """True si la tabla tiene al menos una fila DE ESTA EMPRESA. False también si
    no hay conexión (para que la UI simplemente deje el botón de filtrar
    deshabilitado)."""
    if supabase is None:
        return False
    try:
        resp = _exec(lambda: supabase.table(nombre_tabla).select("id")
                     .eq("empresa_id", empresa_actual()).limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def contar_filas(nombre_tabla: str):
    """Cuenta las filas de la tabla sin traer los datos.

    Devuelve un entero, o None si no hay conexión o falla la consulta (para que
    la UI muestre un guion en vez de un cero engañoso).

    Va por `_filtro_empresa` como todo lo demás. Antes filtraba a pelo con
    `.eq("empresa_id", empresa_actual())`, y eso dejó de valer cuando Compra Ágil
    pasó a copia compartida (`empresa_id` NULL): ninguna fila casa con el id de
    una empresa, así que la tarjeta de inicio marcaba 0 cotizaciones habiendo
    decenas de miles.

    Es la consulta más barata que hay —PostgREST devuelve el total en una
    cabecera—, así que sirve además de aviso barato para la copia local: si tiene
    MÁS filas que la nube, es que arrastra cotizaciones ya soltadas (ver
    `cache_compra_agil._soltar_las_que_ya_no_estan`).
    """
    if supabase is None:
        return None
    try:
        empresa_id = empresa_actual() if not es_compartida(nombre_tabla) else None
        resp = _exec(lambda: _filtro_empresa(
            supabase.table(nombre_tabla).select("id", count="exact"),
            nombre_tabla, empresa_id).limit(1).execute())
        return resp.count
    except Exception:
        return None
