// compra-agil-reciente — Cada 5 minutos, lo recién publicado en Compra Ágil.
//
// POR QUÉ EXISTE
//
// Mercado Público publica las Compras Ágiles en TANDAS cada 5 minutos (medido el
// 2026-09-11: 20 a las 13:45, 11 a las 13:50, 16 a las 13:55 y nada entre medio;
// la API oficial marca sus datos a esas mismas horas). Esta función las trae en
// cuanto salen, en vez de esperar al barrido de la madrugada.
//
// La programa pg_cron un minuto después de cada tanda (ver
// `sql_compra_agil_reciente.sql` en el repo de la app) y sólo entre 07:00 y
// 21:00 de Chile, que es cuando se publica casi todo.
//
// QUÉ HACE, Y QUÉ NO
//
//   · Pide al buscador las páginas de HOY, lo más nuevo primero, hasta dar con
//     una página en que ya se conoce todo.
//   · Le pregunta a la base cuáles de esos códigos no tiene (así no lee la
//     tabla: el tráfico es casi cero).
//   · Baja las fichas de lo nuevo y las guarda en la copia compartida con las
//     MISMAS filas que escribe el Python del motor (ver `ficha.ts`).
//
//   · NO detecta cierres ni pasos a 2do llamado: el orden «más reciente» es por
//     fecha de publicación, así que una cotización que cambia de llamado no
//     vuelve a subir. Eso lo sigue haciendo el barrido nocturno.
//   · NO filtra: los filtros se aplican en la app.
//
// Responde de inmediato y trabaja en segundo plano (`EdgeRuntime.waitUntil`):
// quien la llama es pg_net, y no tiene sentido tenerlo esperando.

import { filasDeFicha, type Fila, type Json } from "./ficha.ts";
import {
  CLAVE_HEADER_API,
  ESTADO_PUBLICADA,
  HEADERS_BUSCADOR,
  HILOS,
  PAGINA_LISTADO,
  URL_BUSCADOR,
} from "./buscador.ts";

declare const EdgeRuntime: { waitUntil(promesa: Promise<unknown>): void } | undefined;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";

// La clave de servicio: la antigua (JWT) si está; si no, la primera de las
// nuevas (`sb_secret_...`), que sólo va en `apikey`.
function claveServicio(): string {
  const antigua = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (antigua) return antigua;
  try {
    const nuevas = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}") as Record<string, string>;
    return Object.values(nuevas)[0] ?? "";
  } catch (_) {
    return "";
  }
}
const CLAVE = claveServicio();

const HEADERS: Record<string, string> = { ...HEADERS_BUSCADOR };
const CLAVE_BUSCADOR = Deno.env.get("MP_BUSCADOR_API_KEY");
if (CLAVE_BUSCADOR && CLAVE_HEADER_API) HEADERS[CLAVE_HEADER_API] = CLAVE_BUSCADOR;

const MAX_PAGINAS = 10;            // una tanda de hora punta son 2-4 páginas
const TIMEOUT_LISTADO_MS = 30_000; // el gateway del portal corta a los ~29 s
const TIMEOUT_FICHA_MS = 30_000;
const LIMITE_LISTADO_MS = 60_000;  // más que esto listando y no queda tiempo para fichas
const PRESUPUESTO_MS = 110_000;    // 150 s de pared en el plan gratis: margen para guardar

type Resumen = {
  hora: string;
  hoy: string;
  paginas: number;
  nuevas: number;
  guardadas: number;
  filas: number;
  sin_ficha: number;
  pendientes: number;
  errores: string[];
  ms: number;
};

async function rpc<T>(nombre: string, cuerpo: unknown): Promise<T> {
  const headers: Record<string, string> = { apikey: CLAVE, "Content-Type": "application/json" };
  if (!CLAVE.startsWith("sb_")) headers.Authorization = `Bearer ${CLAVE}`;
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${nombre}`, {
    method: "POST",
    headers,
    body: JSON.stringify(cuerpo),
  });
  if (!r.ok) throw new Error(`rpc ${nombre}: HTTP ${r.status} ${(await r.text()).slice(0, 300)}`);
  return (await r.json()) as T;
}

function hoyEnChile(): string {
  // «Hoy» de Chile, no de UTC: la función corre en UTC y de noche serían días distintos.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Santiago", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
}

async function listarPagina(pagina: number, hoy: string): Promise<string[]> {
  const url = new URL(URL_BUSCADOR);
  const params: Record<string, string> = {
    page_number: String(pagina),
    page_size: String(PAGINA_LISTADO),
    order_by: "recent",
    status: String(ESTADO_PUBLICADA),
    date_from: hoy,
    date_to: hoy,
  };
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const r = await fetch(url, { headers: HEADERS, signal: AbortSignal.timeout(TIMEOUT_LISTADO_MS) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = (await r.json()) as { payload?: { resultados?: Array<{ codigo?: unknown }> } };
  return (j.payload?.resultados ?? [])
    .map((it) => (it.codigo === null || it.codigo === undefined ? "" : String(it.codigo)))
    .filter((c) => c.length > 0);
}

async function obtenerFicha(codigo: string): Promise<{ [clave: string]: Json } | null> {
  // Como `_obtener_ficha` del motor: sólo cuenta si el portal dice success = "OK";
  // los 429 y 5xx se reintentan (aquí una vez: el tiempo de la función es corto).
  const url = new URL(URL_BUSCADOR);
  url.searchParams.set("action", "ficha");
  url.searchParams.set("code", codigo);
  for (let intento = 0; intento < 2; intento++) {
    try {
      const r = await fetch(url, { headers: HEADERS, signal: AbortSignal.timeout(TIMEOUT_FICHA_MS) });
      if (r.status === 200) {
        const j = (await r.json()) as { success?: string; payload?: { [clave: string]: Json } };
        return j.success === "OK" && j.payload ? j.payload : null;
      }
      if (![429, 500, 502, 503, 504].includes(r.status)) return null;
    } catch (_) {
      // tiempo agotado o red: se reintenta
    }
    await new Promise((listo) => setTimeout(listo, 2000));
  }
  return null;
}

async function repasar(): Promise<Resumen> {
  const t0 = Date.now();
  const hoy = hoyEnChile();
  const errores: string[] = [];
  const nuevas: string[] = [];
  let paginas = 0;

  // 1) Listar hasta una página que ya se conozca entera. NO se para en el primer
  //    código conocido: si una corrida anterior quedó a medias, habría huecos.
  for (let p = 1; p <= MAX_PAGINAS; p++) {
    if (Date.now() - t0 > LIMITE_LISTADO_MS) {
      errores.push("se acabó el tiempo de listar");
      break;
    }
    let codigos: string[];
    try {
      codigos = await listarPagina(p, hoy);
    } catch (e) {
      errores.push(`página ${p}: ${e}`);
      break;
    }
    paginas++;
    if (codigos.length === 0) break;
    const desconocidos = await rpc<string[]>("compra_agil_codigos_nuevos", { p_codigos: codigos });
    for (const c of desconocidos) if (!nuevas.includes(c)) nuevas.push(c);
    if (desconocidos.length === 0 || codigos.length < PAGINA_LISTADO) break;
  }

  // 2) Fichas de lo nuevo, en paralelo, sin pasarse del presupuesto. Lo que no
  //    alcance no se guarda, así que la corrida siguiente lo vuelve a ver nuevo.
  const fichas: Array<{ codigo: string; payload: { [clave: string]: Json } }> = [];
  const sinFicha: string[] = [];
  let siguiente = 0;
  const trabajador = async () => {
    while (siguiente < nuevas.length && Date.now() - t0 < PRESUPUESTO_MS) {
      const codigo = nuevas[siguiente++];
      const payload = await obtenerFicha(codigo);
      if (payload) fichas.push({ codigo, payload });
      else sinFicha.push(codigo);
    }
  };
  await Promise.all(Array.from({ length: HILOS }, trabajador));

  // 3) Filas iguales a las del Python, y a la base de una vez.
  const filas: Fila[] = [];
  const codigos: string[] = [];
  for (const { codigo, payload } of fichas) {
    codigos.push(codigo);
    filas.push(...filasDeFicha(payload));
  }
  const resumen: Resumen = {
    hora: new Date().toISOString(),
    hoy,
    paginas,
    nuevas: nuevas.length,
    guardadas: fichas.length,
    filas: filas.length,
    sin_ficha: sinFicha.length,
    pendientes: nuevas.length - fichas.length - sinFicha.length,
    errores,
    ms: Date.now() - t0,
  };
  await rpc<number>("compra_agil_guardar_tanda", { p_codigos: codigos, p_filas: filas, p_detalle: resumen });
  return resumen;
}

async function repasarYAnotar(): Promise<void> {
  try {
    console.log(JSON.stringify(await repasar()));
  } catch (e) {
    console.error("repaso fallido:", e);
    // Que quede constancia del fallo: sin esto, el registro seguiría diciendo
    // que todo fue bien la última vez.
    try {
      await rpc<number>("compra_agil_guardar_tanda", {
        p_codigos: [], p_filas: [],
        p_detalle: { hora: new Date().toISOString(), errores: [String(e)] },
      });
    } catch (_) {
      // sin base no hay dónde anotarlo
    }
  }
}

Deno.serve(async (req) => {
  // Sólo la llama pg_cron, con la clave que genera la base (nadie la copia):
  // sin esto cualquiera podría gastar las llamadas del plan o cargar al portal.
  const secreto = req.headers.get("x-cron-secreto") ?? "";
  let autorizado = false;
  try {
    autorizado = secreto.length > 0 &&
      (await rpc<boolean>("compra_agil_reciente_autorizado", { p_secreto: secreto }));
  } catch (e) {
    console.error("no se pudo comprobar la clave:", e);
  }
  if (!autorizado) return new Response("no autorizado", { status: 401 });

  const trabajo = repasarYAnotar();
  if (typeof EdgeRuntime !== "undefined" && EdgeRuntime) {
    EdgeRuntime.waitUntil(trabajo);
    return Response.json({ aceptado: true }, { status: 202 });
  }
  await trabajo;
  return Response.json({ aceptado: true });
});
