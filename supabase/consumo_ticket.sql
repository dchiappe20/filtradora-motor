-- ============================================================================
-- Contador de consultas del ticket de Mercado Público (2026-11-24)
--
-- La API oficial da 10.000 consultas diarias POR TICKET y no informa cuánto se
-- lleva. Un solo ticket sirve a todos los clientes, así que cada proceso que lo
-- usa suma aquí lo que gastó:
--
--   licitaciones-reciente   función de Supabase, cada minuto de 07 a 21 h
--   licitaciones-nocturno   barrido de la madrugada (api_client.py)
--   descarga-rango          descarga a pedido (api_client.py)
--   comprador-licitacion    función que resuelve el comprador para la app
--
-- El cupo se reinicia a medianoche UTC (21:00 en Chile en horario de verano, 20:00
-- en invierno), así que el «día» de esta tabla es el día UTC.
--
-- Al pasar el 80% (8.000) se avisa UNA vez por día: `ticket_tomar_aviso`
-- devuelve verdadero sólo a la primera que lo pide. Lo usa
-- licitaciones-reciente para mandar el correo a quien es super_admin.
--
-- Ya corrido en Supabase el 2026-11-24. Se puede volver a correr.
-- ============================================================================

create table if not exists motor.consumo_ticket (
  dia         date        not null,
  origen      text        not null,
  consultas   integer     not null default 0,
  actualizado timestamptz not null default now(),
  primary key (dia, origen)
);

create table if not exists motor.consumo_aviso (
  dia       date        primary key,
  total     integer     not null,
  avisado   timestamptz not null default now()
);

alter table motor.consumo_ticket enable row level security;
alter table motor.consumo_aviso  enable row level security;
revoke all on motor.consumo_ticket, motor.consumo_aviso from public, anon, authenticated;

-- Tope diario de la API y umbral del aviso.
create or replace function motor.cupo_ticket()  returns integer language sql immutable as $$ select 10000 $$;
create or replace function motor.umbral_aviso() returns integer language sql immutable as $$ select 8000 $$;

-- Suma lo gastado y devuelve el total del día (todas las fuentes).
create or replace function public.ticket_sumar_consumo(p_origen text, p_consultas integer)
returns integer
language plpgsql
security definer
set search_path = motor, public, pg_catalog, pg_temp
as $$
declare
  v_dia   date := (now() at time zone 'UTC')::date;
  v_total integer;
begin
  if coalesce(p_consultas, 0) > 0 then
    insert into motor.consumo_ticket as c (dia, origen, consultas)
    values (v_dia, coalesce(nullif(btrim(p_origen), ''), 'otro'), p_consultas)
    on conflict (dia, origen) do update
       set consultas   = c.consultas + excluded.consultas,
           actualizado = now();
  end if;
  select coalesce(sum(consultas), 0) into v_total from motor.consumo_ticket where dia = v_dia;
  return v_total;
end $$;

-- Verdadero sólo la primera vez del día que el total pasa el umbral.
create or replace function public.ticket_tomar_aviso()
returns boolean
language plpgsql
security definer
set search_path = motor, public, pg_catalog, pg_temp
as $$
declare
  v_dia   date := (now() at time zone 'UTC')::date;
  v_total integer;
begin
  select coalesce(sum(consultas), 0) into v_total from motor.consumo_ticket where dia = v_dia;
  if v_total < motor.umbral_aviso() then
    return false;
  end if;
  insert into motor.consumo_aviso (dia, total) values (v_dia, v_total)
  on conflict (dia) do nothing;
  return found;
end $$;

-- Correos de quienes reciben el aviso (super_admin activos).
create or replace function public.ticket_correos_aviso()
returns text[]
language sql
stable
security definer
set search_path = motor, core, pg_catalog, pg_temp
as $$
  select coalesce(array_agg(u.email order by u.email), '{}')
    from core.usuarios u
   where u.rol = 'super_admin' and u.estado = 'activo' and u.email is not null;
$$;

-- Resumen de los últimos días, para revisar a mano o desde Themein.
create or replace view motor.consumo_ticket_diario as
  select dia,
         sum(consultas)::integer                                   as total,
         round(100.0 * sum(consultas) / motor.cupo_ticket(), 1)     as porcentaje,
         jsonb_object_agg(origen, consultas order by origen)        as por_origen
    from motor.consumo_ticket
   group by dia;

-- Las tres van en `public` porque es el esquema que exponen la API REST y el
-- cliente Python sin `.schema()`. Sólo el motor y las funciones (service_role)
-- las pueden llamar.
revoke all on function public.ticket_sumar_consumo(text, integer)  from public, anon, authenticated;
revoke all on function public.ticket_tomar_aviso()         from public, anon, authenticated;
revoke all on function public.ticket_correos_aviso()       from public, anon, authenticated;
grant execute on function public.ticket_sumar_consumo(text, integer) to service_role;
grant execute on function public.ticket_tomar_aviso()       to service_role;
grant execute on function public.ticket_correos_aviso()     to service_role;
grant usage on schema motor to service_role;
grant select on motor.consumo_ticket_diario to service_role;

-- La tabla crece una fila por día y origen: se limpia con lo demás.
create or replace function motor.limpiar_registros()
returns text
language plpgsql
security definer
set search_path = motor, cron, pg_catalog, pg_temp
as $$
declare
  n_cron int;
  n_disp int;
  n_cons int;
begin
  delete from cron.job_run_details where end_time < now() - interval '7 days';
  get diagnostics n_cron = row_count;
  delete from motor.barrido_disparos where pedido_en < now() - interval '30 days';
  get diagnostics n_disp = row_count;
  delete from motor.consumo_ticket where dia < current_date - 90;
  get diagnostics n_cons = row_count;
  delete from motor.consumo_aviso  where dia < current_date - 90;
  return format('job_run_details: %s, barrido_disparos: %s, consumo_ticket: %s', n_cron, n_disp, n_cons);
end $$;

-- ── Comprador de una licitación, para la app (función comprador-licitacion) ──
-- La app de escritorio ya no lleva el ticket: cuando necesita el comprador de
-- una licitación (dar de alta un cliente con el ID de una de sus compras) se lo
-- pide a esta función de Supabase, que primero mira la copia compartida y sólo
-- si no la tiene consulta la API.
--
-- Quién puede: usuarios activos de una empresa con MP Reader (código 'filt'),
-- o del equipo de RendApps. Tope global de 200 consultas al día a la API por
-- este camino, para que una cuenta comprometida no pueda gastar el cupo.

create or replace function public.comprador_licitacion_permitido(p_usuario uuid)
returns boolean
language sql
stable
security definer
set search_path = core, motor, pg_catalog, pg_temp
as $$
  select exists (
           select 1
             from core.usuarios u
            where u.id = p_usuario and u.estado = 'activo'
              and (u.empresa_id is null
                   or exists (select 1
                                from core.empresa_apps ea
                                join core.apps a on a.id = ea.app_id
                               where ea.empresa_id = u.empresa_id and a.codigo = 'filt'))
         )
     and coalesce((select consultas from motor.consumo_ticket
                    where dia = (now() at time zone 'UTC')::date
                      and origen = 'comprador-licitacion'), 0) < 200;
$$;

-- Lo que ya sabe la copia compartida (sin gastar ticket).
create or replace function public.comprador_licitacion_en_copia(p_codigo text)
returns table (rut text, nombre text)
language sql
stable
security definer
set search_path = public, pg_catalog, pg_temp
as $$
  select l.rut_organismo, l.organismo
    from public.licitaciones l
   where l.numero_adquisicion = upper(btrim(p_codigo))
     and coalesce(l.rut_organismo, '') <> ''
   limit 1;
$$;

revoke all on function public.comprador_licitacion_permitido(uuid) from public, anon, authenticated;
revoke all on function public.comprador_licitacion_en_copia(text)  from public, anon, authenticated;
grant execute on function public.comprador_licitacion_permitido(uuid) to service_role;
grant execute on function public.comprador_licitacion_en_copia(text)  to service_role;
