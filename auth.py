import os
import json
import threading
import time
import requests
from supabase import create_client, Client

from helpers import (_get_appdata_dir, leer_sesion, leer_sesion_cruda,
                     obtener_id_pc, ruta_sesion)

# Las credenciales pueden venir por variable de entorno (p. ej. GitHub Secrets en
# el runner nocturno headless) y, si no están, caen a los valores hardcodeados que
# usa la app de escritorio. Así el mismo código sirve para la app y para el cron.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://tbvqpsearfgazjnqvrng.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_l5D1VcgQjUCk7hfhgxm3eA_Asu5pRNx")

# La app NO envia correos. El codigo de acceso lo manda Supabase Auth desde el
# servidor (`enviar_codigo_acceso` -> sign_in_with_otp), con el SMTP que tenga
# configurado el proyecto. Aqui hubo una clave de Brevo escrita a mano que
# alimentaba un `enviar_correo_codigo` que dejo de usarse en eac2dc0
# (28-07-2026); se elimino con la funcion. No hace falta ninguna credencial de
# correo dentro del .exe.

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception:
    supabase = None

SESION_FILE = ruta_sesion()
ROL_FILE = os.path.join(_get_appdata_dir(), "rol_dispositivo.json")

# Código de esta app en el catálogo del registro central (core.apps.codigo).
CODIGO_APP = "filt"

# `obtener_id_pc` vive ahora en helpers, para que gui.py pueda leer la sesión
# sin arrastrar supabase. Se re-exporta aquí porque otros módulos lo importan
# desde auth.


# =============================================================================
# Autorización de acceso
#
# La fuente de verdad es el registro central (schema `core` del proyecto
# Supabase), administrado desde el panel Themein. Sustituye a la antigua tabla
# `lista_blanca`, que sólo guardaba correos sueltos.
#
# Esta app no usa Supabase Auth: se conecta con la clave pública (rol `anon`) y
# entra por correo + código. Por eso no puede leer las tablas de `core`, que
# están cerradas por RLS. Lo único que tiene permitido es preguntar por un
# correo concreto a través de core.verificar_acceso_app(), que responde si
# puede entrar y con qué rol.
# =============================================================================

MOTIVOS = {
    "sin_licencia": "Tu correo no cuenta con una licencia activa.",
    "usuario_inactivo": "Tu usuario está desactivado. Contacta al administrador.",
    "sin_empresa": "Tu usuario no está asociado a ninguna empresa.",
    "empresa_suspendida": "El servicio de tu empresa está suspendido.",
    "app_no_asignada": "Tu empresa no tiene contratada esta aplicación.",
    "app_suspendida": "El acceso de tu empresa a esta aplicación está suspendido.",
    "datos_incompletos": "Faltan datos para verificar el acceso.",
    "sin_conexion": "No hay conexión con la base de datos.",
    "configuracion": ("El registro central no está disponible. Falta ejecutar las migraciones "
                      "SQL (06_acceso_apps.sql) o exponer el schema `core` en la API."),
}

# Motivos que NO son una negativa al usuario, sino un fallo del entorno. Ante
# ellos hay que respetar la sesión local: no poder preguntar no es un "no".
MOTIVOS_TECNICOS = ("sin_conexion", "configuracion")


def es_fallo_tecnico(resultado: dict) -> bool:
    return resultado.get("motivo") in MOTIVOS_TECNICOS


def verificar_acceso(email: str) -> dict:
    """Pregunta al registro central si `email` puede usar esta app.

    Devuelve el dict que responde la función de base de datos:
        {"autorizado": True,  "rol": ..., "email": ..., "empresa": ...}
        {"autorizado": False, "motivo": "sin_licencia"}

    Cuando no hay conexión devuelve motivo 'sin_conexion', que el llamante debe
    distinguir de una negativa real: sin red no se puede afirmar que alguien
    NO tenga acceso.
    """
    if supabase is None:
        return {"autorizado": False, "motivo": "sin_conexion"}

    try:
        resp = supabase.schema("core").rpc(
            "verificar_acceso_app",
            {"p_email": (email or "").strip().lower(), "p_codigo_app": CODIGO_APP},
        ).execute()
    except Exception as e:
        # Un fallo de configuración (función ausente, schema sin exponer, sin
        # permisos) se distingue de un corte de red: el mensaje que ve el
        # usuario es distinto y el diagnóstico, mucho más rápido.
        detalle = f"{getattr(e, 'code', '')} {e}"
        if any(c in detalle for c in ("PGRST202", "PGRST106", "PGRST205", "42501", "42883", "42P01")):
            return {"autorizado": False, "motivo": "configuracion"}
        return {"autorizado": False, "motivo": "sin_conexion"}

    datos = resp.data if isinstance(resp.data, dict) else {}
    return datos or {"autorizado": False, "motivo": "sin_licencia"}


def texto_motivo(resultado: dict) -> str:
    """Mensaje para mostrar al usuario a partir de un resultado no autorizado."""
    return MOTIVOS.get(resultado.get("motivo"), "No se pudo verificar tu acceso.")


# =============================================================================
# Sesión local
#
# Se guarda en %APPDATA%\Amilab\sesion_amilab.json y sobrevive a los reinicios:
# un equipo ya registrado entra sin volver a pedir código. El registro del
# equipo sigue en la tabla `dispositivos`, indexado por hwid.
# =============================================================================

def cargar_sesion() -> dict:
    """Devuelve la sesión guardada, o {} si no hay o es de otro equipo."""
    return leer_sesion()


# El archivo de sesión lo escriben el hilo principal y el de auto-refresco de
# supabase, así que los guardados van serializados.
_lock_sesion = threading.Lock()

# Mientras se cierra sesión se ignoran los eventos de auth: si no, el propio
# sign_out podría reescribir el archivo justo después de borrarlo.
_cerrando_sesion = {"activo": False}


def _escribir_sesion(datos: dict) -> None:
    try:
        with open(SESION_FILE, "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except Exception:
        pass


def guardar_sesion(email: str, rol: str = None, empresa: str = None,
                   logo_url: str = None, empresa_id: str = None,
                   nombre: str = None) -> None:
    """Actualiza la sesión conservando lo que ya hubiera.

    El merge importa: los tokens de Supabase Auth viven en el mismo archivo, y
    reconstruirlo desde cero cerraría la sesión en cada refresco de datos.

    Se lee el archivo CRUDO, sin validar. Con leer_sesion() se perdían los
    tokens: justo después de canjear el código el archivo todavía no tiene
    `email`, así que la validación devolvía {} y este merge los borraba. Ése
    era el motivo de que la sesión caducara en cada reinicio.
    """
    with _lock_sesion:
        datos = leer_sesion_cruda()
        datos["email"] = email
        datos["hwid"] = obtener_id_pc()

        if rol:
            datos["rol"] = rol
        if empresa:
            datos["empresa"] = empresa
        # El nombre lo manda el registro central (core.usuarios.nombre_completo).
        # La app no lo guarda en ningún otro sitio ni deja editarlo: si está mal,
        # se corrige en Themein y llega solo en la siguiente revalidación.
        if nombre:
            datos["nombre"] = nombre
        if empresa_id:
            datos["empresa_id"] = empresa_id
        # logo_url puede volver a None a propósito (se quitó el logo en el panel).
        datos["logo_url"] = logo_url

        _escribir_sesion(datos)

    if rol:
        _guardar_rol_cache(rol)


# =============================================================================
# Supabase Auth: login por código enviado al correo
#
# Sustituye al código propio que se generaba a mano y se enviaba por Brevo.
# De cara al usuario el flujo es el mismo —correo, código, entrar—, pero al
# verificarlo Supabase devuelve una sesión JWT real. Eso es lo que permite que
# RLS sepa quién pregunta y cada empresa vea sólo sus datos.
#
# `should_create_user: False` es deliberado: sólo entran las cuentas que ya
# existen, es decir, las que se crearon desde el panel Themein.
# =============================================================================

def enviar_codigo_acceso(email: str):
    """Pide a Supabase que envíe un código al correo. -> (ok, mensaje_error)"""
    if supabase is None:
        return False, MOTIVOS["sin_conexion"]

    try:
        supabase.auth.sign_in_with_otp({
            "email": (email or "").strip().lower(),
            "options": {"should_create_user": False},
        })
        return True, ""
    except Exception as e:
        detalle = str(e)
        if "Signups not allowed" in detalle:
            return False, ("Ese correo no tiene cuenta de acceso. "
                           "Pide que la creen desde el panel de administración.")
        if "rate limit" in detalle.lower() or "seconds" in detalle.lower():
            return False, "Se envió un código hace poco. Espera un minuto y reintenta."
        return False, f"No se pudo enviar el código: {detalle}"


def iniciar_sesion_password(email: str, password: str):
    """Entra con correo y contraseña. -> (ok, mensaje_error)

    Tener la contraseña correcta no basta: manda el registro central. Si el
    usuario perdió el acceso (desactivado, empresa suspendida, app no
    contratada), se cierra la sesión recién abierta y no entra.
    """
    if supabase is None:
        return False, MOTIVOS["sin_conexion"]

    correo = (email or "").strip().lower()
    try:
        resp = supabase.auth.sign_in_with_password({"email": correo, "password": password})
    except Exception as e:
        detalle = str(e).lower()
        if "invalid login credentials" in detalle or "invalid" in detalle:
            return False, "Contraseña incorrecta."
        if any(senal in detalle for senal in _SENALES_DE_RED):
            return False, MOTIVOS["sin_conexion"]
        return False, f"No se pudo entrar: {e}"

    sesion_sb = getattr(resp, "session", None)
    if not sesion_sb:
        return False, "Contraseña incorrecta."

    acceso = verificar_acceso(correo)
    if not acceso.get("autorizado") and not es_fallo_tecnico(acceso):
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
        return False, texto_motivo(acceso)

    _guardar_tokens(sesion_sb, email=correo)
    return True, ""


def establecer_password(nueva_password: str):
    """Guarda la contraseña elegida y da la cuenta por activada. -> (ok, error)

    Necesita la sesión que abrió el código: es lo que demuestra que quien elige
    la contraseña es el dueño del correo. La contraseña no pasa por ningún canal
    intermedio, nadie más llega a conocerla.
    """
    if supabase is None:
        return False, MOTIVOS["sin_conexion"]

    try:
        supabase.auth.update_user({"password": nueva_password})
    except Exception as e:
        detalle = str(e).lower()
        if "should be different" in detalle or "same as the old" in detalle:
            return False, "La contraseña nueva tiene que ser distinta de la anterior."
        if "at least" in detalle or "weak" in detalle or "6 characters" in detalle:
            return False, "La contraseña es demasiado corta."
        return False, f"No se pudo guardar la contraseña: {e}"

    # Deja constancia en el registro central de que ya eligió la suya; si no,
    # la app le volvería a pedir activarla en el siguiente ingreso.
    try:
        # El {} es obligatorio: esta versión de postgrest-py exige el argumento
        # de parámetros aunque la función no reciba ninguno.
        supabase.schema("core").rpc("marcar_password_definida", {}).execute()
    except Exception as e:
        return False, ("La contraseña se guardó, pero no se pudo marcar la cuenta como "
                       f"activada: {e}. Vuelve a intentarlo.")

    return True, ""


def canjear_codigo(email: str, codigo: str):
    """Verifica el código y deja la sesión iniciada. -> (ok, mensaje_error)"""
    if supabase is None:
        return False, MOTIVOS["sin_conexion"]

    try:
        resp = supabase.auth.verify_otp({
            "email": (email or "").strip().lower(),
            "token": (codigo or "").strip(),
            "type": "email",
        })
    except Exception as e:
        detalle = str(e)
        if "expired" in detalle.lower():
            return False, "El código venció. Pide uno nuevo."
        if "invalid" in detalle.lower() or "token" in detalle.lower():
            return False, "Código incorrecto."
        return False, f"No se pudo verificar el código: {detalle}"

    sesion_sb = getattr(resp, "session", None)
    if not sesion_sb:
        return False, "Código incorrecto o vencido."

    _guardar_tokens(sesion_sb, email=(email or "").strip().lower())
    return True, ""


def _guardar_tokens(sesion_sb, email: str = None) -> None:
    """Persiste los tokens de Supabase Auth junto al resto de la sesión."""
    if sesion_sb is None or _cerrando_sesion["activo"]:
        return

    with _lock_sesion:
        datos = leer_sesion_cruda()
        datos["access_token"] = sesion_sb.access_token
        datos["refresh_token"] = sesion_sb.refresh_token

        # El archivo queda utilizable desde el primer guardado, sin depender de
        # que después llegue guardar_sesion().
        correo = email or getattr(getattr(sesion_sb, "user", None), "email", None)
        if correo and not datos.get("email"):
            datos["email"] = correo
            datos["hwid"] = obtener_id_pc()

        _escribir_sesion(datos)


_SENALES_DE_RED = (
    "connection", "timeout", "timed out", "network", "unreachable",
    "getaddrinfo", "resolve", "ssl", "socket",
)


_lock_restaurar = threading.Lock()


def _sesion_viva() -> bool:
    """True si el cliente YA tiene un token de acceso vigente (con margen).

    Sirve para no refrescar cuando no hace falta: cada refresco rota el token y
    deja el anterior inservible, así que cuantos menos, menos ocasiones de que
    dos partes de la app se pisen."""
    try:
        sesion = supabase.auth.get_session()
    except Exception:
        return False
    if not sesion or not getattr(sesion, "access_token", None):
        return False
    expira = getattr(sesion, "expires_at", None)
    if not expira:
        return False
    try:
        return float(expira) - time.time() > 120
    except Exception:
        return False


def _intentar_refresco(token: str):
    """Canjea un refresh token. -> ('ok', sesion) | ('sin_conexion'|'rechazada', None)"""
    try:
        resp = supabase.auth.refresh_session(token)
    except Exception as e:
        detalle = str(e).lower()
        if any(senal in detalle for senal in _SENALES_DE_RED):
            return "sin_conexion", None
        return "rechazada", None

    sesion_sb = getattr(resp, "session", None)
    return ("ok", sesion_sb) if sesion_sb else ("rechazada", None)


def restaurar_sesion() -> str:
    """Reactiva en el cliente la sesión de Supabase Auth guardada en disco.

    Hay que llamarla al arrancar: sin ella el cliente sale sin identidad y RLS
    no devuelve nada. Se usa el refresh token porque el de acceso dura una hora
    y casi siempre estará vencido entre dos arranques.

    Va serializada y evita refrescos innecesarios a propósito. Supabase ROTA el
    refresh token en cada canje: el anterior queda inservible. Si dos partes de
    la app lo canjeaban a la vez (el arranque, el auto-refresco de la librería y
    las lecturas que reintentan por «JWT expired»), la segunda recibía
    «already used», que se leía como «esta sesión ya no vale» y mandaba al
    usuario al login. Ésa era la causa de que la sesión caducara sola.

    Devuelve:
        'ok'            sesión activa
        'rechazada'     el token ya no vale: hay que verificar un código
        'sin_conexion'  no se pudo comprobar; no es motivo para echar a nadie
    """
    if supabase is None:
        return "sin_conexion"

    with _lock_restaurar:
        # 1. Si la sesión en memoria sigue vigente, no se toca nada.
        if _sesion_viva():
            return "ok"

        # Se lee el archivo crudo: los tokens se guardan antes de que exista el
        # resto de la sesión, y aquí sólo interesa el refresh token.
        token = leer_sesion_cruda().get("refresh_token")
        if not token:
            # Sesión de una versión anterior, de cuando el login no usaba
            # Supabase Auth. Hay que iniciar sesión una vez más.
            return "rechazada"

        estado, sesion_sb = _intentar_refresco(token)
        if estado == "ok":
            _guardar_tokens(sesion_sb)
            return "ok"
        if estado == "sin_conexion":
            return "sin_conexion"

        # 2. No valió, pero puede que el auto-refresco de la librería (u otra
        #    instancia de la app) haya dejado uno más nuevo en disco mientras
        #    tanto. Se reintenta con lo último guardado antes de darla por
        #    perdida.
        token_nuevo = leer_sesion_cruda().get("refresh_token")
        if token_nuevo and token_nuevo != token:
            estado, sesion_sb = _intentar_refresco(token_nuevo)
            if estado == "ok":
                _guardar_tokens(sesion_sb)
                return "ok"
            if estado == "sin_conexion":
                return "sin_conexion"

        # 3. Último vistazo: el canje pudo fallar por venir de un token ya
        #    rotado y, aun así, el cliente tener sesión buena en memoria.
        return "ok" if _sesion_viva() else "rechazada"


def _es_error_jwt(e) -> bool:
    """True si el error es por token de acceso vencido (PGRST303 / 'JWT expired').

    Pasa cuando el access token (dura 1 hora) caduca y el auto-refresh no alcanzó
    a renovarlo: PostgREST rechaza la consulta y hay que refrescar y reintentar.
    """
    txt = str(e).lower()
    return "jwt expired" in txt or "pgrst303" in txt


_lock_refresco = threading.Lock()


def ejecutar_con_refresco(operacion):
    """Ejecuta `operacion()` y, si falla por JWT vencido, refresca la sesión con
    el refresh token y reintenta una vez. Cualquier otro error se propaga igual.

    Centraliza el manejo del token caducado para que las lecturas/escrituras a la
    nube no revienten con 'JWT expired' cuando la app lleva más de una hora abierta.

    El refresco está serializado con un lock: si varias lecturas en paralelo (p. ej.
    las tarjetas del inicio) chocan a la vez con el token vencido, solo UNA refresca.
    Las demás, al tomar el lock, primero reintentan la operación (que ya suele
    funcionar con la sesión recién renovada) antes de refrescar de nuevo. Así se
    evita la carrera de rotación del refresh token, que si no dejaría a unas cuantas
    con 'invalid refresh token' y provocaría un cierre de sesión espurio.
    """
    try:
        return operacion()
    except Exception as e:
        if not _es_error_jwt(e):
            raise
        err = e

    with _lock_refresco:
        # Otro hilo pudo haber refrescado la sesión mientras esperábamos el lock:
        # se reintenta antes de volver a refrescar.
        try:
            return operacion()
        except Exception as e:
            if not _es_error_jwt(e):
                raise

        if restaurar_sesion() == "ok":
            return operacion()
        raise err


def token_de_acceso() -> str:
    """Access token vigente de la sesion, o "" si no hay.

    Lo usa `github_trigger` para identificarse ante la Edge Function que dispara
    las descargas: es la credencial que la app YA tiene, en vez de un token de
    GitHub propio. Si el token esta por caducar se refresca primero, para no
    gastar el viaje en un 401.
    """
    if supabase is None:
        return ""
    try:
        if not _sesion_viva():
            restaurar_sesion()
        sesion = supabase.auth.get_session()
        return getattr(sesion, "access_token", "") or ""
    except Exception:
        return ""


def hay_sesion_supabase() -> bool:
    """True si el cliente tiene ahora mismo una sesión activa."""
    if supabase is None:
        return False
    try:
        return supabase.auth.get_session() is not None
    except Exception:
        return False


def _al_cambiar_sesion(evento, sesion_sb) -> None:
    """Guarda los tokens cada vez que el cliente los renueva.

    supabase-py arranca con `auto_refresh_token` activo: mientras la app está
    abierta va renovando la sesión en segundo plano, y cada renovación INVALIDA
    el refresh token anterior. Sin escuchar estos eventos, lo guardado en disco
    envejece y al siguiente arranque el token ya no sirve: la sesión parecía
    caducar en cada reinicio.
    """
    try:
        if evento in ("TOKEN_REFRESHED", "SIGNED_IN", "USER_UPDATED") and sesion_sb:
            _guardar_tokens(sesion_sb)
    except Exception:
        pass  # nunca debe tumbar el hilo de refresco


if supabase is not None:
    try:
        supabase.auth.on_auth_state_change(_al_cambiar_sesion)
    except Exception:
        pass  # sin listener sigue funcionando, sólo persiste algo menos


def olvidar_tokens() -> None:
    """Borra SOLO las credenciales de Supabase Auth y conserva el correo.

    Es lo que se hace cuando la sesión no se pudo revalidar: el equipo sigue
    siendo el mismo y su correo ya está registrado en `dispositivos`, así que no
    hay ningún motivo para volver a preguntarlo. A lo sumo hay que verificar un
    código. Distinto de `cerrar_sesion()`, que sí olvida el correo porque ahí es
    el usuario quien pide desvincular el equipo."""
    with _lock_sesion:
        datos = leer_sesion_cruda()
        datos.pop("access_token", None)
        datos.pop("refresh_token", None)
        _escribir_sesion(datos)


def correo_recordado() -> str:
    """Correo con el que se entró en este equipo (aunque ya no haya tokens)."""
    return (leer_sesion_cruda().get("email") or "").strip()


def cerrar_sesion() -> None:
    """Olvida la sesión de este equipo. No borra su registro en `dispositivos`."""
    _ROL_CACHE["rol"] = None
    _cerrando_sesion["activo"] = True   # que el listener no reescriba el archivo

    try:
        if supabase is not None:
            try:
                supabase.auth.sign_out()
            except Exception:
                pass  # sin red basta con borrar los tokens locales

        for ruta in (SESION_FILE, ROL_FILE):
            try:
                os.remove(ruta)
            except Exception:
                pass
    finally:
        _cerrando_sesion["activo"] = False

    # El logo de la empresa saliente no debe quedarse puesto para la siguiente.
    try:
        import logo_empresa
        import theme

        logo_empresa.limpiar()
        theme.set_logo_empresa(None)
    except Exception:
        pass


def hay_sesion() -> bool:
    return bool(cargar_sesion())


def email_sesion() -> str:
    return cargar_sesion().get("email", "")


# =============================================================================
# Rol del usuario
#
# Antes el rol era del EQUIPO (columna `rol` de `dispositivos`); ahora es de la
# PERSONA y sale de core.usuarios. Vocabulario: super_admin, admin, seller,
# user, otros.
#
# Sigue habiendo caché en disco para arrancar sin conexión, y se aceptan los
# nombres antiguos que puedan quedar guardados de versiones anteriores:
# 'descargador' -> 'admin', 'lector' -> 'user'.
# =============================================================================

_ROLES_VALIDOS = ("super_admin", "admin", "seller", "user", "otros")

_ROL_CACHE = {"rol": None}


def _normalizar_rol(rol):
    """Normaliza al vocabulario del registro central, aceptando los viejos."""
    r = str(rol or "").strip().lower()
    if r in ("admin", "descargador"):
        return "admin"
    if r in ("user", "lector"):
        return "user"
    if r in _ROLES_VALIDOS:
        return r
    return None


def _guardar_rol_cache(rol):
    rol = _normalizar_rol(rol)
    if not rol:
        return
    _ROL_CACHE["rol"] = rol
    try:
        with open(ROL_FILE, "w", encoding="utf-8") as f:
            json.dump({"rol": rol}, f)
    except Exception:
        pass


def obtener_rol_usuario() -> str:
    """Rol del usuario con sesión iniciada.

    Consulta la nube una vez por ejecución; si no hay conexión usa el último rol
    conocido, y si nunca se conoció, 'user' (lo seguro: un user no puede
    cancelar descargas ajenas).
    """
    if _ROL_CACHE["rol"] in _ROLES_VALIDOS:
        return _ROL_CACHE["rol"]

    sesion = cargar_sesion()

    # Sin sesión no hay privilegios: no se hereda el rol cacheado de otro
    # usuario que hubiera entrado antes en este equipo.
    if not sesion.get("email"):
        return "user"

    rol = None
    resultado = verificar_acceso(sesion["email"])
    if resultado.get("autorizado"):
        rol = _normalizar_rol(resultado.get("rol"))

    if rol is None:
        rol = _normalizar_rol(sesion.get("rol"))

    if rol is None:
        try:
            with open(ROL_FILE, "r", encoding="utf-8") as f:
                rol = _normalizar_rol(json.load(f).get("rol"))
        except Exception:
            rol = None

    if rol is None:
        rol = "user"
    else:
        _guardar_rol_cache(rol)

    _ROL_CACHE["rol"] = rol
    return rol


# Nombre anterior, cuando el rol era del equipo. Se mantiene para no romper
# integraciones externas; internamente ya nadie lo usa.
obtener_rol_dispositivo = obtener_rol_usuario


# =============================================================================
# Permisos por rol
#
#   admin   todo.
#   seller  trabaja con los datos: descarga, revisa foros y exporta. No cancela
#           descargas de OTROS equipos, no borra de la lista de ofertadas y no
#           toca los filtros (que son compartidos por toda la empresa).
#   user    sólo consulta: lee lo ya descargado y lo actualiza desde la nube.
#
# Todo lo que restringen estas funciones es de la interfaz. La barrera de
# verdad está en la base (RLS y las funciones de `core`); esto evita que a
# alguien se le ofrezca algo que no le corresponde.
# =============================================================================

def es_admin() -> bool:
    """Puede cancelar cualquier descarga, propia o lanzada desde otro equipo."""
    return obtener_rol_usuario() in ("admin", "super_admin")


def puede_descargar() -> bool:
    """Puede lanzar descargas y revisiones a mano (los tres módulos)."""
    return obtener_rol_usuario() in ("admin", "super_admin", "seller")


def puede_editar_filtros() -> bool:
    """Puede crear, editar, borrar e importar filtros. Exportar puede cualquiera."""
    return es_admin()


def puede_eliminar_ofertadas() -> bool:
    """Puede quitar licitaciones de la lista de foros."""
    return es_admin()


def puede_gestionar_equipo() -> bool:
    """Puede ver el equipo de su empresa y cambiar el rol de sus miembros."""
    return es_admin()


ROLES_ASIGNABLES = ("admin", "seller", "user")

ETIQUETA_ROL = {
    "super_admin": "Super admin",
    "admin": "Admin",
    "seller": "Seller",
    "user": "User",
    "otros": "Otros",
}


def listar_equipo():
    """Miembros de la empresa del usuario. -> (lista, error)

    La lista la arma `core.equipo_de_empresa()`, que comprueba que quien
    pregunta sea administrador: `core.usuarios` no se puede leer directamente.
    """
    if supabase is None:
        return [], MOTIVOS["sin_conexion"]
    try:
        resp = ejecutar_con_refresco(
            lambda: supabase.schema("core").rpc("equipo_de_empresa", {}).execute())
        return (resp.data or []), ""
    except Exception as e:
        detalle = f"{getattr(e, 'code', '')} {e}"
        if "PGRST202" in detalle or "42883" in detalle:
            return [], ("Falta preparar la base para este módulo: ejecuta "
                        "`sql_equipo.sql` en Supabase.")
        if "42501" in detalle:
            return [], "Sólo un administrador puede ver el equipo."
        return [], f"No se pudo leer el equipo: {e}"


def cambiar_rol(email: str, rol: str):
    """Cambia el rol de un miembro de la empresa. -> (ok, error)"""
    if supabase is None:
        return False, MOTIVOS["sin_conexion"]
    try:
        ejecutar_con_refresco(
            lambda: supabase.schema("core").rpc(
                "cambiar_rol_usuario",
                {"p_email": (email or "").strip().lower(), "p_rol": rol},
            ).execute())
        return True, ""
    except Exception as e:
        # Las funciones de core devuelven el motivo en el mensaje; se muestra
        # tal cual porque están redactadas para leerse.
        texto = str(getattr(e, "message", "") or e)
        return False, texto
