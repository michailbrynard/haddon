// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';
import starlightOpenAPI, { openAPISidebarGroups } from 'starlight-openapi';

import { pages, repoUrl, sections, siteUrl } from './site.config.mjs';

const slug = (page) => (page.slug === 'index' ? 'docs' : `docs/${page.slug}`);

export default defineConfig({
  // DOCS_SITE_URL is the public address once a domain is chosen (site/README.md). The sitemap and canonical
  // links need an absolute URL, so a local build uses the preview server's.
  site: siteUrl || 'http://localhost:4321',
  trailingSlash: 'always',
  integrations: [
    starlight({
      title: 'Haddon',
      description: 'An open-source double-entry ledger for AI agents.',
      social: [{ icon: 'github', label: 'Source code', href: repoUrl }],
      editLink: { baseUrl: `${repoUrl}/edit/main/` },
      lastUpdated: false,
      pagination: true,
      customCss: ['./src/styles/site.css'],
      head: [
        { tag: 'link', attrs: { rel: 'alternate', type: 'text/plain', title: 'llms.txt', href: '/llms.txt' } },
      ],
      components: {
        Head: './src/components/Head.astro',
        PageTitle: './src/components/PageTitle.astro',
        Footer: './src/components/Footer.astro',
      },
      plugins: [
        starlightOpenAPI([
          {
            base: 'api',
            schema: './src/generated/openapi.render.json',
            sidebar: { label: 'Reference', collapsed: true, operations: { badges: true, labels: 'operationId' } },
          },
        ]),
        starlightLinksValidator({
          // Static files in public/ and the generated API pages are not in the content collection, so the
          // validator cannot see them. scripts/check-dist.mjs checks those links in the built output instead.
          exclude: ['/agent-context.md', '/openapi.json', '/llms.txt', '/llms-full.txt', '/index.md', '/docs/*.md', '/api/', '/api/**'],
        }),
      ],
      sidebar: sections.map((section) => ({
        label: section.label,
        items: [
          ...pages.filter((p) => p.section === section.id && p.slug !== 'index').map(slug),
          ...(section.id === 'api' ? openAPISidebarGroups : []),
        ],
      })),
    }),
  ],
});
