-- ---------------------------------------------------------------------------
-- sql_compra_agil_seguimiento.sql — Filtrado en la nube + seguimiento de estado
--
-- Correr una vez en el SQL Editor de Supabase (proyecto de la Filtradora).
--
-- Qué cambia:
--
-- Hasta ahora `compra_agil` guardaba una copia compartida de TODO (≈38.000
-- filas, 15,5 MB) y cada app se la bajaba y le aplicaba sus ~116 reglas al
-- entrar al módulo. El mismo trabajo, repetido por empresa y por visita.
--
-- Ahora el barrido filtra UNA vez por empresa y deja el resultado aquí. La app
-- lee este conjunto —dos órdenes de magnitud más chico— y no filtra nada.
--
-- Lo que eso habilita, y era el punto: como el conjunto filtrado es pequeño, el
-- barrido diurno puede permitirse volver a preguntarle al portal por CADA una
-- de esas cotizaciones y mantener su estado al día. Antes eso sólo pasaba en el
-- barrido completo de la madrugada, y entremedio la pantalla no podía decir más
-- que «Revisar en web».
-- ---------------------------------------------------------------------------


-- === 1. El conjunto filtrado de cada empresa ===============================
--
-- Mismo grano que `compra_agil`: una fila por (cotización, producto). La
-- pantalla muestra las cotizaciones arriba y los productos de la seleccionada
-- abajo, así que necesita las dos cosas.
create table if not exists public.compra_agil_seguimiento (
  id                       bigserial primary key,
  empresa_id               uuid not null,

  -- Copia de la cotización, tal como la dejó el barrido en `compra_agil`.
  numero_adquisicion       text not null,
  nombre_adquisicion       text,
  organismo                text,
  rut_organismo            text,
  fecha_publicacion        text,
  fecha_cierre             text,
  fecha_cierre_1er_llamado text,
  fecha_cierre_2do_llamado text,
  llamado                  text,
  cantidad                 text,
  descripcion_producto     text,
  nombre_producto          text,

  -- Por qué entró: lo produce `processor.filtrar_licitaciones`. Sirve para que
  -- el usuario entienda el match y para depurar un filtro que trae de más.
  filtro_aplicado          text,
  columna_encontrada       text,
  detalle_coincidencia     text,

  -- Seguimiento.
  --
  -- `primera_deteccion` es el dato que NO se puede perder al refiltrar: es lo
  -- que permite decir «esto apareció el martes» aunque el conjunto se rehaga
  -- tres veces al día.
  primera_deteccion        timestamptz not null default now(),
  ultima_revision          timestamptz,
  estado_seguimiento       text not null default 'vigente'
                             check (estado_seguimiento in ('vigente', 'cerrada')),

  -- Permite que la app se traiga sólo lo que cambió, como en `compra_agil`.
  actualizado              timestamptz not null default now()
);

create index if not exists compra_agil_seg_empresa_actualizado_idx
  on public.compra_agil_seguimiento (empresa_id, actualizado);

create index if not exists compra_agil_seg_empresa_codigo_idx
  on public.compra_agil_seguimiento (empresa_id, numero_adquisicion);

-- El seguimiento diurno recorre justo esto: lo vigente de una empresa.
create index if not exists compra_agil_seg_vigentes_idx
  on public.compra_agil_seguimiento (empresa_id, estado_seguimiento)
  where estado_seguimiento = 'vigente';


alter table public.compra_agil_seguimiento enable row level security;

drop policy if exists compra_agil_seg_lectura on public.compra_agil_seguimiento;
drop policy if exists compra_agil_seg_escritura on public.compra_agil_seguimiento;

-- Leer: sólo lo de la propia empresa. A diferencia de `compra_agil`, esto NO es
-- público: son las cotizaciones que le interesan a una empresa concreta, que es
-- justamente su criterio comercial.
create policy compra_agil_seg_lectura on public.compra_agil_seguimiento
  for select
  using (
    empresa_id = core.empresa_del_usuario()
    or core.es_super_admin()
  );

-- Escribir: normalmente lo hace el barrido con service_role (se salta RLS).
-- Se permite también a la propia empresa porque el botón «Refiltrar ahora» de
-- la app rehace el conjunto sin esperar al siguiente barrido. Sigue acotado a
-- su empresa: nadie puede tocar el conjunto de otra.
create policy compra_agil_seg_escritura on public.compra_agil_seguimiento
  for all
  using (
    empresa_id = core.empresa_del_usuario()
    or core.es_super_admin()
  )
  with check (
    empresa_id = core.empresa_del_usuario()
    or core.es_super_admin()
  );

grant select, insert, update, delete on public.compra_agil_seguimiento to authenticated;
grant usage, select on sequence public.compra_agil_seguimiento_id_seq to authenticated;
grant select, insert, update, delete on public.compra_agil_seguimiento to service_role;
grant usage, select on sequence public.compra_agil_seguimiento_id_seq to service_role;


-- === 2. Preferencias por empresa ===========================================
--
-- Por ahora una sola: qué llamados seguir. Una empresa que sólo compite en el
-- segundo no gana nada con que el barrido vigile los primeros.
create table if not exists public.preferencias_empresa (
  empresa_id          uuid primary key,
  llamado_seguimiento text not null default 'todas'
                        check (llamado_seguimiento in ('primer', 'segundo', 'todas')),
  actualizado         timestamptz not null default now()
);

alter table public.preferencias_empresa enable row level security;

drop policy if exists preferencias_empresa_lectura on public.preferencias_empresa;

-- Leer: cualquiera de la empresa (la pantalla la muestra a todos).
create policy preferencias_empresa_lectura on public.preferencias_empresa
  for select
  using (
    empresa_id = core.empresa_del_usuario()
    or core.es_super_admin()
  );

-- Escribir NO lleva policy a propósito: se hace por la función de abajo, que es
-- la que comprueba el rol. Sin policy de escritura, un usuario normal no puede
-- saltarse esa comprobación escribiendo directo en la tabla.
grant select on public.preferencias_empresa to authenticated;
grant select, insert, update on public.preferencias_empresa to service_role;


-- === 3. Guardar la preferencia (sólo administradores) ======================
--
-- Mismo patrón que `core.cambiar_rol_usuario` en sql_equipo.sql: quién pregunta
-- sale de auth.uid(), nunca de un parámetro, y la empresa se deduce de ahí. Así
-- nadie puede cambiarle la preferencia a otra empresa pasando su id.
create or replace function public.guardar_preferencia_llamado(p_valor text)
returns text
language plpgsql
security definer
set search_path = public, core, pg_catalog, pg_temp
as $$
declare
  v_id      uuid := auth.uid();
  v_yo      core.usuarios;
begin
  if v_id is null then
    raise exception 'Necesitas haber iniciado sesión.' using errcode = '42501';
  end if;

  if p_valor is null or p_valor not in ('primer', 'segundo', 'todas') then
    raise exception 'Valor no válido: %', coalesce(p_valor, '(vacío)') using errcode = '22023';
  end if;

  select * into v_yo from core.usuarios u where u.id = v_id;
  if not found then
    raise exception 'Tu usuario no está en el registro central.' using errcode = '42501';
  end if;

  if v_yo.rol not in ('admin', 'super_admin') then
    raise exception 'Sólo un administrador puede cambiar esta preferencia.'
      using errcode = '42501';
  end if;

  if v_yo.empresa_id is null then
    raise exception 'Tu usuario no está asociado a ninguna empresa.' using errcode = '42501';
  end if;

  insert into public.preferencias_empresa (empresa_id, llamado_seguimiento, actualizado)
  values (v_yo.empresa_id, p_valor, now())
  on conflict (empresa_id) do update
    set llamado_seguimiento = excluded.llamado_seguimiento,
        actualizado         = now();

  return p_valor;
end;
$$;

revoke all on function public.guardar_preferencia_llamado(text) from public;
grant execute on function public.guardar_preferencia_llamado(text) to authenticated;


-- === Comprobación ==========================================================
-- Deben aparecer las dos tablas y la función.
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name in ('compra_agil_seguimiento', 'preferencias_empresa')
order by table_name;

select routine_name
from information_schema.routines
where routine_schema = 'public' and routine_name = 'guardar_preferencia_llamado';
