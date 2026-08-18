# processor.py
import numpy as np
import pandas as pd
import re
import config

# Reglas ya parseadas: `obtener_partes_regla` es pura y se la llamaba una vez por
# regla Y POR FILA en la auditoría (50.000 veces en un filtrado normal).
_CACHE_PARTES = {}

# Patrones compilados y su literal de prefiltro (ver `_literales`).
_CACHE_PATRON = {}


def _partes_regla(regla_str):
    """`obtener_partes_regla` con memoria. Mismo resultado, sin re-parsear."""
    partes = _CACHE_PARTES.get(regla_str)
    if partes is None:
        partes = obtener_partes_regla(regla_str)
        _CACHE_PARTES[regla_str] = partes
    return partes


def _ramas(cuerpo):
    """Parte `a|b|c` por los `|` de primer nivel, respetando grupos y escapes."""
    ramas, prof, ini, i = [], 0, 0, 0
    while i < len(cuerpo):
        c = cuerpo[i]
        if c == "\\":
            i += 2
            continue
        if c == "(":
            prof += 1
        elif c == ")":
            prof -= 1
        elif c == "|" and prof == 0:
            ramas.append(cuerpo[ini:i])
            ini = i + 1
        i += 1
    ramas.append(cuerpo[ini:])
    return ramas


def _literales(patron):
    """Textos de los que al menos uno TIENE que aparecer para que `patron` case.

    Sirve para descartar filas sin ejecutar la expresión regular, que es lo caro:
    buscar un literal es una comparación de cadenas en C, mientras que cada
    `re.search` recorre el texto con el motor de Python. Medido sobre 38.037
    filas y 165 patrones, esto baja el escaneo de 52 s a 13 s.

    Todos los patrones los construye `token_a_regex`, así que sólo hay cuatro
    formas posibles: `\\bX\\b`, `\\bX\\w*`, `\\w*X\\b` y alternativas `(?:...)`
    de las anteriores. Si aparece algo que no encaja se devuelve None y esa
    parte se resuelve con la regex de siempre: el prefiltro es un atajo, nunca
    decide por sí solo si una fila coincide.
    """
    if patron.startswith("(?:") and patron.endswith(")"):
        lits = []
        for rama in _ramas(patron[3:-1]):
            sub = _literales(rama)
            if not sub:
                return None      # una sola rama sin literal invalida el atajo
            lits.extend(sub)
        return lits

    for forma in (r"\\b(.+?)\\b", r"\\b(.+?)\\w\*", r"\\w\*(.+?)\\b"):
        m = re.fullmatch(forma, patron)
        if m:
            # Deshacer el re.escape para recuperar el texto tal cual.
            return [re.sub(r"\\(.)", r"\1", m.group(1))]
    return None


def _coincidencias(patron, textos):
    """Máscara booleana de qué `textos` (array de str) casan con `patron`.

    Primero descarta con el literal y sólo pasa la regex por los supervivientes,
    que de media son 128 de 38.037.
    """
    dato = _CACHE_PATRON.get(patron)
    if dato is None:
        dato = (re.compile(patron), _literales(patron))
        _CACHE_PATRON[patron] = dato
    rx, lits = dato

    n = len(textos)
    if not lits:
        return np.fromiter((rx.search(t) is not None for t in textos), bool, n)

    if len(lits) == 1:
        lit = lits[0]
        candidatos = np.fromiter((lit in t for t in textos), bool, n)
    else:
        candidatos = np.fromiter(
            (any(l in t for l in lits) for t in textos), bool, n)

    salida = np.zeros(n, dtype=bool)
    for i in np.flatnonzero(candidatos):
        salida[i] = rx.search(textos[i]) is not None
    return salida


def _mascara_regla(partes, textos):
    """Filas que cumplen TODAS las partes de una regla.

    Las partes van encadenadas: la segunda sólo se comprueba sobre lo que pasó
    la primera, en vez de recorrer las 38.000 filas otra vez.
    """
    mascara = _coincidencias(partes[0], textos)
    for parte in partes[1:]:
        idx = np.flatnonzero(mascara)
        if not len(idx):
            break
        mascara[idx] = _coincidencias(parte, textos[idx])
    return mascara


def quitar_tildes(texto):
    if not isinstance(texto, str): return texto
    reemplazos = {
        'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
        'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u'
    }
    for orig, reemplazo in reemplazos.items():
        texto = texto.replace(orig, reemplazo)
    return texto

def token_a_regex(token):
    token = token.strip().lower()
    token = quitar_tildes(token)
    if not token: return None
    
    if token.startswith('(') and token.endswith(')'):
        contenido = token[1:-1]
        partes = [p.strip() for p in contenido.split(',') if p.strip()]
        if not partes: return None
        regex_partes = [token_a_regex(p) for p in partes]
        regex_partes = [r for r in regex_partes if r]
        return f"(?:{'|'.join(regex_partes)})"
    
    elif token.endswith('*'):
        return f"\\b{re.escape(token[:-1])}\\w*"
    elif token.startswith('*'):
        return f"\\w*{re.escape(token[1:])}\\b"
    else:
        return f"\\b{re.escape(token)}\\b"

def obtener_partes_regla(regla_str):
    if not regla_str.strip(): return []
    tokens = re.findall(r'(\([^\)]+\)|[\w\*]+)', regla_str)
    partes = []
    for t in tokens:
        rgx = token_a_regex(t)
        if rgx: partes.append(rgx)
    return partes

def auditar_fila(fila, reglas_texto):
    valores_texto = [str(x) if pd.notna(x) else "" for x in fila.values]
    fila_texto_completa = " ".join(valores_texto).lower()
    fila_texto_completa = quitar_tildes(fila_texto_completa)

    for regla in reglas_texto:
        # Parseo cacheado: esto corre una vez POR FILA, así que re-parsear las
        # 137 reglas en cada una eran ~50.000 parseos por filtrado.
        partes_regex = _partes_regla(regla)
        if not partes_regex: continue

        cumple_todos = True
        palabras_encontradas = []
        
        for parte in partes_regex:
            match = re.search(parte, fila_texto_completa)
            if not match:
                cumple_todos = False
                break
            else:
                palabras_encontradas.append(match.group())
        
        if cumple_todos:
            columnas_match = []
            for col_nombre, val in fila.items():
                val_str = str(val).lower() if pd.notna(val) else ""
                val_str = quitar_tildes(val_str)
                
                for parte in partes_regex:
                    if re.search(parte, val_str):
                        if col_nombre not in columnas_match:
                            columnas_match.append(col_nombre)
            
            columna_str = ", ".join(columnas_match)
            detalle_str = " + ".join(list(set(palabras_encontradas)))
            return regla, columna_str, detalle_str
            
    return "N/A", "N/A", "N/A"

def filtrar_licitaciones(df, reglas_texto):
    if df is None or not reglas_texto: return None

    # Verificación de seguridad de la columna ID
    if config.COLUMNA_ID not in df.columns:
        raise Exception(f"No se encontró la columna '{config.COLUMNA_ID}' en el Excel. Revisa config.py.")

    print("[filtro] Preparando motor de búsqueda y normalizando texto...")

    # Concatenar columna a columna (vectorizado) en vez de fila a fila con apply:
    # mismo texto exacto, y sobre 38.000 filas baja de 2,2 s a 0,2 s.
    #
    # Los nulos van a "" como hacía el apply de antes. No es cosmético: sin eso
    # un `astype(str)` los escribiría como el texto "nan", que hasta podría casar
    # con una regla. Las tres pantallas que llaman aquí ya normalizan antes, así
    # que en la práctica no hay nulos; esto es para que un df en crudo tampoco
    # rompa. Y `na_rep` evita que `str.cat` convierta la fila entera en NaN.
    columnas = list(df.columns)
    def _texto(col):
        serie = df[col]
        return serie.astype(str).where(serie.notna(), "")
    search_series = _texto(columnas[0]).str.cat(
        [_texto(c) for c in columnas[1:]], sep=' ', na_rep="").str.lower()
    reemplazos = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u'}
    for orig, reempl in reemplazos.items():
        search_series = search_series.str.replace(orig, reempl, regex=False)

    # A partir de aquí se trabaja sobre un array de numpy: las máscaras booleanas
    # se combinan sin el coste por elemento de pandas y se vuelve a Series al final.
    textos = search_series.to_numpy()

    # Máscara de los que matchean EXACTAMENTE con las reglas
    mascara_directas = np.zeros(len(df), dtype=bool)

    contador = 0
    total_reglas = len(reglas_texto)

    print("   -> Fase 1: Identificando coincidencias directas...")
    for regla in reglas_texto:
        partes = _partes_regla(regla)
        if not partes: continue

        mascara_directas |= _mascara_regla(partes, textos)
        contador += 1
        if contador % 50 == 0:
            print(f"      ... revisadas {contador}/{total_reglas} reglas")

    mascara_coincidencias_directas = pd.Series(mascara_directas, index=df.index)

    # --- NUEVA LÓGICA DE AGRUPACIÓN POR ID ---
    print("   -> Fase 1.5: Rescatando filas con ID compartido...")
    
    # 1. Obtenemos todos los IDs únicos de las filas que SÍ cumplieron el filtro
    ids_encontrados = df.loc[mascara_coincidencias_directas, config.COLUMNA_ID].unique()
    
    # 2. Creamos una nueva máscara que incluye TODAS las filas que tengan esos IDs
    mascara_final = df[config.COLUMNA_ID].isin(ids_encontrados)
    
    df_filtrado = df[mascara_final].copy()
    
    print(f"[filtro] {len(ids_encontrados)} licitaciones únicas encontradas.")
    print(f"[filtro] {len(df_filtrado)} filas en total (incluyendo las asociadas por ID).")

    if not df_filtrado.empty:
        print("   -> Fase 2: Generando auditoría de columnas...")
        lista_reglas, lista_cols, lista_detalles = [], [], []
        
        total_filas = len(df_filtrado)
        for i, (idx, fila) in enumerate(df_filtrado.iterrows()):
            # Si esta fila fue una coincidencia directa, la auditamos
            if mascara_coincidencias_directas[idx]:
                r, c, d = auditar_fila(fila, reglas_texto)
            # Si no, significa que entró por compartir ID
            else:
                r, c, d = ("Incluida por ID compartida", "N/A", "N/A")
                
            lista_reglas.append(r)
            lista_cols.append(c)
            lista_detalles.append(d)
            
        df_filtrado['FILTRO_APLICADO'] = lista_reglas
        df_filtrado['COLUMNA_ENCONTRADA'] = lista_cols
        df_filtrado['DETALLE_COINCIDENCIA'] = lista_detalles

    print(f"[filtro] Proceso completo: {len(df_filtrado)} filas listas.")
    return df_filtrado

def guardar_excel(df):
    if df is None or df.empty:
        return
    try:
        import export_excel
        export_excel.exportar_estilizado(df, config.OUTPUT_FILE, titulo_hoja="Resultado")
    except Exception as e:
        print(f"[filtro] ERROR guardando: {e}")