import { defineCollection } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

// src/content/docs/index.mdx is the homepage and is written here. Everything under src/content/docs/docs/ is
// generated from the repository's docs/ by scripts/sync-docs.mjs.
export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};
