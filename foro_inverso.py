# -*- coding: utf-8 -*-
"""
foro_inverso.py — Detección de "foros inversos" (aclaraciones de oferta) dirigidos
a Amilab en licitaciones públicas de Mercado Público.

La revisión es SOLO MANUAL: el usuario pega códigos de licitación y pulsa
"Revisar". No hay barrido automático ni polling en segundo plano.

Persistencia (dos tablas en Supabase, compartidas por todos los PCs):
  - foro_inverso_historial : historial completo de revisiones (con o sin
    mención a Amilab). Ventana de 30 días; lo más viejo se poda. Solo lo consulta
    el botón "Revisar historial mensual".
  - foro_inverso_actuales  : solo los hallazgos con mención a Amilab,
    acumulados y podados a 30 días. Lo lee el listado principal y el badge del
    menú (con caché en memoria para no consultar la nube a cada instante).

Flujo de sesión (sin navegador headless):
  1) GET DetailsAcquisition.aspx?idLicitacion=<codigo> -> ficha (cookies + token qs).
  2) Extraer el `enc` fresco del enlace #imgAclaracionOferta.
  3) GET al foro (BuyQuestionAndAnswers.aspx?enc=...) -> shell con `intRBFCode`.
  4) POST a Foros/servicesJS.aspx (opt=3, RFBCode=<intRBFCode>) -> JSON de aclaraciones.
"""
import re
import time
import html as html_mod
import unicodedata
import urllib.parse
import threading
from datetime import datetime, timedelta, timezone

import requests

import config
from auth import supabase, ejecutar_con_refresco

TABLA_ACTUALES = "foro_inverso_actuales"
TABLA_HISTORIAL = "foro_inverso_historial"
VENTANA_DIAS = config.FORO_INVERSO_VENTANA_DIAS

DETALLE_FICHA_URL = "https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx"
SERVICES_JS_URL = "https://www.mercadopublico.cl/Foros/servicesJS.aspx"
HOME_URL = "https://www.mercadopublico.cl/Home"

_HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# Se activa mientras hay una descarga de licitaciones (pantalla_api) en curso. La
# UI lo consulta para deshabilitar botones de foros. Compra Ágil ya no aparece
# aquí: sus barridos corren en el servidor, no en el PC del usuario.
descarga_en_progreso = threading.Event()

_lock_ciclo = threading.Lock()
# Serializa el uso del cliente Supabase entre hilos (lectura de hallazgos en el hilo
# principal vs. escritura de revisión en el worker): evita que se pisen / se cuelguen.
_lock_supabase = threading.Lock()

# Eventos de cancelación por módulo (el usuario pulsa "Cancelar")
_cancelar_foro = threading.Event()
_cancelar_api = threading.Event()

# Progreso compartido de la descarga de licitaciones
# (sobrevive destrucción/recreación del widget para reconexión de barra)
_progreso_api = {"activo": False, "mensaje": "", "actual": 0, "total": 0}


# =============================================================================
# Normalización / detección de Amilab
# =============================================================================
def _normalizar(texto: str) -> str:
    texto = str(texto or "")
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower().strip()


def _variantes_normalizadas():
    """Término(s) de detección: los nombres que la empresa eligió revisar (hasta
    3, ver `nombres_a_revisar`), y si no eligió ninguno, el del registro central.
    Si tampoco hay, cae al config antiguo (`FORO_INVERSO_PROVEEDOR_VARIANTES`)."""
    variantes = variantes_de(nombres_a_revisar())
    if variantes:
        return variantes
    return [_normalizar(v) for v in config.FORO_INVERSO_PROVEEDOR_VARIANTES if str(v).strip()]


def _menciona_amilab(texto: str, variantes_norm) -> bool:
    norm = _normalizar(texto)
    return any(variante in norm for variante in variantes_norm)


# =============================================================================
# Persistencia en Supabase: historial completo + actuales (poda a 30 días)
# =============================================================================
_cache_actuales = {"ts": 0.0, "datos": []}
_CACHE_TTL_SEG = 60  # el badge del menú consulta cada 6 s; no ir a la nube cada vez


def _limite_ventana_iso() -> str:
    return (datetime.now() - timedelta(days=VENTANA_DIAS)).isoformat(timespec="seconds")


def _vigente(iso_str, dias=VENTANA_DIAS) -> bool:
    """True si la fecha ISO cae dentro de los últimos `dias` días. Si no hay fecha
    o no se puede parsear, se conserva (True) para no perder datos por las dudas."""
    if not iso_str:
        return True
    try:
        f = datetime.fromisoformat(str(iso_str)[:19])
    except Exception:
        return True
    return (datetime.now() - f).days <= dias


def _empresa_id():
    """Empresa de la sesión — o de EMPRESA_ID del entorno cuando corre headless
    (runner nocturno en GitHub, sin sesión). Los foros son de cada empresa."""
    import os
    env_id = os.environ.get("EMPRESA_ID", "").strip()
    if env_id:
        return env_id
    from helpers import leer_sesion
    return leer_sesion().get("empresa_id")


def cargar_historial() -> list:
    """Revisiones del historial completo (tabla foro_inverso_historial), podadas
    a la ventana de 30 días. Cada elemento: {codigo, url_ficha, hallazgos,
    tiene_foro, tiene_aclaraciones, error, fecha_revision}.
    Solo lo usa 'Revisar historial mensual'."""
    if supabase is None:
        return []

    empresa_id = _empresa_id()
    if not empresa_id:
        return []

    try:
        resp = ejecutar_con_refresco(lambda: supabase.table(TABLA_HISTORIAL)
                .select("codigo,cliente,url_ficha,hallazgos,tiene_foro,tiene_aclaraciones,error,fecha_revision")
                .eq("empresa_id", empresa_id)
                .order("fecha_revision")
                .execute())
        return [r for r in (resp.data or []) if _vigente(r.get("fecha_revision"))]
    except Exception:
        return []


def obtener_hallazgos() -> list:
    """Hallazgos actuales (mención a Amilab) desde la tabla foro_inverso_actuales,
    podados a 30 días. Los lee el listado principal y el badge del menú.
    Usa un caché en memoria (60 s) para no consultar la nube constantemente."""
    ahora = time.monotonic()
    if ahora - _cache_actuales["ts"] < _CACHE_TTL_SEG:
        return list(_cache_actuales["datos"])

    if supabase is None:
        return list(_cache_actuales["datos"])

    empresa_id = _empresa_id()
    if not empresa_id:
        return list(_cache_actuales["datos"])

    try:
        with _lock_supabase:
            resp = ejecutar_con_refresco(lambda: supabase.table(TABLA_ACTUALES)
                    .select("codigo,cliente,idp,pregunta,fecha_pregunta,pendiente,url_ficha,fecha_deteccion")
                    .eq("empresa_id", empresa_id)
                    .order("fecha_deteccion")
                    .execute())
        datos = [
            {**h, "idP": h.pop("idp", None)}
            for h in (resp.data or [])
            if _vigente(h.get("fecha_deteccion"))
        ]
        _cache_actuales["datos"] = datos
        _cache_actuales["ts"] = ahora
        return list(datos)
    except Exception:
        return list(_cache_actuales["datos"])


def _invalidar_cache_actuales():
    _cache_actuales["ts"] = 0.0


def obtener_count_total_hallazgos() -> dict:
    """Cuenta de hallazgos actuales para el badge. {"total": N, "pendientes": M}."""
    hs = obtener_hallazgos()
    return {"total": len(hs), "pendientes": sum(1 for h in hs if h.get("pendiente"))}


def _registrar_revision(resultados):
    """Guarda en foro_inverso_actuales SOLO los hallazgos con mención a la empresa
    (dedup por código, poda a 30 días). Ya NO se usa foro_inverso_historial: la
    lista de licitaciones a revisar vive ahora en `foro_ofertadas`.
    Best-effort: si la nube no responde, la revisión igual se muestra en pantalla."""
    if supabase is None or not resultados:
        return

    empresa_id = _empresa_id()
    if not empresa_id:
        return

    ahora = datetime.now().isoformat(timespec="seconds")
    codigos = [str(r.get("codigo")) for r in resultados if r.get("codigo")]
    limite = _limite_ventana_iso()

    _lock_supabase.acquire()
    try:
        # --- Actuales: reemplazar los hallazgos de estos códigos + poda ---
        ejecutar_con_refresco(lambda: supabase.table(TABLA_ACTUALES).delete()
         .eq("empresa_id", empresa_id).in_("codigo", codigos).execute())
        ejecutar_con_refresco(lambda: supabase.table(TABLA_ACTUALES).delete()
         .eq("empresa_id", empresa_id).lt("fecha_deteccion", limite).execute())
        filas_act = []
        for r in resultados:
            codigo = str(r.get("codigo", ""))
            cliente = r.get("cliente", "") or ""
            url = r.get("url_ficha", "") or ""
            for h in (r.get("hallazgos") or []):
                filas_act.append({
                    "empresa_id": empresa_id,
                    "codigo": codigo,
                    "cliente": cliente,
                    "idp": "" if h.get("idP") is None else str(h.get("idP")),
                    "pregunta": h.get("pregunta", ""),
                    "fecha_pregunta": h.get("fecha_pregunta", ""),
                    "pendiente": bool(h.get("pendiente")),
                    "url_ficha": url,
                    "fecha_deteccion": ahora,
                })
        if filas_act:
            ejecutar_con_refresco(lambda: supabase.table(TABLA_ACTUALES).insert(filas_act).execute())
    except Exception:
        pass
    finally:
        _lock_supabase.release()
        _invalidar_cache_actuales()


def revision_en_curso() -> bool:
    """True si hay una revisión de foros corriendo en este momento."""
    if _lock_ciclo.acquire(blocking=False):
        _lock_ciclo.release()
        return False
    return True


def hay_revision_foro_activa() -> bool:
    """True si hay una revisión de foros (importada) en curso."""
    return _progreso_importacion["activo"]


# ---------------------------------------------------------------------------
# API pública para que pantalla_api registre su progreso
# (permite que la UI reconecte la barra al volver a la pantalla)
# ---------------------------------------------------------------------------

def obtener_progreso_api() -> dict:
    return dict(_progreso_api)


def iniciar_descarga_api():
    _cancelar_api.clear()
    _progreso_api.update({"activo": True, "mensaje": "Iniciando descarga...", "actual": 0, "total": 100})
    descarga_en_progreso.set()


def actualizar_progreso_api(mensaje, actual, total=None):
    _progreso_api["mensaje"] = mensaje
    _progreso_api["actual"] = actual
    if total is not None:
        _progreso_api["total"] = total


def finalizar_descarga_api():
    _progreso_api.update({"activo": False, "mensaje": "", "actual": 0, "total": 0})
    descarga_en_progreso.clear()


def cancelar_revision_foro():
    """Señala a la revisión de foros que debe detenerse."""
    _cancelar_foro.set()


# =============================================================================
# Flujo de sesión: código -> aclaraciones (JSON)
# =============================================================================
def _crear_sesion() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS_BASE)
    return s


def _extraer_intRBFCode(texto_html):
    m = re.search(r'intRBFCode\s*=\s*"(\d+)"', texto_html)
    return m.group(1) if m else None


def _post_servicios_js(sesion, rbf_code, referer):
    headers = {
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    resp = sesion.post(SERVICES_JS_URL, data={"opt": 3, "RFBCode": rbf_code}, headers=headers, timeout=20)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return []


_RUT_COMPRADOR_RE = re.compile(r'https://comprador\.mercadopublico\.cl/ficha/[\d.\-Kk]+')
# Razón social del organismo comprador en la ficha (columna "Cliente" en la UI)
_CLIENTE_RE = re.compile(r'id="lnkFicha2Razon"[^>]*>([^<]+)<')
# Título y estado, del mismo HTML de la ficha. Salen en UNA petición que ya se
# hacía, y sin ticket de Mercado Público: por eso la app puede mantener la lista
# al día aunque la empresa no tenga ticket propio (sólo Amilab tiene uno).
_NOMBRE_RE = re.compile(r'id="lblFicha1Nombre"[^>]*>([^<]*)<')
_ESTADO_RE = re.compile(r'id="lblFicha1Estado"[^>]*>([^<]*)<')


def esta_cerrada(estado) -> bool:
    """¿La licitación está CERRADA, o sea en evaluación?

    Es el único estado con foro inverso vivo: las preguntas del comprador a un
    oferente ocurren mientras evalúa. Antes de cerrar no hay a quién preguntarle,
    y después de adjudicar ya no hay plazo para contestar."""
    return "cerrad" in _normalizar(estado)


def es_irreversible(estado) -> bool:
    """¿De este estado ya no se vuelve a «Cerrada»?

    Adjudicada, desierta, revocada o cancelada: el proceso terminó. Ésas salen
    de la lista de ofertadas, porque revisar su foro es gastar peticiones en algo
    que ya nadie podría responder a tiempo."""
    e = _normalizar(estado)
    return any(k in e for k in ("adjudicad", "desiert", "revocad", "cancelad"))


def resolver_urls_licitacion(codigo):
    """Resuelve, vía la ficha pública de la licitación, la URL final con el token
    `qs` (para enlazar directo a la licitación) y la URL del perfil del organismo
    comprador. Devuelve (url_ficha, url_comprador); cualquiera puede venir vacía
    ("") si no se pudo resolver."""
    try:
        sesion = _crear_sesion()
        resp = sesion.get(DETALLE_FICHA_URL, params={"idLicitacion": codigo}, timeout=25)
        resp.raise_for_status()
        url_ficha = resp.url
        m = _RUT_COMPRADOR_RE.search(resp.text)
        url_comprador = m.group(0) if m else ""
        return url_ficha, url_comprador
    except Exception:
        return "", ""


def datos_ficha(sesion, codigo) -> dict:
    """Lo que dice la ficha pública de una licitación, en UNA petición.

    -> {codigo, nombre, estado, cliente, url_ficha, foro_url}

    `foro_url` vacío significa que la ficha no ofrece sección de «Aclaración de
    oferta»: es lo que la pantalla muestra como «Foro abierto: No».

    Va aparte del foro a propósito. El estado es lo que decide si hay algo que
    revisar, y preguntarlo cuesta esta sola petición; entrar al foro de una
    licitación que ni siquiera ha cerrado son dos peticiones tiradas."""
    resp = sesion.get(DETALLE_FICHA_URL, params={"idLicitacion": codigo}, timeout=25)
    resp.raise_for_status()
    html_ficha = resp.text

    def _campo(rx):
        m = rx.search(html_ficha)
        return html_mod.unescape(m.group(1)).strip() if m else ""

    m_foro = re.search(r'id="imgAclaracionOferta"[^>]*href="([^"]+)"', html_ficha)
    return {
        "codigo": codigo,
        "nombre": _campo(_NOMBRE_RE),
        "estado": _campo(_ESTADO_RE),
        "cliente": _campo(_CLIENTE_RE),
        "url_ficha": resp.url,
        "foro_url": (urllib.parse.urljoin(DETALLE_FICHA_URL, html_mod.unescape(m_foro.group(1)))
                     if m_foro else ""),
    }


def aclaraciones_de(sesion, ficha):
    """Las aclaraciones del foro de esa licitación. -> (aclaraciones, intRBFCode)

    `intRBFCode` en None significa que no hay foro que leer: ni enlace en la
    ficha, ni código dentro de la página del foro."""
    if not ficha.get("foro_url"):
        return [], None
    resp_foro = sesion.get(ficha["foro_url"],
                           headers={"Referer": ficha.get("url_ficha") or DETALLE_FICHA_URL},
                           timeout=25)
    resp_foro.raise_for_status()
    rbf_code = _extraer_intRBFCode(resp_foro.text)
    if not rbf_code:
        return [], None
    return _post_servicios_js(sesion, rbf_code, referer=resp_foro.url), rbf_code


def _resolver_via_ficha(sesion, codigo):
    """Flujo completo: ficha -> foro -> JSON.
    -> (aclaraciones, intRBFCode, url_ficha, cliente, nombre, estado)"""
    ficha = datos_ficha(sesion, codigo)
    aclaraciones, rbf_code = aclaraciones_de(sesion, ficha)
    return (aclaraciones, rbf_code, ficha["url_ficha"], ficha["cliente"],
            ficha["nombre"], ficha["estado"])


def mensajes_de(codigo) -> dict:
    """TODO el foro de una licitación, tal como está AHORA en el portal.

    Lo usa la ventana de «Mensajes»: se consulta en vivo al abrirla y no se
    guarda. Así se ven también las preguntas dirigidas a otros oferentes y las
    respuestas que llegaron después de la última revisión.

    -> {codigo, nombre, cliente, estado, url_ficha, aclaraciones, error}
    """
    try:
        sesion = _crear_sesion()
        ficha = datos_ficha(sesion, codigo)
        aclaraciones, _ = aclaraciones_de(sesion, ficha)
        ficha["aclaraciones"] = aclaraciones
        ficha["error"] = None
        return ficha
    except Exception as e:
        return {"codigo": codigo, "nombre": "", "cliente": "", "estado": "",
                "url_ficha": "", "aclaraciones": [], "error": str(e)}


# =============================================================================
# Análisis de aclaraciones: ¿involucran a Amilab? ¿están pendientes?
# =============================================================================
def _analizar_aclaraciones(aclaraciones, variantes_norm):
    """Devuelve la lista de aclaraciones donde Amilab está involucrada (como
    destinatario o como proveedor que respondió), con su estado pendiente/respondida."""
    hallazgos = []
    for acl in aclaraciones:
        idP = acl.get("idP")
        proveedor_raiz = acl.get("Proveedor", "")
        respuestas = acl.get("Respuesta") or []
        provs_resp = [r.get("Proveedor", "") for r in respuestas]
        provs_resp.append(acl.get("ProvResp", ""))

        involucra = _menciona_amilab(proveedor_raiz, variantes_norm) or \
            any(_menciona_amilab(p, variantes_norm) for p in provs_resp)
        if not involucra:
            continue

        amilab_ya_respondio = any(
            _menciona_amilab(r.get("Proveedor", ""), variantes_norm) and str(r.get("Descripcion", "")).strip()
            for r in respuestas
        )
        pendiente = bool(acl.get("ATiempoResponder")) and not amilab_ya_respondio

        hallazgos.append({
            "idP": idP,
            "pregunta": str(acl.get("Nombre", "")).strip(),
            "fecha_pregunta": acl.get("FechaPregunta", ""),
            "proveedor": proveedor_raiz,
            "pendiente": pendiente,
        })
    return hallazgos


# =============================================================================
# Revisión manual de una lista de licitaciones pegada por el usuario
# (única forma de detectar foros inversos: no hay barrido automático)
# =============================================================================
_progreso_importacion = {"activo": False, "mensaje": "", "actual": 0, "total": 0, "resultados": []}


def obtener_progreso_importacion() -> dict:
    """Snapshot del estado de la revisión en curso (activo, mensaje, actual, total,
    resultados de la corrida actual) para que la UI reconecte la barra al navegar."""
    return dict(_progreso_importacion)


def crear_sesion_publica():
    """Sesión HTTP nueva (para el runner nocturno headless)."""
    return _crear_sesion()


def variantes_de(nombres):
    """Variantes de detección (normalizadas) a partir de uno o varios nombres.

    Acepta un texto o una lista. Son varios porque el nombre con el que una
    empresa está dada de alta aquí no tiene por qué ser la razón social exacta
    con la que Mercado Público la escribe en el foro, y con el nombre equivocado
    la revisión no encuentra nada y parece que no hay menciones. Cada empresa
    elige hasta tres (tabla `foro_nombres`)."""
    if isinstance(nombres, str):
        nombres = [nombres]
    fuera, vistos = [], set()
    for nombre in (nombres or []):
        n = _normalizar(nombre)
        if n and n not in vistos:
            vistos.add(n)
            fuera.append(n)
    return fuera


def revisar_codigo(sesion, codigo, variantes_norm):
    """Revisa UN código y devuelve el resultado (mismo formato que la revisión
    manual). No registra nada: el llamador acumula y llama `registrar_revision`."""
    try:
        ficha = datos_ficha(sesion, codigo)
        return revisar_con_ficha(sesion, ficha, variantes_norm)
    except Exception as e:
        return {
            "codigo": codigo, "cliente": "", "url_ficha": "", "hallazgos": [],
            "tiene_foro": False, "tiene_aclaraciones": False, "error": str(e),
            "nombre": "", "estado": "",
        }


def revisar_con_ficha(sesion, ficha, variantes_norm):
    """Como `revisar_codigo`, pero con la ficha ya pedida.

    Es lo que usan el barrido nocturno y la revisión manual, que miran el estado
    antes de decidir si vale la pena entrar al foro."""
    try:
        aclaraciones, rbf_code = aclaraciones_de(sesion, ficha)
        hallazgos = _analizar_aclaraciones(aclaraciones, variantes_norm)
        error = None
    except Exception as e:
        aclaraciones, rbf_code, hallazgos, error = [], None, [], str(e)
    return {
        "codigo": ficha.get("codigo", ""), "cliente": ficha.get("cliente", ""),
        "url_ficha": ficha.get("url_ficha", ""), "hallazgos": hallazgos,
        "tiene_foro": rbf_code is not None,
        "tiene_aclaraciones": len(aclaraciones) > 0, "error": error,
        "nombre": ficha.get("nombre", ""), "estado": ficha.get("estado", ""),
    }


def olvidar_hallazgos(codigos):
    """Borra de la lista principal las menciones de esas licitaciones. -> cuántas.

    Se usa cuando una licitación deja de estar en evaluación: adjudicada,
    desierta o revocada. Ahí ya no hay nada que responder, así que sus preguntas
    no pintan nada en una lista que existe para no dejar sin contestar lo que
    todavía se puede contestar.

    Sin esto se quedaban hasta 30 días —lo que tarda la poda por antigüedad— y
    la lista mezclaba lo que urge con lo que ya no se puede hacer.
    """
    codigos = [str(c).strip() for c in dict.fromkeys(codigos or []) if str(c).strip()]
    if supabase is None or not codigos:
        return 0
    empresa_id = _empresa_id()
    if not empresa_id:
        return 0

    _lock_supabase.acquire()
    try:
        for i in range(0, len(codigos), 100):
            lote = codigos[i:i + 100]
            ejecutar_con_refresco(lambda lote=lote: supabase.table(TABLA_ACTUALES)
                                  .delete().eq("empresa_id", empresa_id)
                                  .in_("codigo", lote).execute())
        return len(codigos)
    except Exception as e:
        print(f"  aviso: no se pudieron olvidar las menciones: {e}", flush=True)
        return 0
    finally:
        _lock_supabase.release()


# =============================================================================
# Con qué nombres se busca a la empresa, y en qué quedó cada ofertada
# =============================================================================

def nombres_a_revisar() -> list:
    """Nombres con los que se busca a la empresa en los foros. Hasta 3.

    Antes se usaba sólo el nombre del registro central, y eso fallaba callado:
    en Mercado Público la empresa aparece con su razón social exacta, que puede
    no ser el nombre con el que está dada de alta aquí. Con el nombre equivocado
    la revisión no encuentra nada y parece que no hay menciones.

    Si la empresa no eligió ninguno, se usa el del registro central."""
    nombre_core = ""
    try:
        import empresa_config
        nombre_core = (empresa_config.nombre_empresa_actual() or "").strip()
    except Exception:
        pass

    empresa_id = _empresa_id()
    if supabase is None or not empresa_id:
        return [nombre_core] if nombre_core else []
    try:
        resp = ejecutar_con_refresco(
            lambda: supabase.table("foro_nombres").select("nombres")
            .eq("empresa_id", empresa_id).limit(1).execute())
        filas = resp.data or []
        guardados = [str(n).strip()
                     for n in ((filas[0].get("nombres") if filas else None) or [])
                     if str(n).strip()]
        if guardados:
            return guardados[:3]
    except Exception as e:
        print(f"  aviso: no se pudieron leer los nombres a revisar: {e}", flush=True)
    return [nombre_core] if nombre_core else []


def guardar_nombres_a_revisar(nombres) -> list:
    """Guarda hasta 3 nombres (sin repetir) y devuelve los que quedaron."""
    limpios = []
    for n in (nombres or []):
        n = " ".join(str(n).split())
        if n and n.lower() not in [x.lower() for x in limpios]:
            limpios.append(n)
    limpios = limpios[:3]

    empresa_id = _empresa_id()
    if supabase is None or not empresa_id:
        raise RuntimeError("No hay sesión para guardar los nombres.")
    ejecutar_con_refresco(
        lambda: supabase.table("foro_nombres").upsert(
            {"empresa_id": empresa_id, "nombres": limpios,
             "actualizado": datetime.now(timezone.utc).isoformat()},
            on_conflict="empresa_id").execute())
    return limpios


def resumen_de_revision(ficha, resultado, nombres=None) -> dict:
    """En qué quedó una licitación. -> {estado_foro, menciones, pendientes}

    `estado_foro` es la frase que lee el cliente en la tabla. Se redacta aquí y
    no en la pantalla porque la escriben los dos caminos —la revisión nocturna y
    la manual— y tienen que decir exactamente lo mismo."""
    nombre_empresa = (nombres or [])[0] if (nombres or []) else "su empresa"
    estado = ficha.get("estado", "") if isinstance(ficha, dict) else ""

    if resultado is None:
        # No se revisó el foro: o no ha cerrado, o el proceso terminó.
        if es_irreversible(estado):
            return {"estado_foro": "Proceso terminado", "menciones": 0, "pendientes": 0}
        return {"estado_foro": "Aún no cierra: sin foro que revisar",
                "menciones": 0, "pendientes": 0}

    if resultado.get("error"):
        return {"estado_foro": "No se pudo revisar", "menciones": 0, "pendientes": 0}
    if not resultado.get("tiene_foro"):
        return {"estado_foro": "Sin foro de aclaraciones", "menciones": 0, "pendientes": 0}

    hallazgos = resultado.get("hallazgos") or []
    if not hallazgos:
        return {"estado_foro": f"Sin menciones a {nombre_empresa}",
                "menciones": 0, "pendientes": 0}

    pendientes = sum(1 for h in hallazgos if h.get("pendiente"))
    if pendientes:
        texto = ("1 mención sin responder" if pendientes == 1
                 else f"{pendientes} menciones sin responder")
    else:
        texto = ("1 mención respondida" if len(hallazgos) == 1
                 else f"{len(hallazgos)} menciones respondidas")
    return {"estado_foro": texto, "menciones": len(hallazgos), "pendientes": pendientes}


def actualizar_ofertadas(fichas, resultados=None, nombres=None) -> int:
    """Guarda en `foro_ofertadas` lo que se vio de cada licitación.

    Es lo que llena las columnas de la tabla principal: título, estado, si tiene
    foro y en qué quedó la revisión. Lo escriben tanto el barrido nocturno como
    la revisión manual, con esta misma función, para que la pantalla diga lo
    mismo venga de donde venga.

    Best-effort: si la nube no responde, lo revisado ya se guardó aparte."""
    if supabase is None or not fichas:
        return 0
    empresa_id = _empresa_id()
    if not empresa_id:
        return 0

    nombres = nombres or nombres_a_revisar()
    por_codigo = {str(r.get("codigo")): r for r in (resultados or []) if r.get("codigo")}
    ahora = datetime.now(timezone.utc).isoformat()

    filas = []
    for ficha in fichas:
        codigo = str(ficha.get("codigo") or "").strip()
        if not codigo:
            continue
        resumen = resumen_de_revision(ficha, por_codigo.get(codigo), nombres)
        filas.append({
            "empresa_id": empresa_id,
            "codigo": codigo,
            "nombre": ficha.get("nombre") or "",
            "estado": ficha.get("estado") or "",
            "foro_abierto": bool(ficha.get("foro_url")),
            "estado_foro": resumen["estado_foro"],
            "menciones": resumen["menciones"],
            "pendientes": resumen["pendientes"],
            "ultima_revision": ahora,
        })

    try:
        for i in range(0, len(filas), 100):
            lote = filas[i:i + 100]
            ejecutar_con_refresco(
                lambda lote=lote: supabase.table("foro_ofertadas")
                .upsert(lote, on_conflict="empresa_id,codigo").execute())
    except Exception as e:
        print(f"  aviso: no se pudo actualizar la lista de ofertadas: {e}", flush=True)
        return 0
    return len(filas)


def registrar_revision(resultados):
    """Acumula resultados en historial + actuales para la empresa actual
    (EMPRESA_ID del entorno o la sesión). Público para el runner nocturno."""
    _registrar_revision(resultados)


def revisar_licitaciones_importadas(codigos, callback_resultado=None, callback_progreso=None):
    """Revisa una lista de códigos de licitación (pegados por el usuario) buscando
    aclaraciones dirigidas a Amilab. Al terminar, acumula los resultados en el
    historial y en actuales (ver _registrar_revision).

    callback_resultado(resultados) recibe la lista completa; cada elemento es
    {"codigo", "url_ficha", "hallazgos", "tiene_foro", "tiene_aclaraciones", "error"}.
    """
    _progreso_importacion["activo"] = True
    _progreso_importacion["mensaje"] = "Iniciando revisión de licitaciones..."
    _progreso_importacion["actual"] = 0
    _progreso_importacion["total"] = len(codigos)
    _progreso_importacion["resultados"] = []

    def _ejecutar():
        if not _lock_ciclo.acquire(blocking=False):
            _progreso_importacion["mensaje"] = "⏸ Ya hay una revisión en curso, espera a que termine."
            _progreso_importacion["activo"] = False
            if callback_progreso:
                callback_progreso(_progreso_importacion["mensaje"], 0, 0)
            if callback_resultado:
                callback_resultado([])
            return

        _cancelar_foro.clear()
        try:
            variantes_norm = _variantes_normalizadas()
            sesion = _crear_sesion()
            resultados = []
            total = len(codigos)
            renovar_cada = max(50, config.FORO_INVERSO_RENOVAR_SESION_CADA)

            for i, codigo in enumerate(codigos, start=1):
                if _cancelar_foro.is_set():
                    mensaje = "🚫 Revisión cancelada por el usuario."
                    _progreso_importacion["mensaje"] = mensaje
                    if callback_progreso:
                        callback_progreso(mensaje, i - 1, total)
                    break

                if i > 1 and (i - 1) % renovar_cada == 0:
                    sesion = _crear_sesion()

                mensaje = f"Revisando licitación {i} de {total} ({codigo})..."
                _progreso_importacion["mensaje"] = mensaje
                _progreso_importacion["actual"] = i
                if callback_progreso:
                    callback_progreso(mensaje, i, total)

                try:
                    (aclaraciones, rbf_code, url_ficha, cliente,
                     _nombre, _estado) = _resolver_via_ficha(sesion, codigo)
                    hallazgos = _analizar_aclaraciones(aclaraciones, variantes_norm)
                    resultados.append({
                        "codigo": codigo,
                        "cliente": cliente,
                        "url_ficha": url_ficha,
                        "hallazgos": hallazgos,
                        "tiene_foro": rbf_code is not None,
                        "tiene_aclaraciones": len(aclaraciones) > 0,
                        "error": None,
                    })
                except Exception as e:
                    resultados.append({
                        "codigo": codigo,
                        "cliente": "",
                        "url_ficha": "",
                        "hallazgos": [],
                        "tiene_foro": False,
                        "tiene_aclaraciones": False,
                        "error": str(e),
                    })

                _progreso_importacion["resultados"] = list(resultados)

                if i < total:
                    time.sleep(config.FORO_INVERSO_PAUSA_SEGUNDOS)

            mensaje_final = "✅ Revisión de licitaciones completa."
            _progreso_importacion["mensaje"] = mensaje_final
            if callback_progreso:
                callback_progreso(mensaje_final, total, total)
        finally:
            _lock_ciclo.release()
            _progreso_importacion["activo"] = False
            # Acumular en historial + actuales (solo si hubo algo revisado)
            if _progreso_importacion["resultados"]:
                _registrar_revision(_progreso_importacion["resultados"])
            if callback_resultado:
                callback_resultado(_progreso_importacion["resultados"])

    threading.Thread(target=_ejecutar, daemon=True).start()
