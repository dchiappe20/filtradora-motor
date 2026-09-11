// contrato_licitaciones.test.ts — La función `licitaciones-reciente` guarda
// EXACTAMENTE lo mismo que el Python del motor.
//
// De día escribe la función (cada minuto) y de noche el barrido, en la misma
// tabla. Esta prueba pasa las mismas respuestas de la API por los dos lados y
// exige el mismo estado y las mismas filas, carácter a carácter.
//
// Uso (lo que hace el workflow `contrato-compra-agil.yml`):
//   python pruebas/filas_python_licitaciones.py > esperado_licitaciones.json
//   ESPERADO_LICITACIONES=esperado_licitaciones.json node --test pruebas/contrato_licitaciones.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { filasDeRespuesta, HILOS, URL_API } from "../supabase/functions/licitaciones-reciente/ficha.ts";

const aqui = dirname(fileURLToPath(import.meta.url));
const rutaEsperado = process.env.ESPERADO_LICITACIONES;
if (!rutaEsperado) {
  throw new Error("Falta ESPERADO_LICITACIONES: el JSON que escribe pruebas/filas_python_licitaciones.py");
}
const esperado = JSON.parse(readFileSync(rutaEsperado, "utf-8"));
const carpeta = join(aqui, "fichas_licitaciones");
const archivos = readdirSync(carpeta).filter((n) => n.endsWith(".json")).sort();

// Igual que `cargar` en el lado Python.
function cargar(ruta: string): { codigo: string; fecha: string; respuesta: any } {
  const obj = JSON.parse(readFileSync(ruta, "utf-8"));
  if ("respuesta" in obj) return { codigo: obj.codigo, fecha: obj.fecha, respuesta: obj.respuesta };
  const codigo = basename(ruta).replace(/\.json$/, "");
  const fecha = String(obj.Listado[0].Fechas.FechaPublicacion).slice(0, 10);
  return { codigo, fecha, respuesta: obj };
}

test("hay respuestas de ejemplo reales y hechas a mano", () => {
  assert.ok(archivos.some((n) => n.startsWith("sintetica_")), "faltan las raras");
  assert.ok(archivos.some((n) => !n.startsWith("sintetica_")), "faltan reales");
});

test("la función usa la misma API y los mismos hilos que el Python", () => {
  assert.equal(URL_API, esperado["__constantes__"].URL_API);
  assert.equal(HILOS, esperado["__constantes__"].HILOS);
});

for (const nombre of archivos) {
  test(`mismo resultado que el Python: ${nombre}`, () => {
    const { codigo, fecha, respuesta } = cargar(join(carpeta, nombre));
    const py = esperado[nombre];
    assert.ok(py, `el Python no dio resultado para ${nombre}`);
    if (py.estado === "EXCEPCION") {
      assert.throws(() => filasDeRespuesta(respuesta, codigo, fecha));
      return;
    }
    assert.deepStrictEqual(filasDeRespuesta(respuesta, codigo, fecha), { estado: py.estado, filas: py.filas });
  });
}
