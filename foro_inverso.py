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
from datetime import datetime, timedelta

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
    """Término(s) de detección = NOMBRE de la empresa actual (sesión o EMPRESA_NOMBRE
    del entorno). Multi-empresa: cada una detecta el suyo. Si no hay nombre, cae al
    config antiguo (`FORO_INVERSO_PROVEEDOR_VARIANTES`)."""
    import empresa_config
    n = _normalizar(empresa_config.nombre_empresa_actual())
    if n:
        return [n]
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


def _resolver_via_ficha(sesion, codigo):
    """Flujo completo: ficha -> enc -> foro -> intRBFCode -> JSON.
    Devuelve (aclaraciones, intRBFCode, url_ficha, cliente). intRBFCode puede ser
    None si la licitación no tiene sección de "Aclaración de oferta"; `cliente`
    es la razón social del organismo comprador (puede venir vacía)."""
    resp_ficha = sesion.get(DETALLE_FICHA_URL, params={"idLicitacion": codigo}, timeout=25)
    resp_ficha.raise_for_status()
    url_ficha = resp_ficha.url
    html_ficha = resp_ficha.text

    m_cli = _CLIENTE_RE.search(html_ficha)
    cliente = html_mod.unescape(m_cli.group(1)).strip() if m_cli else ""

    m = re.search(r'id="imgAclaracionOferta"[^>]*href="([^"]+)"', html_ficha)
    if not m:
        return [], None, url_ficha, cliente

    raw_href = html_mod.unescape(m.group(1))
    foro_url = urllib.parse.urljoin(DETALLE_FICHA_URL, raw_href)

    resp_foro = sesion.get(foro_url, headers={"Referer": url_ficha}, timeout=25)
    resp_foro.raise_for_status()

    rbf_code = _extraer_intRBFCode(resp_foro.text)
    if not rbf_code:
        return [], None, url_ficha, cliente

    aclaraciones = _post_servicios_js(sesion, rbf_code, referer=resp_foro.url)
    return aclaraciones, rbf_code, url_ficha, cliente


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


def variantes_de(nombre):
    """Variantes de detección (normalizadas) a partir del nombre de la empresa.
    En el barrido nocturno se detecta por el nombre del registro central."""
    n = _normalizar(nombre)
    return [n] if n else []


def revisar_codigo(sesion, codigo, variantes_norm):
    """Revisa UN código y devuelve el resultado (mismo formato que la revisión
    manual). No registra nada: el llamador acumula y llama `registrar_revision`."""
    try:
        aclaraciones, rbf_code, url_ficha, cliente = _resolver_via_ficha(sesion, codigo)
        hallazgos = _analizar_aclaraciones(aclaraciones, variantes_norm)
        return {
            "codigo": codigo, "cliente": cliente, "url_ficha": url_ficha,
            "hallazgos": hallazgos, "tiene_foro": rbf_code is not None,
            "tiene_aclaraciones": len(aclaraciones) > 0, "error": None,
        }
    except Exception as e:
        return {
            "codigo": codigo, "cliente": "", "url_ficha": "", "hallazgos": [],
            "tiene_foro": False, "tiene_aclaraciones": False, "error": str(e),
        }


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
                    aclaraciones, rbf_code, url_ficha, cliente = _resolver_via_ficha(sesion, codigo)
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
