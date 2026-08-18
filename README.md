# Filtradora — motor de barridos

Rutinas que recorren los portales de compras públicas de Chile
([Mercado Público](https://www.mercadopublico.cl)) y dejan los resultados en una
base de datos Supabase. Corren solas en GitHub Actions; no tienen interfaz.

Es la mitad *headless* de la Filtradora: la aplicación de escritorio que consume
estos datos vive aparte y no forma parte de este repositorio.

## Qué hace cada workflow

| Workflow | Cuándo | Qué hace |
|---|---|---|
| `descarga-nocturna.yml` | 05:00 UTC | Barrido **completo** de Compra Ágil: recorre los 30 días de la ventana. |
| `compra-agil-diurno.yml` | 13/14 y 19/20 UTC | Barridos **cortos**: sólo lo publicado hoy, para que lo nuevo no espere a la madrugada. |
| `foros-nocturno.yml` | 09:00 UTC | Revisa los foros de aclaración de las licitaciones ofertadas y detecta menciones. |
| `descarga-licitaciones.yml` | bajo demanda | Descarga las licitaciones de un rango de fechas. |

Los horarios se programan en las **dos** horas UTC posibles porque Chile cambia
de huso dos veces al año; el primer paso de cada job descarta la que no toca.

## Configuración

Todo lo sensible vive en *Secrets* del repositorio, nunca en el código:

| Secret | Para qué |
|---|---|
| `SUPABASE_URL` | Proyecto donde se guardan los datos. |
| `SUPABASE_SERVICE_KEY` | Escritura sin sesión de usuario. El código acota todo por `empresa_id`. |
| `MP_TICKET_<EMPRESA>` | Ticket de la API de Mercado Público. Uno por empresa: la API limita las peticiones por ticket. |

Lo que sí está a la vista es público por diseño:

- La *publishable key* de Supabase — lo que protege los datos es RLS, no
  esconder la clave.
- La `API_KEY` del buscador de Mercado Público, que el propio portal lleva en su
  front y cualquiera ve en las herramientas de desarrollo del navegador. Se
  puede sobreescribir con `MP_BUSCADOR_API_KEY` si el portal la cambia.

## Disparo bajo demanda

`descarga-licitaciones.yml` no se dispara desde la aplicación directamente: ésta
llama a una Edge Function de Supabase que valida quién pide la descarga, deduce
su empresa del token de sesión y sólo entonces lanza el workflow. Así la
aplicación distribuida no lleva ningún token de GitHub encima.

## Ejecutar a mano

```bash
pip install -r requirements.txt

export SUPABASE_URL=... SUPABASE_KEY=... TZ=America/Santiago
python barrido_compra_agil.py completo   # o 'dia'
```
