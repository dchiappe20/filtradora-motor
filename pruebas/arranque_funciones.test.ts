// arranque_funciones.test.ts — Cada función de Supabase se puede CARGAR entera.
//
// Supabase enlaza todos los imports de `index.ts` al arrancar la función. Si uno
// no existe, la función no llega a ejecutar nada: contesta 503 BOOT_ERROR, no
// deja constancia en ninguna tabla y desde fuera sólo se ve que «no trae nada».
// Así estuvo `licitaciones-reciente` desde que se desplegó (2026-09-11) hasta el
// 2026-09-16: importaba `esDict` de `./ficha.ts`, que no lo exporta. Las pruebas
// de contrato no lo vieron porque sólo cargan `ficha.ts`.
//
// Aquí se simula lo mínimo de Deno (`env` y `serve`) y se importa cada
// `index.ts`. No se llama a nada: basta con que el módulo enlace.
//
// Uso:  node --test pruebas/arranque_funciones.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const raiz = join(dirname(fileURLToPath(import.meta.url)), "..", "supabase", "functions");

// Lo único de Deno que las funciones tocan al cargarse.
(globalThis as any).Deno = { env: { get: () => "" }, serve: () => {} };

const funciones = readdirSync(raiz, { withFileTypes: true })
  .filter((d) => d.isDirectory() && !d.name.startsWith("_") && existsSync(join(raiz, d.name, "index.ts")))
  .map((d) => d.name)
  .sort();

test("hay funciones que probar", () => {
  assert.ok(funciones.includes("compra-agil-reciente"));
  assert.ok(funciones.includes("licitaciones-reciente"));
});

for (const nombre of funciones) {
  test(`${nombre} arranca`, async () => {
    await import(pathToFileURL(join(raiz, nombre, "index.ts")).href);
  });
}
