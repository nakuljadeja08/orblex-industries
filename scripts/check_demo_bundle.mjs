// check_demo_bundle.mjs — fails `npm run build:demo` if anything private reached the public demo:
// any value in .env, the strings in PRIVATE_STRINGS, or any ticker the real system tracks.
//
// Nothing private is written in this file, so it can be public. It reads .env at build time: every
// value 8+ characters long (keys, the database address, account numbers) is banned, plus PRIVATE_STRINGS,
// a comma-separated list in .env of anything else that must never ship (emails, a private repo name).
import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const DIST = process.argv[2] || 'dist-demo';

function files(dir) {
  return readdirSync(dir).flatMap(f => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

export function privateStrings(envPath = '.env') {
  // The playground's own Supabase address is meant to ship; the trading one comes from .env below.
  const out = ['@gmail.com'];
  if (!existsSync(envPath)) return out;
  for (const line of readFileSync(envPath, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
    if (!m) continue;
    const value = m[2].trim().replace(/^["']|["']$/g, '');
    if (m[1] === 'PRIVATE_STRINGS') out.push(...value.split(',').map(s => s.trim()).filter(Boolean));
    else if (value.length >= 8 && !/^[a-z_.-]+$/.test(value) && !value.includes('localhost')) out.push(value);   // skip plain words like "yfinance"
  }
  return out;
}

const banned = privateStrings();
const problems = [];
const bundle = files(DIST).filter(f => /\.(js|html|css|map)$/.test(f));
for (const file of bundle) {
  const text = readFileSync(file, 'utf8');
  for (const b of banned) if (text.includes(b)) problems.push(`${file}: contains a banned string (${b.slice(0, 4)}…)`);
}

// Real tickers may appear nowhere in the sample data.
const fixtures = readFileSync('src/demo/fixtures.js', 'utf8');
const quoted = new Set([...fixtures.matchAll(/["'`]([A-Z]{1,5})["'`]/g)].map(m => m[1]));
const real = ['universe.txt', 'candidates.txt'].flatMap(f =>
  readFileSync(f, 'utf8').split(/\r?\n/).map(l => l.split('#')[0].trim().split(/\s+/)[0]).filter(Boolean));
for (const t of real) if (quoted.has(t)) problems.push(`src/demo/fixtures.js: real ticker ${t}`);

if (problems.length) {
  console.error('DEMO BUNDLE CHECK FAILED\n' + problems.map(p => '  ' + p).join('\n'));
  process.exit(1);
}
console.log(`demo bundle check passed: ${bundle.length} files, ${banned.length} banned strings, ${real.length} real tickers`);
