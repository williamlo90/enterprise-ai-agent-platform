import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
process.env.RAYON_NUM_THREADS = '2';
const { rolldown } = await import('../../../frontend/node_modules/rolldown/dist/index.mjs');
const out = fileURLToPath(new URL('../../../local/g13/browser-assets/', import.meta.url));
await mkdir(out, { recursive:true });
const bundle = await rolldown({ input:fileURLToPath(new URL('../../../frontend/tests/integration/g13-browser-entry.ts',import.meta.url)) });
try { await bundle.write({ file:`${out}/browser-client.js`, format:'esm' }); }
finally { await bundle.close(); }
console.log('Controlled browser client bundle ready.');
