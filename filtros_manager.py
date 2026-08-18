import os
import json
import threading

from auth import supabase
from helpers import _get_appdata_dir

FILTROS_FILE = os.path.join(_get_appdata_dir(), "filtros.json")

DEFAULT_FILTERS = {
    "CONVENIOS": [
        "(convenio, comodato, contrato, contratacion, suministro) (quimica, reactivos, hematologia, bioquimica, orina, uroanalisis, urianalisis, laboratorio, tbc, tuberculosis, muestra)"
    ],
    "ELECTROLITOS": [
        "electrolitos(plasmaticos)",
        "electrodos (referencia, sodio, cloro, potasio)",
        "filling (solution)",
        "refill (solution)"
    ],
    "EQUIPAMIENTO": [
        "cintas(orina)", "dirui", "tiras (orina)", "lector(cintas, tiras)", "urit",
        "autoanaliz*", "contador (hematologico)", "centrifuga",
        "analizador(sangre, sanguinea, plasma, suero, plasmatico)",
        "vhs", "velocidad (*sedimentacion)", "equipo (laboratorio)", "microscopio"
    ],
    "HEMATOLOGIA": ["hematol*"],
    "INSUMOS": ["inmuno", "parasi*", "paf", "teleman*", "pafs", "graham"],
    "MEDIOS DE CULTIVO": [
        "(caldo, medio, agar, placa, tubo) (hinton, conkey, conckey, sangre, columbia, cordero, cromo, chrom*)",
        "lia", "tsi", "mio", "urea", "bilis", "esculina", "thiog*", "tiog*", "glucosa",
        "thayer", "xld", "citrato (tubo)", "simmons", "hinton", "conkey", "conckey",
        "columbia", "cled", "sabouraud (dextrosa)", "dnasa", "nutritivo", "tripticasa*",
        "tcbs", "manitol", "mannitol", "bacteriol*", "agar", "(muller, mueller) (hinton)",
        "chocolate(agar, placa)", "hb&l", "alfred", "uroquick", "uro(quick)"
    ],
    "ORINAS": ["orina", "fus", "urs", "sheath", "(cintas, tiras)(reactivas)"],
    "QUIMICA Y REACTIVOS": [
        "alat", "alt", "transam*", "ck", "creatin*", "gpt", "asat", "ast",
        "glucosa", "glicemia", "trigliceridos", "tg", "rpr", "vdrl", "fofatasa",
        "amilasa", "pcr", "urea", "bilirrubina", "colesterol", "hdl", "Idh",
        "control (suero, bioquimico, nivel, normal, patologico)", "hb1ac",
        "microalbuminuria", "multicalibrador", "alp", "hemoglob*", "mau", "bili",
        "malb", "cholestrol", "bun", "calcemia", "fosfemia", "crp", "nac", "jaffe",
        "ggt", "qualitrol", "labtest", "glicosilada", "reumatoideo", "tromboplastina",
        "cefalina", "protrombina", "coagulometro", "reactivo (coagulacion)",
        "soluplastin", "soloplastin", "triniclot", "reactivo(tp)"
    ]
}

# ---------------------------------------------------------------------------
# Cache en memoria + lock para escrituras concurrentes a Supabase
# ---------------------------------------------------------------------------
_CACHE: dict | None = None
_supabase_lock = threading.Lock()


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def cargar_filtros_json() -> dict:
    """
    Devuelve los filtros al instante desde cache/local.
    En segundo plano, sincroniza con Supabase y actualiza el cache.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    # Primera llamada: carga local de inmediato
    _CACHE = _cargar_local()

    # Inicia sincronización desde Supabase en hilo de fondo
    threading.Thread(target=_pull_supabase, daemon=True).start()

    return _CACHE


def guardar_filtros_json(datos: dict):
    """
    Guarda en cache y en archivo local al instante.
    Sincroniza con Supabase en hilo de fondo.
    """
    global _CACHE
    _CACHE = datos
    _guardar_local(datos)
    threading.Thread(target=_push_supabase, args=(datos,), daemon=True).start()


def obtener_lista_reglas_planas() -> list:
    datos = cargar_filtros_json()
    reglas_planas = []
    for reglas in datos.values():
        reglas_planas.extend(reglas)
    return [r for r in reglas_planas if str(r).strip()]


# ---------------------------------------------------------------------------
# Hilos de fondo — Supabase
# ---------------------------------------------------------------------------

def _empresa_id():
    """Empresa cuyos filtros tocan. None si no se sabe todavía.

    Mira PRIMERO la variable de entorno, igual que `datos_nube.empresa_actual()`:
    en un runner headless no hay archivo de sesión, y sin esto `_pull_supabase`
    salía temprano y se acababa filtrando con DEFAULT_FILTERS —los de Amilab—
    para todas las empresas. El barrido recorre las empresas fijando EMPRESA_ID
    en cada vuelta, así que es la única forma de saber de quién son las reglas.
    """
    env_id = os.environ.get("EMPRESA_ID", "").strip()
    if env_id:
        return env_id

    from helpers import leer_sesion
    return leer_sesion().get("empresa_id")


def _leer_de_supabase(empresa_id: str) -> dict:
    """{categoria: [reglas]} de esa empresa, leído de Supabase. {} si no hay.

    Sin cache y sin hilos: devuelve lo que hay en la nube en este momento, para
    la empresa que se le pida. Es lo que necesita el barrido, que recorre varias
    empresas seguidas y no puede compartir un cache global entre ellas.
    """
    if supabase is None:
        return {}

    cats_resp = (supabase.table("filtros_categorias").select("id, nombre")
                 .eq("empresa_id", empresa_id).order("nombre").execute())
    if not cats_resp.data:
        return {}

    resultado = {}
    for cat in cats_resp.data:
        reglas_resp = (
            supabase.table("filtros_reglas")
            .select("texto")
            .eq("categoria_id", cat["id"])
            .order("orden")
            .execute()
        )
        resultado[cat["nombre"]] = [r["texto"] for r in (reglas_resp.data or [])]
    return resultado


def reglas_planas_de_empresa(empresa_id: str) -> list:
    """Reglas de UNA empresa concreta, leídas de la nube al momento.

    `obtener_lista_reglas_planas` no sirve headless: devuelve el cache global
    —que en un bucle por empresas sería el de la anterior— y sincroniza en un
    hilo de fondo, así que el primer llamador recibe el respaldo local, que en
    un runner recién clonado son los DEFAULT_FILTERS.

    Devuelve [] si la empresa no tiene filtros cargados. Quien llame debe
    distinguir ese caso de "no coincidió nada": filtrar con una lista vacía
    borraría el trabajo de la empresa.
    """
    if not empresa_id:
        return []
    datos = _leer_de_supabase(empresa_id)
    reglas = []
    for lista in datos.values():
        reglas.extend(lista)
    return [r for r in reglas if str(r).strip()]


def _pull_supabase():
    """Lee las categorías y reglas de esta empresa desde Supabase y actualiza el cache."""
    global _CACHE
    if supabase is None:
        return

    empresa_id = _empresa_id()
    if not empresa_id:
        return  # sin sesión no hay filtros que traer; se usa el respaldo local

    try:
        resultado = _leer_de_supabase(empresa_id)
        if not resultado:
            return
        _CACHE = resultado
        _guardar_local(resultado)
    except Exception as e:
        print(f"[filtros] Error sincronizando desde Supabase: {e}")


def _push_supabase(datos: dict):
    """Escribe los filtros en Supabase. Se ejecuta en hilo de fondo."""
    if supabase is None:
        return

    empresa_id = _empresa_id()
    if not empresa_id:
        return  # sin sesión sólo se guarda en local

    with _supabase_lock:
        try:
            # Todas las consultas van acotadas a la empresa: sin el filtro, un
            # cliente borraría las categorías de los demás.
            cats_resp = (supabase.table("filtros_categorias").select("id, nombre")
                         .eq("empresa_id", empresa_id).execute())
            existentes = {c["nombre"]: c["id"] for c in (cats_resp.data or [])}

            # Eliminar categorías borradas
            for nombre_existente, cat_id in existentes.items():
                if nombre_existente not in datos:
                    (supabase.table("filtros_categorias").delete()
                     .eq("id", cat_id).eq("empresa_id", empresa_id).execute())

            # Upsert categorías y reglas
            for nombre, reglas in datos.items():
                if nombre in existentes:
                    cat_id = existentes[nombre]
                    (supabase.table("filtros_reglas").delete()
                     .eq("categoria_id", cat_id).eq("empresa_id", empresa_id).execute())
                else:
                    resp = (supabase.table("filtros_categorias")
                            .insert({"nombre": nombre, "empresa_id": empresa_id}).execute())
                    cat_id = resp.data[0]["id"]

                if reglas:
                    filas = [{"categoria_id": cat_id, "texto": r, "orden": i,
                              "empresa_id": empresa_id}
                             for i, r in enumerate(reglas)]
                    supabase.table("filtros_reglas").insert(filas).execute()

        except Exception as e:
            print(f"[filtros] Error guardando en Supabase: {e}")


# ---------------------------------------------------------------------------
# Fallback local
# ---------------------------------------------------------------------------

def _cargar_local() -> dict:
    if not os.path.exists(FILTROS_FILE):
        _guardar_local(DEFAULT_FILTERS)
        return DEFAULT_FILTERS
    try:
        with open(FILTROS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_FILTERS


def _guardar_local(datos: dict):
    try:
        with open(FILTROS_FILE, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4, ensure_ascii=False)
    except Exception:
        pass
