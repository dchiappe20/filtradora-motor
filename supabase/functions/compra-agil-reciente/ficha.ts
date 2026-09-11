// ficha.ts — Ficha del buscador de Compra Ágil -> filas de la tabla `compra_agil`.
//
// ES UN ESPEJO, Y TIENE QUE SEGUIR SIÉNDOLO. Lo mismo lo hace el Python del
// motor en el barrido nocturno:
//
//   compra_agil_api._procesar_ficha   -> las filas de una ficha
//   DataFrame.drop_duplicates         -> sin repetir (código, descripción)
//   datos_nube._a_filas               -> cada valor a texto, como lo guarda la tabla
//
// De día escribe esta función y de noche el Python, EN LA MISMA TABLA. Si
// difieren en un detalle, la misma cotización queda distinta según la hora a la
// que llegó (con las fechas de licitaciones ya pasó: cuatro formatos que el
// filtro por rango no supo leer). Por eso `pruebas/contrato_fichas.test.ts`
// pasa fichas reales y raras por los dos lados y exige el mismo resultado; corre
// en GitHub en cada cambio. Las rarezas de Python que se imitan están en
// `../_shared/py.ts`.

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

export type { Fila, Json };

function textoLlamado(v: Json): string {
  // _LLAMADO_TEXTOS.get(...): en un dict de Python, 1 == 1.0 == True.
  if (v === 1 || v === true) return "1er llamado";
  if (v === 2) return "2do llamado";
  return "";
}

/** Las filas que el Python guardaría para esta ficha (el `payload` del buscador). */
export function filasDeFicha(ficha: Dict): Fila[] {
  const codigo = get(ficha, "codigo", "");
  const nombre = get(ficha, "nombre", "");
  const descripcionGeneral = get(ficha, "descripcion", "");
  const infoCruda = get(ficha, "informacion_institucion", null);
  const info: Dict = verdadero(infoCruda) && esDict(infoCruda) ? infoCruda : {};
  const organismo = get(info, "organismo_comprador", "");
  const rut = get(info, "rut_organismo_comprador", "");
  const fPub = get(ficha, "fecha_publicacion", "");
  const cierre1 = get(ficha, "fecha_cierre_primer_llamado", null);
  const fCierre1 = verdadero(cierre1) ? cierre1 : get(ficha, "fecha_cierre", "");
  const cierre2 = get(ficha, "fecha_cierre_segundo_llamado", null);
  const fCierre2 = verdadero(cierre2) ? cierre2 : "";
  const llamado = textoLlamado(get(ficha, "estado_convocatoria", null));

  const crudos = get(ficha, "productos_solicitados", null);
  let productos: Json[] = verdadero(crudos) && Array.isArray(crudos) ? crudos : [];
  if (productos.length === 0) {
    productos = [{ nombre: "", descripcion: descripcionGeneral, cantidad: "" }];
  }

  const filas: Fila[] = [];
  for (const p of productos) {
    const prod: Dict = esDict(p) ? p : {};
    const nombreProd = pyStrip(pyStr(get(prod, "nombre", "")));
    const descProd = pyStrip(pyStr(get(prod, "descripcion", ""))).split("_x000D_").join("");
    const descFinal = descProd ? descProd : nombreProd;

    filas.push({
      numero_adquisicion: limpiar(codigo),
      nombre_adquisicion: limpiar(nombre),
      organismo: limpiar(organismo),
      rut_organismo: limpiar(rut),
      fecha_publicacion: limpiar(fPub),
      fecha_cierre: limpiar(fCierre1),
      fecha_cierre_1er_llamado: limpiar(fCierre1),
      fecha_cierre_2do_llamado: limpiar(fCierre2),
      llamado: llamado,
      cantidad: textoCantidad(get(prod, "cantidad", "")),
      descripcion_producto: descFinal,
      nombre_producto: nombreProd,
      empresa_id: null,
    });
  }
  return sinRepetir(filas);
}
