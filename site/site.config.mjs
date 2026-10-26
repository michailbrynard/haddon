// What the site is made of: which repository files become which pages, in which sidebar section.
// The prose itself lives in docs/ and README.md; scripts/sync-docs.mjs reads this list to build the pages.

// The public URL the site is served from, with no trailing slash. Unset until a domain is chosen; the build
// then uses relative links in llms.txt and a localhost URL for the sitemap. See site/README.md.
export const siteUrl = process.env.DOCS_SITE_URL || '';

// Where "edit this page" and links to repository files (examples, CONTRIBUTING.md) point.
export const repoUrl = (process.env.DOCS_REPO_URL || 'https://github.com/michailbrynard/haddon').replace(/\/$/, '');

// A short block of context for a coding or LLM agent, served as raw markdown at `path` and copied by the
// homepage's "Copy for your agent" button. Its root-relative links become absolute when `siteUrl` is set.
export const agentContext = { source: 'docs/agent-context.md', path: '/agent-context.md' };

export const sections = [
  { id: 'start', label: 'Start here' },
  { id: 'agents', label: 'Agents' },
  {
    id: 'api',
    label: 'API',
    extra: [
      ['OpenAPI document', '/openapi.json', 'the full schema of every route, request and response, as the service publishes it'],
    ],
  },
  { id: 'running', label: 'Running Haddon' },
  { id: 'contributing', label: 'Changing Haddon' },
];

// `slug` is the page's path under /docs/. `title` overrides the file's own H1 where the site needs a different
// one (the README's H1 is the project name), and `label` is a shorter name for the sidebar.
export const pages = [
  {
    source: 'docs/README.md',
    slug: 'index',
    section: 'start',
    title: 'Documentation',
    description: 'Every Haddon document, and when to read each one.',
  },
  { source: 'README.md', slug: 'overview', section: 'start', title: 'Overview' },
  { source: 'docs/quickstart.md', slug: 'quickstart', section: 'start' },
  { source: 'docs/concepts.md', slug: 'concepts', section: 'start', label: 'Concepts' },
  {
    source: 'docs/comparison.md',
    slug: 'comparison',
    section: 'start',
    label: 'Comparison',
    description: 'How Haddon compares with hledger, Beancount and Fava, Ledger and GnuCash, and when to pick each.',
  },
  { source: 'docs/using-from-an-agent.md', slug: 'using-from-an-agent', section: 'agents' },
  { source: 'docs/agent-guide.md', slug: 'agent-guide', section: 'agents' },
  {
    source: 'docs/api.md',
    slug: 'api',
    section: 'api',
    title: 'API conventions',
    description: 'Authentication, idempotency, amounts, dates, pagination and errors, and a list of every route.',
  },
  { source: 'docs/reviewer-ui.md', slug: 'reviewer-ui', section: 'running', label: 'Web interface' },
  { source: 'docs/hledger.md', slug: 'hledger', section: 'running', label: 'hledger' },
  { source: 'docs/operations.md', slug: 'operations', section: 'running', label: 'Operations' },
  { source: 'docs/architecture.md', slug: 'architecture', section: 'contributing' },
];
