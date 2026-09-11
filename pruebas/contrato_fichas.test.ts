// contrato_fichas.test.ts — La función de Supabase guarda EXACTAMENTE lo mismo
// que el Python del motor.
//
// De día escribe `compra-agil-reciente` (TypeScript) y de noche el barrido
// (Python), en la misma tabla. Esta prueba pasa las mismas fichas por los dos y
// exige el mismo resultado, fila a fila y carácter a carácter. Si alguien cambia
// un lado sin el otro, falla.
//
// Uso (lo que hace el workflow `contrato-compra-agil.yml`):
//   python pruebas/filas_python.py > esperado.json
//   ESPERADO=esperado.json node --test pruebas/contrato_fichas.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { filasDeFicha } from "../supabase/functions/compra-agil-reciente/ficha.ts";
import * as buscador from "../supabase/functions/compra-agil-reciente/buscador.ts";

const aqui = dirname(fileURLToPath(import.meta.url));
const rutaEsperado = process.env.ESPERADO;
if (!rutaEsperado) {
  throw new Error("Falta ESPERADO: la ruta del JSON que escribe pruebas/filas_python.py");
}
const esperado = JSON.parse(readFileSync(rutaEsperado, "utf-8"));
const carpeta = join(aqui, "fichas_compra_agil");
const archivos = readdirSync(carpeta).filter((n) => n.endsWith(".json")).sort();

test("hay fichas de ejemplo reales y hechas a mano", () => {
  assert.ok(archivos.some((n) => n.startsWith("sintetica_")), "faltan las fichas raras");
  assert.ok(archivos.some((n) => !n.startsWith("sintetica_")), "faltan fichas reales");
});

test("la función le habla al buscador igual que el Python", () => {
  const py = esperado["__constantes__"];
  assert.equal(buscador.URL_BUSCADOR, py.URL_BUSCADOR);
  assert.deepEqual(buscador.HEADERS_BUSCADOR, py.HEADERS_BUSCADOR);
  assert.equal(buscador.CLAVE_HEADER_API, py.CLAVE_HEADER_API);
  assert.equal(buscador.ESTADO_PUBLICADA, py.ESTADO_PUBLICADA);
  assert.equal(buscador.PAGINA_LISTADO, py.PAGINA_LISTADO);
  assert.equal(buscador.HILOS, py.HILOS);
});

for (const nombre of archivos) {
  test(`mismas filas que el Python: ${nombre}`, () => {
    const ficha = JSON.parse(readFileSync(join(carpeta, nombre), "utf-8")).payload;
    assert.ok(nombre in esperado, `el Python no dio resultado para ${nombre}`);
    assert.deepStrictEqual(filasDeFicha(ficha), esperado[nombre]);
  });
}
