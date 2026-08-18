-- ---------------------------------------------------------------------------
-- sql_descarga_run.sql — Guardar la corrida de GitHub que atiende cada descarga
--
-- Correr una vez en el SQL Editor de Supabase (proyecto de la Filtradora).
--
-- Para qué: la app ya no habla con la API de GitHub —el token dejó de viajar
-- dentro del .exe—, así que tampoco puede preguntar «¿cuál es la corrida activa?»
-- para cancelarla. Ahora la anota el propio runner al arrancar, y la Edge
-- Function `disparar-descarga` la lee de aquí cuando toca cancelar.
--
-- Que el id salga de la base y no del cliente es lo que impide que alguien con
-- sesión cancele la corrida de otra empresa mandando un número a mano.
--
-- Es seguro repetirlo: `if not exists` no toca nada si la columna ya está.
-- ---------------------------------------------------------------------------

alter table public.descarga_estado
  add column if not exists run_id bigint;

comment on column public.descarga_estado.run_id is
  'id de la corrida de GitHub Actions que atiende esta descarga (github.run_id).';

-- Los runners entran con service_role (se salta RLS), pero igual necesitan
-- permiso sobre la tabla; ya estaba concedido y se deja explícito por si se
-- recrea el rol.
grant select, insert, update on public.descarga_estado to service_role;

-- Comprobación rápida: debe listar run_id.
select column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and table_name = 'descarga_estado'
  and column_name = 'run_id';
