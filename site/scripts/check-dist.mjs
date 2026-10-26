// After a build: every same-site link in dist/ must resolve to a file in dist/, in the HTML pages and in the
// markdown and llms.txt files. starlight-links-validator covers the docs pages' links and anchors as they are
// written; this covers what it cannot see, such as the generated API pages and the raw markdown copies.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../dist');
if (!existsSync(dist)) throw new Error('no dist/: run the build first');

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else yield path;
  }
}

function resolves(href) {
  const path = decodeURI(href.split('#')[0].split('?')[0]);
  if (!path) return true;
  const target = join(dist, path);
  return existsSync(target) && (statSync(target).isFile() || existsSync(join(target, 'index.html')));
}

const broken = [];
let checked = 0;
for (const file of files(dist)) {
  let links = [];
  const text = /\.(html|md|txt)$/.test(file) ? readFileSync(file, 'utf8') : '';
  if (file.endsWith('.html')) links = [...text.matchAll(/\s(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
  else if (file.endsWith('.md') || file.endsWith('.txt')) links = [...text.matchAll(/\]\((\/[^)\s]*)\)/g)].map((m) => m[1]);
  for (const href of links) {
    if (!href.startsWith('/') || href.startsWith('//')) continue;
    checked += 1;
    if (!resolves(href)) broken.push(`${relative(dist, file)}: ${href}`);
  }
}
if (broken.length) {
  console.error(`check-dist: ${broken.length} broken links\n  ${[...new Set(broken)].join('\n  ')}`);
  process.exit(1);
}
console.log(`check-dist: ${checked} same-site links in dist/ all resolve`);
