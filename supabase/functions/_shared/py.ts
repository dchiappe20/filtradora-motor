// py.ts — Cómo haría Python ciertas cosas, para que las funciones de Supabase
// escriban EXACTAMENTE lo mismo que el motor.
//
// Lo usan `compra-agil-reciente` y `licitaciones-reciente`, que de día escriben
// en las mismas tablas que de noche escribe el Python. Lo vigilan las pruebas
// de contrato (pruebas/contrato_*.test.ts), que pasan fichas reales y raras por
// los dos lados y exigen el mismo resultado.
//
// Rarezas de Python que se imitan a propósito:
//   · `str(None)` es "None", no vacío.
//   · `.strip()` quita los separadores U+001C a U+001F y U+0085, que `trim()` de
//     JavaScript no quita, y NO quita U+FEFF, que `trim()` sí.
//   · `a or b` da por falsos "", 0, [] y {}, no sólo null.
//
// Sin APIs de Deno: así las pruebas lo importan desde Node. Y sin caracteres
// invisibles en el fuente: la lista de espacios va por código.

export type Json = null | boolean | number | string | Json[] | { [clave: string]: Json };
export type Dict = { [clave: string]: Json };
export type Fila = { [columna: string]: string | null };

// Los caracteres que `str.strip()` de Python considera espacio (str.isspace()).
const ESPACIOS_PY = String.fromCharCode(
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
);

/** dict.get de Python: si la clave está, su valor —aunque sea null—; si no, el defecto. */
export function get(obj: Dict, clave: string, porDefecto: Json): Json {
  return Object.prototype.hasOwnProperty.call(obj, clave) ? obj[clave] : porDefecto;
}

export function esDict(v: Json | undefined): v is Dict {
  return v !== null && v !== undefined && typeof v === "object" && !Array.isArray(v);
}

/** Veracidad de Python. */
export function verdadero(v: Json | undefined): boolean {
  if (v === null || v === undefined) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0 && !Number.isNaN(v);
  if (typeof v === "string") return v.length > 0;
  if (Array.isArray(v)) return v.length > 0;
  return Object.keys(v).length > 0;
}

/** str() de Python. JavaScript no distingue 5 de 5.0 al leer el JSON: un entero sale "5". */
export function pyStr(v: Json | undefined): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** str.strip() de Python. */
export function pyStrip(s: string): string {
  let i = 0;
  let j = s.length;
  while (i < j && ESPACIOS_PY.includes(s[i])) i++;
  while (j > i && ESPACIOS_PY.includes(s[j - 1])) j--;
  return s.slice(i, j);
}

/** datos_nube._limpiar_celda: None y NaN a "", el resto con str(). */
export function limpiar(v: Json | undefined): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number" && Number.isNaN(v)) return "";
  return pyStr(v);
}

/** _texto_cantidad del motor: None a "", un entero como entero (también si vino
 *  como 3.0), el resto con str(). */
export function textoCantidad(v: Json | undefined): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** drop_duplicates(subset=["Numero Adquisición", "Descripción Producto"]): se queda la primera. */
export function sinRepetir(filas: Fila[]): Fila[] {
  const vistas = new Set<string>();
  const salida: Fila[] = [];
  for (const fila of filas) {
    const clave = JSON.stringify([fila.numero_adquisicion, fila.descripcion_producto]);
    if (vistas.has(clave)) continue;
    vistas.add(clave);
    salida.push(fila);
  }
  return salida;
}
