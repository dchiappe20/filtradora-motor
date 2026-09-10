# -*- coding: utf-8 -*-
"""
historial_clientes.py — Lo que se guarda de los compradores que alguien sigue.

EL AGUJERO QUE TAPA

El módulo «Clientes» arma su tabla cruzando por RUT lo que la app ya tiene
descargado. Rápido y sin pedirle nada al portal, pero las dos tablas de las que
cruza se vacían solas:

  · `compra_agil` suelta cada noche lo que dejó de estar Publicada y lo que sale
    de la ventana de 30 días;
  · `licitaciones` se REEMPLAZA entera en cada descarga.

Así que al vendedor que sigue a un comprador se le borraba el historial de ese
comprador cada noche. Lo que compró el mes pasado —justo lo que sirve para saber
qué venderle el mes que viene— no quedaba en ninguna parte.

CÓMO SE ARREGLA, Y POR QUÉ ASÍ

Antes de soltar una fila, si es de un comprador seguido, se copia a
`clientes_seguimiento` con el estado al que pasó. La fila deja las tablas de
trabajo —que siguen ligeras— y el historial del cliente no se pierde.

No se hace «dejando de borrar» en las tablas originales por dos razones que no
son de gusto:

  · en `licitaciones` no se puede, porque la tabla se reemplaza entera y no
    borrar algo que sencillamente no viene en los datos nuevos exigiría rehacer
    esa descarga de arriba abajo;
  · `compra_agil` es la copia COMPARTIDA con la que filtran todas las empresas.
    Dejar lo cerrado ahí para siempre la haría crecer sin tope y metería
    cotizaciones muertas en los resultados de todo el mundo.

NADA DE ESTO PUEDE TUMBAR UN BARRIDO

Archivar es un extra sobre el trabajo del barrido, no su trabajo. Todas las
funciones de aquí se tragan sus errores y lo dejan dicho en el log del runner:
que un día no se pueda guardar el historial de un cliente es molesto; que por
eso se caiga la descarga de las tres empresas, no.

Requiere `sql_clientes_historial.sql` corrido, y `36_clientes_barrido.sql` en el
core (de ahí sale `core.compradores_seguidos()`).
"""
import auth
import datos_nube

TABLA = "clientes_seguimiento"

TIPO_COMPRA_AGIL = "compra_agil"
TIPO_LICITACION = "licitacion"

# Lo que se guarda cuando el portal no dice a qué estado pasó.
#
# En Compra Ágil, una cotización que desaparece del listado de abiertas «cerró,
# se adjudicó, se canceló o quedó desierta» —el listado se pide con `status=2` y
# no distingue cuál—. En licitaciones el API no devuelve estado en absoluto. En
# los dos casos lo único cierto es que dejó de estar publicada, y eso es lo que
# se escribe: inventar 'adjudicada' sería más informativo y sería mentira.
ESTADO_POR_DEFECTO = "cerrada"

# Y lo que se guarda cuando ni siquiera eso se puede afirmar.
#
# En licitaciones la tabla NO es «lo que está abierto»: es el último rango de
# fechas que alguien descargó. Una licitación desaparece de ahí en cuanto se pide
# otro rango, y eso no dice nada sobre si cerró — puede seguir abierta y
# recibiendo ofertas. Etiquetarla 'cerrada' sería cómodo y sería falso, y quien
# mire el historial de su cliente tomaría decisiones con un dato inventado.
#
# Así que cuando el estado no consta se mira la fecha de cierre, que sí está en
# la fila: pasada, cerró; futura o ilegible, se dice que no se sabe.
ESTADO_DESCONOCIDO = "sin confirmar"

_TAM_LOTE = 400
_TAM_IN = 100


def _log(mensaje):
    """Al log de la corrida. Igual que en `compra_agil_api`, esto no sube a la
    app: el cliente no tiene por qué enterarse de la contabilidad interna."""
    print(f"[clientes] {mensaje}", flush=True)


# ---------------------------------------------------------------------------
# A quién sigue la gente
# ---------------------------------------------------------------------------

def ruts_seguidos() -> set:
    """Los RUT de compradores que alguien sigue, sin repetir.

    Sale de `core.compradores_seguidos()`, que agrupa `core.marcas` y está
    concedida a service_role. Se pregunta a la base y no a un archivo porque los
    favoritos son de cada persona y viven en la nube: el runner no tiene delante
    el disco de nadie.

    Devuelve un conjunto vacío si algo falla. Un conjunto vacío significa «no
    archives nada», que es exactamente lo que hacía la versión anterior: nunca
    empeora lo que ya había.
    """
    try:
        resp = auth.supabase.schema("core").rpc("compradores_seguidos").execute()
    except Exception as e:
        _log(f"no se pudo saber a quién sigue la gente ({e}); no se archiva nada")
        return set()

    ruts = set()
    for fila in (resp.data or []):
        rut = str(fila.get("rut") or "").strip()
        if rut:
            ruts.add(rut)
    return ruts


# ---------------------------------------------------------------------------
# Qué hay archivado ya
# ---------------------------------------------------------------------------

def _ya_archivados(tipo, codigos) -> set:
    """De esos códigos, los que ya están guardados para ese tipo.

    Evita insertar dos veces lo mismo la noche que un código vuelva a aparecer
    en `a_soltar` (pasa: sale de la ventana de 30 días después de haber cerrado,
    y las dos veces es candidato a archivarse).
    """
    codigos = [str(c) for c in codigos if str(c).strip()]
    if not codigos:
        return set()

    vistos = set()
    for i in range(0, len(codigos), _TAM_IN):
        lote = codigos[i:i + _TAM_IN]
        try:
            resp = datos_nube.supabase.table(TABLA).select("numero_adquisicion") \
                .is_("empresa_id", "null").eq("tipo", tipo) \
                .in_("numero_adquisicion", lote).execute()
        except Exception as e:
            # Ante la duda se da por archivado: repetir una fila del historial
            # ensucia la pantalla del cliente, y perderla no, porque la de la
            # tanda anterior sigue ahí.
            _log(f"no se pudo comprobar lo ya archivado ({e}); se salta ese lote")
            vistos.update(lote)
            continue
        for fila in (resp.data or []):
            vistos.add(str(fila.get("numero_adquisicion") or ""))
    return vistos


# ---------------------------------------------------------------------------
# Archivar
# ---------------------------------------------------------------------------

def archivar(df, codigos, tipo, estados=None, ruts=None,
             estado_defecto=ESTADO_POR_DEFECTO) -> int:
    """Guarda en el historial las filas de `codigos` que sean de un cliente seguido.

    `df` es la tabla tal como está AHORA en la nube, con encabezados de Excel
    (los de `datos_nube.MAPEO_COMPRA_AGIL` o `MAPEO_LICITACIONES`): de ahí salen
    los datos de la fila, porque a punto de borrarse es la última vez que se
    tienen. `codigos` son los que van a soltarse.

    `estados` es {código: estado} para los que el portal SÍ dijo a qué pasaron.
    Para el resto manda `estado_defecto`, y con `None` se deduce de la fecha de
    cierre de cada fila (ver `ESTADO_DESCONOCIDO`).

    `ruts` se puede pasar ya resuelto para no preguntarlo dos veces cuando se
    archivan los dos tipos en la misma corrida.

    -> cuántas filas se guardaron. Nunca lanza.
    """
    try:
        return _archivar(df, codigos, tipo, estados or {}, ruts, estado_defecto)
    except Exception as e:
        _log(f"no se pudo archivar el historial de {tipo} ({e}); el barrido sigue")
        return 0


def _archivar(df, codigos, tipo, estados, ruts, estado_defecto):
    codigos = {str(c) for c in (codigos or []) if str(c).strip()}
    if df is None or df.empty or not codigos:
        return 0

    ruts = ruts_seguidos() if ruts is None else ruts
    if not ruts:
        return 0

    if "RUT Organismo" not in df.columns or "Numero Adquisición" not in df.columns:
        _log(f"la tabla de {tipo} no trae RUT o código; no se archiva")
        return 0

    # Las filas que se van Y son de alguien seguido. El RUT se normaliza igual
    # que en la app (`clientes_api.normalizar_rut`): en las tablas viene con
    # puntos y guion unas veces y pelado otras, y `core.marcas` lo guarda pelado.
    codigo_de = df["Numero Adquisición"].astype(str)
    rut_de = df["RUT Organismo"].map(_rut_plano)
    interesan = df[codigo_de.isin(codigos) & rut_de.isin(ruts)]
    if interesan.empty:
        return 0

    suyos = sorted({str(c) for c in interesan["Numero Adquisición"]})
    saltar = _ya_archivados(tipo, suyos)

    filas = []
    for _, fila in interesan.iterrows():
        codigo = str(fila.get("Numero Adquisición", ""))
        if codigo in saltar:
            continue
        filas.append({
            "empresa_id": None,
            "rut_organismo": _rut_plano(fila.get("RUT Organismo")),
            "organismo": _texto(fila.get("Organismo")),
            "tipo": tipo,
            "numero_adquisicion": codigo,
            "nombre_adquisicion": _texto(fila.get("Nombre Adquisición")),
            "fecha_publicacion": _texto(fila.get("Fecha Publicación")),
            "fecha_cierre": _texto(fila.get("Fecha Cierre")),
            "fecha_cierre_1er_llamado": _texto(fila.get("Fecha Cierre 1er Llamado")),
            "fecha_cierre_2do_llamado": _texto(fila.get("Fecha Cierre 2do Llamado")),
            "llamado": _texto(fila.get("Llamado")),
            "estado": _estado_de(codigo, fila, estados, estado_defecto),
            "cantidad": _texto(fila.get("Cantidad")),
            "descripcion_producto": _texto(fila.get("Descripción Producto")),
            "nombre_producto": _texto(fila.get("Nombre Producto")),
        })

    if not filas:
        return 0

    for i in range(0, len(filas), _TAM_LOTE):
        lote = filas[i:i + _TAM_LOTE]
        datos_nube.supabase.table(TABLA).insert(lote).execute()

    cuantos = len({f["numero_adquisicion"] for f in filas})
    _log(f"{cuantos} {tipo}(s) de clientes seguidos guardadas en el historial "
         f"antes de soltarlas ({len(filas)} filas).")
    return len(filas)


def _estado_de(codigo, fila, estados, estado_defecto) -> str:
    """A qué estado pasó esta fila. Lo que consta antes que lo que se supone."""
    dicho = estados.get(codigo)
    if dicho:
        return str(dicho).strip().lower()
    if estado_defecto:
        return str(estado_defecto).strip().lower()
    return ESTADO_POR_DEFECTO if _ya_cerro(fila.get("Fecha Cierre")) else ESTADO_DESCONOCIDO


def _ya_cerro(fecha) -> bool:
    """¿La fecha de cierre quedó atrás? Con una fecha ilegible, no se afirma.

    Las fechas llegan como texto y en varios formatos según de dónde salga la
    fila; `pandas` los resuelve todos y devuelve `NaT` con lo que no entiende,
    que es justo la respuesta que hace falta aquí.
    """
    texto = _texto(fecha).strip()
    if not texto:
        return False
    try:
        import pandas as pd
        momento = pd.to_datetime(texto, dayfirst=True, errors="coerce")
        if momento is None or pd.isna(momento):
            return False
        return momento < pd.Timestamp.now()
    except Exception:
        return False


def _rut_plano(valor) -> str:
    """'61.606.606-4' -> '616066064'. La llave con la que se cruza todo."""
    return "".join(c for c in str(valor or "").upper() if c.isdigit() or c == "K")


def _texto(valor) -> str:
    if valor is None:
        return ""
    texto = str(valor)
    return "" if texto.lower() == "nan" else texto
