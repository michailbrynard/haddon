// Builds the site's inputs from the repository: docs/*.md and README.md become Starlight pages, raw markdown
// copies and llms.txt, and the running code's OpenAPI document becomes public/openapi.json.
//
// Nothing written here is committed (see site/.gitignore). docs/ and the README stay the only copy of the
// prose, readable on GitHub with their relative links; the links are rewritten here for the site.
//
// A relative link to a file that does not exist fails the build, as does a page listed below without a source.

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, posix, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { agentContext, pages, repoUrl, sections, siteUrl } from '../site.config.mjs';

const siteDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = resolve(siteDir, '..');
const contentDir = join(siteDir, 'src/content/docs/docs');
const generatedDir = join(siteDir, 'src/generated');
const publicDir = join(siteDir, 'public');

const bySource = new Map(pages.map((p) => [p.source, p]));

// An absolute URL on the site when its address is known, else the root-relative path.
const abs = (path) => (siteUrl ? new URL(path, siteUrl).href : path);

// The URL of a page, as HTML (`/docs/quickstart/`) or as its raw markdown (`/docs/quickstart.md`).
function pageUrl(page, form) {
  if (form === 'md') return page.slug === 'index' ? '/docs/index.md' : `/docs/${page.slug}.md`;
  return page.slug === 'index' ? '/docs/' : `/docs/${page.slug}/`;
}

function pageFor(target, fromSource) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('#') || target.startsWith('/')) return undefined;
  const resolved = posix.normalize(posix.join(posix.dirname(fromSource), target.split('#')[0])).replace(/\/$/, '');
  return bySource.get(resolved) ?? (resolved === agentContext.source ? { source: resolved } : undefined);
}

const titleOf = (page) => page.title ?? splitTitle(readFileSync(join(repoDir, page.source), 'utf8'), page.source).h1;

// Rewrite one link target found in `fromSource` (a repository-relative path).
function rewriteTarget(target, fromSource, form) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('#') || target.startsWith('/')) return target;
  const [path, hash] = target.split('#', 2);
  const resolved = posix.normalize(posix.join(posix.dirname(fromSource), path)).replace(/\/$/, '');
  const suffix = hash === undefined ? '' : `#${hash}`;
  const page = bySource.get(resolved);
  if (page) return pageUrl(page, form) + suffix;
  if (resolved === agentContext.source) return agentContext.path + suffix;
  const onDisk = join(repoDir, resolved);
  if (!existsSync(onDisk)) throw new Error(`${fromSource}: link to ${target} points at nothing (${resolved})`);
  const kind = statSync(onDisk).isDirectory() ? 'tree' : 'blob';
  return `${repoUrl}/${kind}/main/${resolved}${suffix}`;
}

// Rewrite inline links outside code. Fenced blocks are left exactly as written. A link whose text is just the
// file name it points at ("[quickstart.md](quickstart.md)") reads well on GitHub but not on the site, so on the
// site it takes the page's title instead.
function rewriteLinks(markdown, fromSource, form) {
  let fenced = false;
  const link = /\[((?:`[^`]*`|[^\]`])+)\]\(([^)\s]+)\)/g;
  return markdown
    .split('\n')
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        fenced = !fenced;
        return line;
      }
      if (fenced) return line;
      return line.replace(link, (_, text, target) => {
        const page = pageFor(target, fromSource);
        const bare = text.replace(/^`|`$/g, '');
        const label = page && (bare === target.split('#')[0] || bare === page.source) ? titleOf(page) : text;
        return `[${label}](${rewriteTarget(target, fromSource, form)})`;
      });
    })
    .join('\n');
}

// Split a markdown file into its H1 and the rest.
function splitTitle(markdown, source) {
  const match = markdown.match(/^# (.+)\n+/);
  if (!match) throw new Error(`${source}: expected the file to start with a "# Title" line`);
  return { h1: match[1].trim(), body: markdown.slice(match[0].length) };
}

// A paragraph of markdown as plain text on one line.
function plain(para) {
  return para
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*?([^*]+)\*\*?/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}

// The first paragraph as plain text, cut at a sentence end, for <meta name="description"> and llms.txt.
function describe(body) {
  const text = plain(body.split(/\n\s*\n/).find((p) => p.trim() && !/^\s*(#|\||```|-|\d+\.|>)/.test(p)) ?? '');
  if (text.length <= 200) return text;
  const cut = text.slice(0, 200);
  const end = cut.lastIndexOf('. ');
  return end > 60 ? cut.slice(0, end + 1) : `${cut.slice(0, cut.lastIndexOf(' '))}...`;
}

function yamlString(s) {
  return JSON.stringify(s);
}

function write(path, text) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
}

// The README, cut into the parts the homepage shows. Headings are the README's own.
function readmeSection(markdown, heading) {
  const start = markdown.indexOf(`\n## ${heading}\n`);
  if (start < 0) throw new Error(`README.md: no "## ${heading}" section for the homepage`);
  const rest = markdown.slice(start + heading.length + 5);
  const end = rest.search(/\n## /);
  return (end < 0 ? rest : rest.slice(0, end)).trim() + '\n';
}

function dumpOpenAPI() {
  const python = process.env.HADDON_PYTHON ?? join(repoDir, '.venv/bin/python');
  const out = join(publicDir, 'openapi.json');
  // The schema the service publishes at /openapi.json, from the same create_app() call, with every extension
  // mounted (the default). Failing here fails the build: a stale or missing reference is worse than none.
  const env = { ...process.env };
  delete env.HADDON_RECONCILIATION;
  const json = execFileSync(
    python,
    ['-c', 'import json, sys; from haddon.api.app import create_app; json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=False)'],
    { cwd: repoDir, env, maxBuffer: 64 * 1024 * 1024 },
  );
  write(out, json.toString() + '\n');
  return JSON.parse(json.toString());
}

// The copy the API reference pages are rendered from. Each write's request body in the published document is
// self-contained: it carries its own `$defs` and refers to them as `#/$defs/...`, which is what lets a tool
// definition be cut straight out of it (examples/haddon_tools.py). A document-level renderer resolves those
// references against the document root instead, so this copy moves every `$defs` entry into
// `components.schemas` and points the references there. Two different schemas under one name fail the build.
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object')
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  return JSON.stringify(value);
}

function forRendering(spec) {
  const copy = structuredClone(spec);
  const schemas = (copy.components ??= {}).schemas ??= {};
  const hoist = (node, isRoot) => {
    if (Array.isArray(node)) return node.forEach((n) => hoist(n, false));
    if (!node || typeof node !== 'object') return;
    if (!isRoot && node.$defs) {
      for (const [name, def] of Object.entries(node.$defs)) {
        if (schemas[name] && canonical(schemas[name]) !== canonical(def))
          throw new Error(`openapi.json: two different schemas are both called ${name}`);
        schemas[name] = def;
      }
      delete node.$defs;
    }
    for (const value of Object.values(node)) hoist(value, false);
  };
  hoist(copy, true);
  // Write commands have their command name as operationId. Reads have FastAPI's generated one, such as
  // `list_entries_v1_entities__entity_id__entries_get`; the reference pages and sidebar use the function name
  // at the front of it (`list_entries`) instead.
  const seen = new Set();
  for (const item of Object.values(copy.paths))
    for (const op of Object.values(item)) {
      op.operationId = op.operationId.replace(/_(v1|healthz|readyz)(_.*)?_(get|post)$/, '');
      if (seen.has(op.operationId)) throw new Error(`openapi.json: two operations shorten to ${op.operationId}`);
      seen.add(op.operationId);
    }
  return JSON.parse(JSON.stringify(copy).replaceAll('"#/$defs/', '"#/components/schemas/'));
}

function main() {
  for (const dir of [contentDir, generatedDir, join(publicDir, 'docs')]) rmSync(dir, { recursive: true, force: true });
  for (const f of [agentContext.path.slice(1), 'index.md', 'llms.txt', 'llms-full.txt', 'openapi.json', 'robots.txt']) rmSync(join(publicDir, f), { force: true });

  const rendered = [];
  for (const page of pages) {
    const sourcePath = join(repoDir, page.source);
    if (!existsSync(sourcePath)) throw new Error(`site.config.mjs lists ${page.source}, which does not exist`);
    const raw = readFileSync(sourcePath, 'utf8');
    const { h1, body } = splitTitle(raw, page.source);
    const title = page.title ?? h1;
    const description = page.description ?? describe(body);
    const htmlBody = rewriteLinks(body, page.source, 'html');
    const mdBody = rewriteLinks(body, page.source, 'md');

    const front = [
      '---',
      `title: ${yamlString(title)}`,
      `description: ${yamlString(description)}`,
      `editUrl: ${yamlString(`${repoUrl}/edit/main/${page.source}`)}`,
      ...(page.label ? [`sidebar:`, `  label: ${yamlString(page.label)}`] : []),
      '---',
      `<!-- Generated from ${page.source} by site/scripts/sync-docs.mjs. Edit that file, not this one. -->`,
      '',
    ].join('\n');
    write(join(contentDir, `${page.slug}.md`), front + htmlBody);
    // The summary line is left out where it would only repeat the first paragraph.
    const summary = description === describe(body) ? '' : `> ${description}\n\n`;
    const markdown = `# ${title}\n\n${summary}${mdBody}`;
    write(join(publicDir, pageUrl(page, 'md')), markdown);
    rendered.push({ page, title, description, markdown });
  }

  // The homepage's quick start and agent example, from the README and the example file themselves.
  const readme = readFileSync(join(repoDir, 'README.md'), 'utf8');
  const { body: readmeBody } = splitTitle(readme, 'README.md');
  // The homepage hero already says the README's first sentence, so the intro starts after it.
  const intro = readmeBody.slice(0, readmeBody.indexOf('\n## ')).trim().replace(/^[^.]*\.\s+/, '') + '\n';
  write(join(generatedDir, 'intro.md'), rewriteLinks(intro, 'README.md', 'html'));
  write(join(generatedDir, 'try-it.md'), rewriteLinks(readmeSection(readme, 'Try it'), 'README.md', 'html'));
  write(join(generatedDir, 'agent.md'), rewriteLinks(readmeSection(readme, 'Giving it to an agent'), 'README.md', 'html'));
  const example = readFileSync(join(repoDir, 'examples/haddon_tools.py'), 'utf8');
  const callAt = example.indexOf('\ndef call(');
  if (callAt < 0) throw new Error('examples/haddon_tools.py: no call() function for the homepage excerpt');
  write(join(generatedDir, 'call.py'), example.slice(callAt + 1).trimEnd() + '\n');
  write(join(publicDir, 'index.md'), rendered.find((r) => r.page.source === 'README.md').markdown);

  // The block the homepage's "Copy for your agent" button copies, also served as markdown. It is pasted
  // somewhere other than this site, so its links are absolute whenever the site's address is known.
  const context = splitTitle(readFileSync(join(repoDir, agentContext.source), 'utf8'), agentContext.source);
  const contextBody = rewriteLinks(context.body.replace(/<!--[\s\S]*?-->\n+/g, ''), agentContext.source, 'md');
  const contextMarkdown = `# ${context.h1}\n\n${contextBody}`.replace(
    /\]\((\/[^)\s]*)\)/g,
    (_, path) => `](${abs(path)})`,
  );
  write(join(publicDir, agentContext.path), contextMarkdown);
  write(join(generatedDir, 'agent-context.md'), contextMarkdown);

  const spec = dumpOpenAPI();
  write(join(generatedDir, 'openapi.render.json'), JSON.stringify(forRendering(spec), null, 2) + '\n');

  // llms.txt (https://llmstxt.org): a title, a one-paragraph summary, then sections of links to markdown.
  // The summary is the README's first paragraph whole, which says what Haddon is.
  const summary = plain(readmeBody.split(/\n\s*\n/)[0]);
  const lines = ['# Haddon', '', `> ${summary}`, ''];
  lines.push(
    `An agent should read ${abs(agentContext.path)} first: what Haddon is, how to authenticate, what to ask the ` +
      'service before writing, and the rules for writes and refusals. Every page below is plain markdown. The ' +
      `same pages as HTML are at the path without \`.md\`. The full text of all of them in one file is at ${abs('/llms-full.txt')}.`,
    '',
  );
  for (const [i, section] of sections.entries()) {
    const items = rendered.filter((r) => r.page.section === section.id);
    if (!items.length && !section.extra) continue;
    lines.push(`## ${section.label}`, '');
    if (i === 0)
      lines.push(
        `- [${context.h1}](${abs(agentContext.path)}): a short block to paste into an agent's instructions: ` +
          'authentication, discovery, idempotency, corrections and locked periods.',
      );
    for (const r of items) lines.push(`- [${r.title}](${abs(pageUrl(r.page, 'md'))}): ${r.description}`);
    if (section.extra) for (const [label, href, text] of section.extra) lines.push(`- [${label}](${abs(href)}): ${text}`);
    lines.push('');
  }
  write(join(publicDir, 'llms.txt'), lines.join('\n'));
  write(join(publicDir, 'robots.txt'), `User-agent: *\nAllow: /\n${siteUrl ? `\nSitemap: ${abs('/sitemap-index.xml')}\n` : ''}`);

  const full = [`# Haddon\n\n> ${summary}\n`, `<!-- ${abs(agentContext.path)} -->\n\n${contextMarkdown.trimEnd()}\n`];
  for (const section of sections)
    for (const r of rendered.filter((x) => x.page.section === section.id))
      full.push(`<!-- ${abs(pageUrl(r.page, 'md'))} -->\n\n${r.markdown.trimEnd()}\n`);
  write(join(publicDir, 'llms-full.txt'), full.join('\n---\n\n'));

  const ops = Object.values(spec.paths).flatMap((item) => Object.values(item)).length;
  console.log(
    `sync-docs: ${rendered.length} pages from ${relative(repoDir, join(repoDir, 'docs'))}/ and README.md, ` +
      `openapi.json with ${Object.keys(spec.paths).length} paths and ${ops} operations`,
  );
}

main();
