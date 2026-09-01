import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const manifest = JSON.parse(readFileSync('tests/TEST_MANIFEST.json', 'utf8'));
const sha256 = (path) => createHash('sha256').update(readFileSync(path)).digest('hex');
const failures = [];
const blockRe = /^\s*(?:test|it)\s*\(/gm;
const assertionRe = /\bassert(?:\.[A-Za-z_$][\w$]*)?\s*\(/g;

const actualTests = readdirSync('tests', { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith('.test.ts'))
  .map((entry) => join('tests', entry.name))
  .sort();
const expectedTests = manifest.tests.map((item) => item.path).sort();
if (JSON.stringify(actualTests) !== JSON.stringify(expectedTests)) failures.push('test path set mismatch');

let blocks = 0;
let assertions = 0;
for (const item of manifest.tests) {
  let text;
  try { text = readFileSync(item.path, 'utf8'); } catch { failures.push(`missing test: ${item.path}`); continue; }
  const actual = { sha256: sha256(item.path), blocks: [...text.matchAll(blockRe)].length, assertions: [...text.matchAll(assertionRe)].length };
  for (const key of ['sha256', 'blocks', 'assertions']) if (actual[key] !== item[key]) failures.push(`${item.path} ${key}: ${actual[key]} != ${item[key]}`);
  blocks += actual.blocks;
  assertions += actual.assertions;
}
if (blocks !== manifest.totals.blocks) failures.push(`total blocks: ${blocks} != ${manifest.totals.blocks}`);
if (assertions !== manifest.totals.assertions) failures.push(`total assertions: ${assertions} != ${manifest.totals.assertions}`);

const expectedFixtures = new Map(readFileSync('tests/fixtures/SHA256SUMS', 'utf8').trim().split('\n').map((line) => {
  const [hash, path] = line.split(/\s{2}/);
  return [path, hash];
}));
const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? walk(join(dir, entry.name)) : [join(dir, entry.name)]);
const actualFixtures = walk('tests/fixtures').filter((path) => path !== 'tests/fixtures/SHA256SUMS').sort();
if (JSON.stringify(actualFixtures) !== JSON.stringify([...expectedFixtures.keys()].sort())) failures.push('fixture path set mismatch');
for (const [path, hash] of expectedFixtures) {
  try { if (sha256(path) !== hash) failures.push(`${path} sha256 mismatch`); } catch { failures.push(`missing fixture: ${path}`); }
}

if (failures.length) {
  for (const failure of failures) console.error(`CONTRACT_FAIL ${failure}`);
  process.exit(1);
}
console.log(`CONTRACT_PASS tests=${manifest.tests.length} blocks=${blocks} assertions=${assertions} fixtures=${expectedFixtures.size}`);
