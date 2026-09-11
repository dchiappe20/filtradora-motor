// ficha.ts — Respuesta de la API oficial de licitaciones -> filas de `licitaciones`.
//
// ES UN ESPEJO, Y TIENE QUE SEGUIR SIÉNDOLO, de lo que hace el Python del motor:
//
//   api_client._filas_de_respuesta    -> las filas y el estado de una respuesta
//   DataFrame.drop_duplicates         -> sin repetir (código, descripción)
//   datos_nube._a_filas               -> cada valor a texto, como lo guarda la tabla
//
// De día escribe esta función (cada minuto) y de noche el barrido, EN LA MISMA
// TABLA. `pruebas/contrato_licitaciones.test.ts` pasa respuestas reales y raras
// por los dos lados y exige el mismo resultado. Las rarezas de Python que se
// imitan están en `../_shared/py.ts`.

import {
  type Dict,
  esDict,
  type Fila,
  get,
  type Json,
  limpiar,
  pyStr,
  pyStrip,
  sinRepetir,
  textoCantidad,
  verdadero,
} from "../_shared/py.ts";

export type { Dict, Fila, Json };

// Constantes que tienen que coincidir con las del Python (las comprueba la prueba).
export const URL_API = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json";
// MAX_WORKERS_LICIT: la API limita por ticket; medido que con 6 hilos se pierde
// ~25% de las licitaciones y con 3 ninguna.
export const HILOS = 3;

// Descripciones que en realidad no dicen nada (se compara en minúsculas).
const VACIOS = new Set(["", "sin descripcion", "sin descripción", "n/a", "none"]);

/**
 * Qué dice UNA respuesta de la API sobre una licitación.
 *
 *   REINTENTAR  el portal contestó con un «Mensaje» y sin datos: está saturado.
 *   VACIO       el portal no tiene detalle de esa licitación.
 *   OTRA_FECHA  el listado del día también trae licitaciones publicadas otro día.
 *   OK          las filas, ya como las guarda la tabla.
 *
 * Lanza donde el Python lanzaría (p. ej. `Fechas` nula): quien llama lo toma
 * como un fallo y reintenta, igual que `procesar_licitacion`.
 */
export function filasDeRespuesta(
  datos: Dict,
  codigo: string,
  fechaExacta: string,
): { filas: Fila[]; estado: "OK" | "VACIO" | "OTRA_FECHA" | "REINTENTAR" } {
  if (Object.prototype.hasOwnProperty.call(datos, "Mensaje") && !verdadero(get(datos, "Listado", null))) {
    return { filas: [], estado: "REINTENTAR" };
  }
  const detalles = get(datos, "Listado", []);
  if (!verdadero(detalles)) return { filas: [], estado: "VACIO" };
  if (!Array.isArray(detalles) || !esDict(detalles[0])) throw new Error("Listado inesperado");
  const detalle = detalles[0];

  const fechas = get(detalle, "Fechas", {});
  if (!esDict(fechas)) throw new Error("Fechas inesperadas");
  const fPub = pyStr(get(fechas, "FechaPublicacion", ""));
  if (!fPub.startsWith(fechaExacta)) return { filas: [], estado: "OTRA_FECHA" };

  const compradorCrudo = get(detalle, "Comprador", {});
  const comprador: Dict = verdadero(compradorCrudo) && esDict(compradorCrudo) ? compradorCrudo : {};
  const organismo = get(comprador, "NombreOrganismo", "Desconocido");
  const rut = get(comprador, "RutUnidad", "");
  const fCierre = get(fechas, "FechaCierre", "");
  const nombre = get(detalle, "Nombre", "");

  const filas: Fila[] = [];
  const itemsData = get(detalle, "Items", null);
  if (esDict(itemsData)) {
    const listado = get(itemsData, "Listado", null);
    let items: Json[];
    if (esDict(listado)) items = [listado];
    else if (!verdadero(listado)) items = [];
    else if (Array.isArray(listado)) items = listado;
    else throw new Error("Items.Listado inesperado");

    for (const item of items) {
      if (!esDict(item)) throw new Error("ítem inesperado");
      const descReal = pyStrip(pyStr(get(item, "Descripcion", ""))).split("_x000D_").join("");
      const nombreGenerico = pyStrip(pyStr(get(item, "NombreProducto", ""))).split("_x000D_").join("");
      const descFinal = descReal && !VACIOS.has(descReal.toLowerCase()) ? descReal : nombreGenerico;
      // Sólo para filtrar: el nombre genérico y la descripción, sin los que no dicen nada.
      const partes = [nombreGenerico, descReal].filter((p) => p && !VACIOS.has(p.toLowerCase()));

      filas.push({
        numero_adquisicion: limpiar(codigo),
        nombre_adquisicion: limpiar(nombre),
        organismo: limpiar(organismo),
        rut_organismo: limpiar(rut),
        fecha_publicacion: fPub,
        fecha_cierre: limpiar(fCierre),
        cantidad: textoCantidad(get(item, "Cantidad", 0)),
        descripcion_producto: descFinal,
        texto_filtrado: partes.join(" "),
        empresa_id: null,
      });
    }
  }
  return { filas: sinRepetir(filas), estado: "OK" };
}
