-- ============================================================================
-- cron_barridos.sql — Los barridos salen a su hora, disparados desde Supabase.
--
-- PARA QUÉ ESTO
--
-- Los `schedule:` de GitHub Actions son «best effort», y en este repo dejaron de
-- ser aceptables. Medido sobre las corridas reales, desde el 2026-08-27:
--
--   Barrido diurno (le tocaba 12:00 Chile) ...... entre 2h17 y 4h34 tarde
--   Descarga nocturna (le tocaba 01:07) ......... entre 4h22 y 6h28 tarde
--   Foros inversos (le tocaba 05:23) ............ entre 4h16 y 7h44 tarde
--
-- Y el atraso se comía el barrido entero: el guardia del workflow miraba el
-- RELOJ para saber qué ranura era, así que una corrida que llegaba a las 14:42
-- no se reconocía como la de las 12 y se descartaba en silencio. Las últimas
-- cinco corridas programadas del diurno terminaron en 6 segundos sin bajar nada.
--
-- Las corridas lanzadas por `workflow_dispatch`, en cambio, arrancan SIEMPRE en
-- el mismo segundo en que se piden — no pasan por esa cola. De ahí la solución:
-- el reloj lo pone Supabase (pg_cron, que sí es puntual) y GitHub sólo ejecuta.
-- Los `schedule:` se quedan en los workflows como RESPALDO, y cada uno le
-- pregunta a la API si el disparo puntual ya cubrió su turno antes de repetirlo.
--
-- CÓMO SE INSTALA (una vez, desde el SQL Editor del proyecto Supabase)
--
--   1. Crear en GitHub un fine-grained token con acceso SÓLO al repositorio
--      dchiappe20/filtradora-motor y el permiso «Actions: Read and write».
--      (Settings -> Developer settings -> Personal access tokens -> Fine-grained)
--      ⚠ Caduca: anotar la fecha. Si caduca, los barridos dejan de salir a su
--        hora y sólo quedan los respaldos atrasados de GitHub.
--   2. Pegar este archivo entero en el SQL Editor y reemplazar el
--      'PEGA_AQUI_EL_TOKEN' (aparece UNA sola vez, marcado con ⚠).
--   3. Comprobar con las consultas del final.
--
-- ⚠ ESTE REPOSITORIO ES PÚBLICO. El token se pega en el SQL Editor y se queda
--   en Vault, cifrado: NUNCA se guarda aquí. Este archivo se versiona siempre
--   con el marcador puesto.
-- ============================================================================

-- ── Extensiones ─────────────────────────────────────────────────────────────
-- pg_cron pone la hora; pg_net hace la llamada HTTP a GitHub. Las dos vienen
-- con Supabase, sólo hay que encenderlas.
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net;

-- ── Dónde vive esto ─────────────────────────────────────────────────────────
-- Esquema aparte, NO `public`: PostgREST sólo expone `public`, así que nada de
-- lo de aquí se asoma a la API ni a la app.
create schema if not exists motor;

-- ── El token ────────────────────────────────────────────────────────────────
-- En Vault, cifrado. Nunca en el código ni en la definición del cron job.
-- Volver a ejecutar esto con otro token lo reemplaza (útil cuando caduca).
do $$
declare
  -- ⚠ LO ÚNICO QUE HAY QUE EDITAR EN TODO EL ARCHIVO.
  token      text := 'PEGA_AQUI_EL_TOKEN';
  id_secreto uuid;
begin
  -- Que el marcador siga puesto es el error más fácil de cometer, y sin este
  -- aviso se descubriría de noche, cuando GitHub contestara 401 a un disparo que
  -- nadie está mirando.
  if token not like 'github_pat_%' and token not like 'ghp_%' then
    raise exception 'Falta el token: reemplaza el marcador de arriba por el fine-grained token de GitHub.';
  end if;

  select id into id_secreto from vault.secrets where name = 'github_actions_token';
  if id_secreto is null then
    perform vault.create_secret(
      token,
      'github_actions_token',
      'PAT con Actions:write en dchiappe20/filtradora-motor, para disparar los barridos'
    );
  else
    perform vault.update_secret(id_secreto, token);
  end if;
end $$;

-- ── Bitácora ────────────────────────────────────────────────────────────────
-- pg_net es asíncrono: `http_post` devuelve un id y no espera la respuesta, así
-- que sin esto un disparo fallido sería invisible. Se guarda qué se pidió y el
-- id de la petición, que es lo que permite ir a buscar cómo terminó.
create table if not exists motor.barrido_disparos (
  id          bigserial primary key,
  archivo     text        not null,
  ranura      text,
  pedido_en   timestamptz not null default now(),
  request_id  bigint,
  nota        text
);

create index if not exists barrido_disparos_archivo_fecha
  on motor.barrido_disparos (archivo, pedido_en desc);

-- ── El disparo ──────────────────────────────────────────────────────────────
create or replace function motor.disparar_workflow(archivo text, inputs jsonb default '{}'::jsonb)
returns bigint
language plpgsql
security definer
set search_path = motor, extensions, vault, public
as $$
declare
  token text;
  req   bigint;
begin
  select decrypted_secret into token
    from vault.decrypted_secrets where name = 'github_actions_token';
  if token is null or token = '' then
    raise exception 'No hay token de GitHub en Vault (github_actions_token).';
  end if;

  -- La API contesta 204 sin cuerpo. Lo que importa es el código: 401/403 es
  -- token malo o caducado, 422 es que el input no existe en el workflow.
  select net.http_post(
    url     := 'https://api.github.com/repos/dchiappe20/filtradora-motor/actions/workflows/'
               || archivo || '/dispatches',
    body    := jsonb_build_object('ref', 'main', 'inputs', coalesce(inputs, '{}'::jsonb)),
    headers := jsonb_build_object(
      'Authorization',        'Bearer ' || token,
      'Accept',               'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'Content-Type',         'application/json',
      'User-Agent',           'filtradora-motor-cron'
    ),
    timeout_milliseconds := 20000
  ) into req;

  insert into motor.barrido_disparos (archivo, ranura, request_id)
    values (archivo, inputs ->> 'ranura', req);

  return req;
end $$;

-- ── El reloj ────────────────────────────────────────────────────────────────
-- Se llama CADA HORA y decide dentro, mirando la hora de Chile.
--
-- Por qué así y no un cron a la hora UTC exacta: pg_cron programa en la zona de
-- la base (UTC en Supabase) y Chile cambia de huso dos veces al año, así que un
-- cron fijo se correría una hora en septiembre y en abril. Y si algún día
-- cambiara la zona de la base, un cron escrito en UTC se iría sin que nadie lo
-- notara, mientras que esto seguiría dando en la hora chilena correcta.
-- Las 23 llamadas que no son la hora cuestan una comparación y vuelven.
create or replace function motor.disparar_si_toca(
  archivo     text,
  hora_chile  int,
  inputs      jsonb default '{}'::jsonb
)
returns text
language plpgsql
security definer
set search_path = motor, extensions, public
as $$
declare
  ahora_cl timestamp := now() at time zone 'America/Santiago';
  ultimo   timestamptz;
begin
  if extract(hour from ahora_cl)::int <> hora_chile then
    return 'no toca';
  end if;

  -- Red anti-duplicado: si ya se disparó lo mismo en las últimas 6 horas, no se
  -- repite. Protege de un job reprogramado a mano o de un reintento.
  select max(pedido_en) into ultimo
    from motor.barrido_disparos
   where barrido_disparos.archivo = disparar_si_toca.archivo
     and coalesce(ranura, '') = coalesce(inputs ->> 'ranura', '')
     and pedido_en > now() - interval '6 hours';
  if ultimo is not null then
    return 'ya se disparó a las ' || ultimo::text;
  end if;

  perform motor.disparar_workflow(archivo, inputs);
  return 'disparado';
end $$;

-- ── Los turnos ──────────────────────────────────────────────────────────────
-- `schedule` en UTC no importa: se corre cada hora en punto y la función mira la
-- hora de Chile. El nombre del job es la clave; volver a ejecutar esto lo
-- reemplaza sin duplicarlo.
select cron.unschedule(jobname)
  from cron.job
 where jobname in ('barrido-mediodia', 'barrido-nocturno',
                   'licitaciones-nocturno', 'foros-nocturno',
                   'llamados-08', 'llamados-11', 'llamados-14', 'llamados-17');

-- 12:00 Chile — el diurno de Compra Ágil: RETIRADO el 2026-09-16. Lo del día lo
-- trae la función `compra-agil-reciente` cada 5 minutos y los 2dos llamados el
-- repaso de más abajo. Su nombre sigue en el `unschedule` de arriba para que
-- volver a ejecutar este archivo lo quite donde todavía esté.

-- 01:00 Chile — barrido completo de los 30 días, cuando el portal va suelto.
select cron.schedule(
  'barrido-nocturno', '0 * * * *',
  $cron$ select motor.disparar_si_toca('descarga-nocturna.yml', 1) $cron$
);

-- 03:00 Chile — barrido de Licitaciones: los 30 días de la ventana más los
-- días de publicación de lo que sigue abierto. Va entre los otros dos y no
-- compite con ellos: es otro portal (la API oficial, con su propio ticket) y la
-- cuenta es de repositorio público, así que los minutos de Actions son gratis.
select cron.schedule(
  'licitaciones-nocturno', '0 * * * *',
  $cron$ select motor.disparar_si_toca('licitaciones-nocturno.yml', 3) $cron$
);

-- 05:00 Chile — después del nocturno de Compra Ágil.
select cron.schedule(
  'foros-nocturno', '0 * * * *',
  $cron$ select motor.disparar_si_toca('foros-nocturno.yml', 5) $cron$
);

-- 08:00, 11:00, 14:00 y 17:00 Chile — el repaso de LLAMADOS.
--
-- Detecta durante el día las cotizaciones que reabren en 2do llamado; antes
-- había que esperar a la madrugada siguiente. Recorre los 10 días anteriores
-- (189 páginas, lo mismo que el nocturno) sin mirar lo de hoy.
--
-- CADA TURNO LLEVA SU PROPIA `ranura`, y no es cosmético: la red anti-duplicado
-- de `disparar_si_toca` mira las últimas 6 HORAS para el mismo (archivo,
-- ranura). Con la ranura vacía en los cuatro, sólo habría corrido el de las
-- 08:00 y los otros tres se habrían dado por repetidos, en silencio.
--
-- El de las 08:00 es el más valioso: recoge las que reabrieron de madrugada,
-- que el nocturno de la 01:00 no alcanza a ver.
select cron.schedule(
  'llamados-08', '0 * * * *',
  $cron$ select motor.disparar_si_toca('compra-agil-llamados.yml', 8, '{"ranura":"08"}'::jsonb) $cron$
);

select cron.schedule(
  'llamados-11', '0 * * * *',
  $cron$ select motor.disparar_si_toca('compra-agil-llamados.yml', 11, '{"ranura":"11"}'::jsonb) $cron$
);

select cron.schedule(
  'llamados-14', '0 * * * *',
  $cron$ select motor.disparar_si_toca('compra-agil-llamados.yml', 14, '{"ranura":"14"}'::jsonb) $cron$
);

select cron.schedule(
  'llamados-17', '0 * * * *',
  $cron$ select motor.disparar_si_toca('compra-agil-llamados.yml', 17, '{"ranura":"17"}'::jsonb) $cron$
);

-- Domingo 04:40 UTC — `limpiar-registros` borra cron.job_run_details de más de
-- 7 días y motor.barrido_disparos de más de 30. Lo define
-- sql_seguridad_limpieza.sql en el repo de la app (Filtradora-amilab).

-- ============================================================================
-- COMPROBAR QUE FUNCIONA
--
--   -- 1) Disparo de prueba, ahora mismo (debería aparecer una corrida en Actions):
--   select motor.disparar_workflow('compra-agil-diurno.yml', '{"ranura":"mediodia"}'::jsonb);
--
--   -- 2) Cómo terminó ese disparo. 204 = GitHub lo aceptó.
--   --    401/403 = token malo o caducado. 422 = input inexistente en el workflow.
--   select d.pedido_en, d.archivo, d.ranura, r.status_code, r.content
--     from motor.barrido_disparos d
--     left join net._http_response r on r.id = d.request_id
--    order by d.pedido_en desc limit 10;
--   -- (net._http_response sólo guarda las respuestas unas horas; después queda
--   --  el registro del disparo pero no su resultado.)
--
--   -- 3) Los turnos programados y cuándo corrió cada uno:
--   select jobid, jobname, schedule, active from cron.job order by jobname;
--   select j.jobname, d.status, d.start_time, d.return_message
--     from cron.job_run_details d join cron.job j using (jobid)
--    order by d.start_time desc limit 20;
--
-- SI HAY QUE APAGARLO
--   select cron.unschedule('barrido-mediodia');   -- y los otros tres
-- Los `schedule:` de los workflows siguen ahí de respaldo: los barridos volverán
-- a salir, pero con las horas de atraso de siempre.
-- ============================================================================
