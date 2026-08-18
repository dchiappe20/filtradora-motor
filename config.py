# config.py
import os
import sys

if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

INPUT_FILE = "" 
OUTPUT_FILE = "" 
HEADER_ROW = 7 

# --- CONFIGURACIÓN ---
# Nombre EXACTO de la columna que contiene el ID de la licitación en tu Excel
COLUMNA_ID = "Numero Adquisición"

# --- FORO INVERSO (aclaraciones de oferta dirigidas a Amilab) ---
# Variantes del nombre de Amilab tal como puede aparecer en Mercado Público.
# La comparación se hace normalizada (sin acentos, minúsculas, substring).
FORO_INVERSO_PROVEEDOR_VARIANTES = ["Amilab", "Amilab Ltda", "Amilab Limitada"]

# Cuántos días hacia atrás se revisan licitaciones cerradas
FORO_INVERSO_VENTANA_DIAS = 30

# Pausa (segundos) entre cada licitación revisada, para no saturar el portal
FORO_INVERSO_PAUSA_SEGUNDOS = 3

# Días tras el cierre de una licitación a partir de los cuales se asume que su
# foro de aclaraciones ya no tendrá más cambios (deja de revisarse activamente)
FORO_INVERSO_DIAS_ESTABILIDAD = 14

# Cada cuántas licitaciones se renueva la sesión HTTP para evitar que el portal
# bloquee la sesión por volumen de peticiones consecutivas
FORO_INVERSO_RENOVAR_SESION_CADA = 100