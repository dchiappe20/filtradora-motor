# Filtradora — motor de barridos

Rutinas que recorren los portales de compras públicas de Chile
([Mercado Público](https://www.mercadopublico.cl)) y dejan los resultados en una
base de datos Supabase. Corren solas en GitHub Actions; no tienen interfaz.

Es la mitad *headless* de la Filtradora: la aplicación de escritorio que consume
estos datos vive aparte y no forma parte de este repositorio.

## Qué hace cada workflow

| Workflow | Cuándo (hora de Chile) | Qué hace |
|---|---|---|
| `descarga-nocturna.yml` | 01:00 | Barrido **completo** de Compra Ágil: recorre los 30 días de la ventana y filtra para cada empresa. |
| `compra-agil-diurno.yml` | 12:00 | Barrido **corto**: lo publicado hoy, más el **seguimiento de estado** de lo ya filtrado. |
| `foros-nocturno.yml` | 05:00 | Revisa los foros de aclaración de las licitaciones ofertadas y detecta menciones. |
| `descarga-licitaciones.yml` | bajo demanda | Descarga las licitaciones de un rango de fechas. |

### Quién pone la hora

**Supabase, no GitHub.** Un `pg_cron` del proyecto llama a la API de GitHub y
lanza cada barrido por `workflow_dispatch` a la hora exacta de Chile — cambio de
huso incluido, porque la hora se compara contra `America/Santiago` y no contra
UTC. Todo eso es un solo archivo: [`supabase/cron_barridos.sql`](supabase/cron_barridos.sql).

El motivo es medido, no teórico: desde el 27-08-2026 **todas** las corridas
programadas con el `schedule:` de GitHub salen entre 2 y 12 horas tarde, mientras
que las lanzadas por `dispatch` arrancan en el mismo segundo en que se piden. El
atraso además dejaba al diurno sin hacer nada: su guardia miraba el reloj para
saber qué ranura era y, llegando a las 14:42, no se reconocía como el de las
12:00 y se descartaba en silencio.

Los `schedule:` siguen en los workflows como **respaldo**, con dos arreglos:

- la ranura se deduce de la hora **programada** (`github.event.schedule`), no del
  reloj, así que una corrida atrasada hace su trabajo en vez de evaporarse;
- antes de correr, cada job le pregunta a la API si el disparo puntual ya cubrió
  ese turno, y si ya está hecho no lo repite.

Cada barrido se sigue programando en las **dos** horas UTC posibles porque Chile
cambia de huso dos veces al año; el guardia descarta la que no toca.

## Filtrado y seguimiento

Compra Ágil es pública e idéntica para todos, así que se guarda **una sola
copia** (`compra_agil`, ~38.000 filas). Sobre ella, cada barrido hace dos cosas
más, por empresa:

1. **Filtra.** Aplica las reglas de esa empresa y deja el resultado en
   `compra_agil_seguimiento`. Antes esto lo hacía cada aplicación de escritorio,
   con los mismos datos, en cada visita.
2. **Sigue el estado.** Como el conjunto filtrado son cientos de cotizaciones y
   no decenas de miles, los barridos del día pueden permitirse volver a
   preguntarle al portal por cada una y mantener al día su llamado y sus
   cierres. Antes eso sólo pasaba en el barrido completo de la madrugada, y
   entremedio no se podía saber si algo había pasado a segundo llamado.

Qué llamados se siguen lo decide cada empresa (`preferencias_empresa`). Una
empresa sin reglas cargadas se **salta**, no se le vacía el conjunto.

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
