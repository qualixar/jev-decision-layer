import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
const root = fileURLToPath(new URL('../', import.meta.url));
const catalog = JSON.parse(readFileSync(resolve(root, 'plugins/qualixar-jev-decision-layer/runtime/recipe_catalog.json'), 'utf8'));
const recipes = catalog.recipes.slice(0, 20);
console.log('Offline synthetic replay: recorded answers through real gates. No provider call, accuracy or savings claim.');
let failed = false;
for (const recipe of recipes) {
  for (const variant of ['nominal', 'uncertain', 'adversarial']) {
    const result = JSON.parse(execFileSync(resolve(root, 'plugins/qualixar-jev-decision-layer/scripts/jev'), ['selftest', '--recipe', recipe.id, '--variant', variant], { cwd: root, encoding: 'utf8', timeout: 15000 }));
    failed ||= result.passed !== true;
    console.log(JSON.stringify({ recipe: recipe.id, title: recipe.title, variant, passed: result.passed, gate: result.raw_gate, live_policy: result.observed }));
  }
}
if (recipes.length !== 20) throw new Error('Expected twenty public recipe examples');
console.log(failed ? 'FAIL' : 'PASS: 20 recipe examples, 60 synthetic gate checks');
process.exitCode = failed ? 1 : 0;
