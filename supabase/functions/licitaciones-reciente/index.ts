// licitaciones-reciente — Cada minuto, las licitaciones recién publicadas.
//
// POR QUÉ EXISTE
//
// Las licitaciones entraban sólo con el barrido de la madrugada. Medido el
// 2026-09-11, la API oficial muestra una licitación nueva entre 0,1 y 1 minuto
// después de publicada, y el listado del día contesta en ~0,4 s. Esta función
// la trae en ese momento.
//
// La programa pg_cron cada minuto (ver `sql_licitaciones_reciente.sql` en el
// repo de la app), sólo de 07:00 a 21:00 de Chile.
//
// EL TICKET
//
// Es el de Amilab (personal del usuario), el mismo del barrido nocturno. La API
// limita a 10.000 consultas diarias por ticket: esto gasta ~840 listados + una
// ficha por licitación nueva (~450), es decir ~13% del cupo. Va en la URL, así
// que NUNCA se escribe en un registro: ver `sinTicket`.
//
// QUÉ HACE, Y QUÉ NO
//
//   · Pide el listado de hoy, se queda con las publicadas y le pregunta a la
//     base cuáles no tiene todavía (así no lee la tabla).
//   · El listado del día trae también licitaciones publicadas OTRO día (a las
//     15:00 del 2026-09-11 aparecieron 98 de golpe). Su ficha se pide una sola
//     vez: la base las recuerda en `licitaciones_vistas` y no las vuelve a dar
//     por nuevas ese día.
//   · Guarda las filas IGUAL que el Python del motor (ver `ficha.ts`).
//   · NO detecta cambios de estado: eso lo hace el barrido nocturno.
//   · NO filtra: los filtros se aplican en la app.

import { type Dict, esDict, filasDeRespuesta, type Fila, HILOS, URL_API } from "./ficha.ts";

declare const EdgeRuntime: { waitUntil(promesa: Promise<unknown>): void } | undefined;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const TICKET = Deno.env.get("MP_TICKET") ?? "";

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

const TIMEOUT_MS = 15_000;       // como `procesar_licitacion`
const PRESUPUESTO_MS = 45_000;   // corre cada minuto: tiene que acabar antes de la siguiente
const MAX_FICHAS = 60;           // una tanda rara (las 98 de otro día) se reparte en varias pasadas

/** El ticket va en la URL, y los errores de red de Deno citan la URL. Esto lo
 *  quita de cualquier texto antes de que llegue a un registro. */
function sinTicket(texto: string): string {
  return TICKET ? texto.split(TICKET).join("***") : texto;
}

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

function hoyEnChile(): { iso: string; ddmmyyyy: string } {
  const iso = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Santiago", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());
  const [a, m, d] = iso.split("-");
  return { iso, ddmmyyyy: `${d}${m}${a}` };
}

/** GET a la API oficial. -> el JSON, o null si no contestó bien. */
async function pedir(params: Record<string, string>): Promise<Dict | null> {
  const url = new URL(URL_API);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  url.searchParams.set("ticket", TICKET);
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!r.ok) return null;
    const j = await r.json();
    return esDict(j) ? j : null;
  } catch (_) {
    return null;
  }
}

type Resumen = {
  hora: string;
  hoy: string;
  en_el_listado: number;
  publicadas: number;
  nuevas: number;
  guardadas: number;
  filas: number;
  otra_fecha: number;
  vacias: number;
  fallidas: number;
  pendientes: number;
  errores: string[];
  ms: number;
};

async function repasar(): Promise<Resumen> {
  const t0 = Date.now();
  const { iso, ddmmyyyy } = hoyEnChile();
  const errores: string[] = [];

  // 1) El listado de hoy. Una sola consulta, sin páginas.
  const lista = await pedir({ fecha: ddmmyyyy });
  const listado = lista && Array.isArray(lista.Listado) ? lista.Listado : null;
  if (!listado) {
    const mensaje = lista && typeof lista.Mensaje === "string" ? lista.Mensaje : "sin respuesta";
    throw new Error(`el listado no respondió (${mensaje})`);
  }
  const publicadas = listado
    .filter((x) => esDict(x) && x.CodigoEstado === 5 && x.CodigoExterno)
    .map((x) => String((x as Dict).CodigoExterno));

  // 2) Cuáles no tiene la base (ni se descartaron ya hoy).
  const nuevas = publicadas.length
    ? await rpc<string[]>("licitaciones_codigos_nuevos", { p_codigos: publicadas, p_dia: iso })
    : [];

  // 3) Fichas, con 3 a la vez como mucho (el ticket no aguanta más).
  const codigos: string[] = [];
  const filas: Fila[] = [];
  const vistos: string[] = [];
  let otraFecha = 0, vacias = 0, fallidas = 0;
  const cola = nuevas.slice(0, MAX_FICHAS);
  let siguiente = 0;

  const trabajador = async () => {
    while (siguiente < cola.length && Date.now() - t0 < PRESUPUESTO_MS) {
      const codigo = cola[siguiente++];
      let resultado: ReturnType<typeof filasDeRespuesta> | null = null;
      for (let intento = 0; intento < 2 && !resultado; intento++) {
        const datos = await pedir({ codigo });
        if (datos) {
          try {
            const r = filasDeRespuesta(datos, codigo, iso);
            if (r.estado !== "REINTENTAR") resultado = r;
          } catch (_) {
            // respuesta rara: se trata como fallo, igual que el Python
          }
        }
        if (!resultado) await new Promise((listo) => setTimeout(listo, 2000));
      }
      if (!resultado) {
        fallidas++;                       // la próxima pasada la vuelve a intentar
      } else if (resultado.estado === "OK") {
        codigos.push(codigo);
        filas.push(...resultado.filas);
        vistos.push(codigo);
      } else {
        if (resultado.estado === "OTRA_FECHA") otraFecha++;
        else vacias++;
        vistos.push(codigo);               // no volver a pedirla hoy
      }
    }
  };
  await Promise.all(Array.from({ length: HILOS }, trabajador));

  const resumen: Resumen = {
    hora: new Date().toISOString(),
    hoy: iso,
    en_el_listado: listado.length,
    publicadas: publicadas.length,
    nuevas: nuevas.length,
    guardadas: codigos.length,
    filas: filas.length,
    otra_fecha: otraFecha,
    vacias,
    fallidas,
    pendientes: nuevas.length - codigos.length - otraFecha - vacias - fallidas,
    errores,
    ms: Date.now() - t0,
  };
  await rpc<number>("licitaciones_guardar_tanda", {
    p_codigos: codigos, p_filas: filas, p_vistos: vistos, p_dia: iso, p_detalle: resumen,
  });
  return resumen;
}

async function repasarYAnotar(): Promise<void> {
  try {
    console.log(JSON.stringify(await repasar()));
  } catch (e) {
    const error = sinTicket(String(e));
    console.error("repaso fallido:", error);
    try {
      await rpc<number>("licitaciones_guardar_tanda", {
        p_codigos: [], p_filas: [], p_vistos: [], p_dia: hoyEnChile().iso,
        p_detalle: { hora: new Date().toISOString(), errores: [error] },
      });
    } catch (_) {
      // sin base no hay dónde anotarlo
    }
  }
}

Deno.serve(async (req) => {
  // Sólo la llama pg_cron, con la clave que genera la base: sin esto cualquiera
  // podría gastar el ticket (que es personal) y las llamadas del plan.
  const secreto = req.headers.get("x-cron-secreto") ?? "";
  let autorizado = false;
  try {
    autorizado = secreto.length > 0 &&
      (await rpc<boolean>("licitaciones_reciente_autorizado", { p_secreto: secreto }));
  } catch (e) {
    console.error("no se pudo comprobar la clave:", sinTicket(String(e)));
  }
  if (!autorizado) return new Response("no autorizado", { status: 401 });
  if (!TICKET) return new Response("falta MP_TICKET en los secretos de la función", { status: 500 });

  const trabajo = repasarYAnotar();
  if (typeof EdgeRuntime !== "undefined" && EdgeRuntime) {
    EdgeRuntime.waitUntil(trabajo);
    return Response.json({ aceptado: true }, { status: 202 });
  }
  await trabajo;
  return Response.json({ aceptado: true });
});
