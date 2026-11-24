// comprador-licitacion — El comprador de una licitación, para la app.
//
// POR QUÉ EXISTE
//
// La app de escritorio llevaba el ticket de Mercado Público dentro del
// instalador, que es público: cualquiera podía sacarlo y gastar el cupo (o hacer
// que bloquearan el ticket, que es personal). Lo único que la app hacía con él
// era averiguar el comprador de una licitación para darlo de alta como cliente
// (`compradores.registrar_desde_codigo`). Ahora se lo pregunta a esta función.
//
// QUÉ HACE
//
//   · Exige sesión (verify_jwt) y que la persona use MP Reader; el tope diario
//     de este camino lo pone la base (`comprador_licitacion_permitido`).
//   · Primero mira la copia compartida de licitaciones: si está, no gasta ticket.
//   · Si no, una consulta a la API oficial, que suma al contador del ticket.
//
// Responde { rut, nombre, error } con los mismos textos que daba la app.

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const TICKET = Deno.env.get("MP_TICKET") ?? "";
const URL_API = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json";

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

function cabecerasServicio(): Record<string, string> {
  const h: Record<string, string> = { apikey: CLAVE, "Content-Type": "application/json" };
  if (!CLAVE.startsWith("sb_")) h.Authorization = `Bearer ${CLAVE}`;
  return h;
}

async function rpc<T>(nombre: string, cuerpo: unknown): Promise<T> {
  const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${nombre}`, {
    method: "POST",
    headers: cabecerasServicio(),
    body: JSON.stringify(cuerpo),
  });
  if (!r.ok) throw new Error(`rpc ${nombre}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** El id de quien llama, leído de su propio JWT a través de Auth. */
async function usuarioDe(req: Request): Promise<string | null> {
  const auth = req.headers.get("Authorization") ?? "";
  if (!auth.startsWith("Bearer ")) return null;
  const r = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: CLAVE, Authorization: auth },
  });
  if (!r.ok) return null;
  const u = (await r.json()) as { id?: string };
  return u.id ?? null;
}

function responder(rut: string, nombre: string, error: string, status = 200): Response {
  return Response.json({ rut, nombre, error }, { status });
}

/** El ticket va en la URL: nunca puede llegar a un registro. */
function sinTicket(texto: string): string {
  return TICKET ? texto.split(TICKET).join("***") : texto;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("Método no permitido", { status: 405 });

  let codigo = "";
  try {
    const cuerpo = (await req.json()) as { codigo?: unknown };
    codigo = String(cuerpo.codigo ?? "").trim().toUpperCase();
  } catch (_) {
    // sin cuerpo válido: se responde abajo
  }
  if (!codigo) return responder("", "", "Escriba el ID de una licitación.", 400);

  try {
    const usuario = await usuarioDe(req);
    if (!usuario || !(await rpc<boolean>("comprador_licitacion_permitido", { p_usuario: usuario }))) {
      return responder("", "", "No se pudo consultar el comprador. Inténtalo más tarde.", 403);
    }

    // 1) La copia compartida, sin gastar ticket.
    const enCopia = await rpc<{ rut: string; nombre: string }[]>(
      "comprador_licitacion_en_copia", { p_codigo: codigo });
    if (enCopia?.length) return responder(enCopia[0].rut ?? "", enCopia[0].nombre ?? "", "");

    // 2) La API oficial.
    if (!TICKET) return responder("", "", "No se pudo consultar Mercado Público.", 500);
    const url = new URL(URL_API);
    url.searchParams.set("codigo", codigo);
    url.searchParams.set("ticket", TICKET);
    let r: Response;
    try {
      r = await fetch(url, { signal: AbortSignal.timeout(30_000) });
    } finally {
      await rpc<number>("ticket_sumar_consumo", { p_origen: "comprador-licitacion", p_consultas: 1 })
        .catch(() => 0);
    }
    if (!r.ok) {
      return responder("", "", `Mercado Público respondió ${r.status}. Inténtalo de nuevo.`);
    }
    const datos = (await r.json()) as { Listado?: { Comprador?: Record<string, string> }[]; Mensaje?: string };
    const listado = datos.Listado ?? [];
    if (!listado.length) {
      // La API contesta 200 con un `Mensaje` cuando el código no existe o
      // cuando el ticket está saturado; se distinguen mirando si hay listado.
      const aviso = String(datos.Mensaje ?? "").trim();
      return responder("", "", aviso || `Mercado Público no encontró la licitación ${codigo}.`);
    }
    const comprador = listado[0].Comprador ?? {};
    return responder(comprador.RutUnidad ?? "", comprador.NombreOrganismo ?? "", "");
  } catch (e) {
    console.error("[comprador-licitacion]", sinTicket(String(e)));
    return responder("", "", "No se pudo consultar Mercado Público.", 500);
  }
});
