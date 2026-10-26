// The raw markdown copy of a page, as scripts/sync-docs.mjs writes it, or null for a page without one (the API
// reference, which is generated from openapi.json).
export function markdownPath(id: string): string | null {
  if (id === '' || id === 'index') return '/index.md';
  if (id === 'docs') return '/docs/index.md';
  if (id.startsWith('docs/')) return `/${id}.md`;
  return null;
}
