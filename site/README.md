# The documentation site

The homepage and documentation site, built with [Astro Starlight](https://starlight.astro.build). This
directory holds the site's configuration and the homepage, and none of the documentation itself: the pages are
made at build time from `docs/`, `README.md`, `examples/haddon_tools.py` and the OpenAPI document the code
produces. Edit those, not anything generated here.

## Building it

You need Node 22 (the same as `web/`) and the Python environment from `make install`, which the build uses to
produce the OpenAPI document.

```bash
make site        # build into site/dist and check every link
make site-dev    # serve on http://localhost:4321 while you work on site/
```

`make site-dev` reads `docs/` when it starts, so restart it after editing a document. To look at a finished
build, run `cd site && npx astro preview` after `make site`.

## How it is put together

`scripts/sync-docs.mjs` runs before Astro and writes everything the build needs. None of it is committed (see
`.gitignore`).

| Output | Made from |
|---|---|
| `src/content/docs/docs/*.md`, the documentation pages | the files listed in `site.config.mjs`, with the H1 moved into the page title and relative links rewritten for the site |
| `public/docs/*.md` and `public/index.md`, each page as raw markdown at its own URL plus `.md` | the same files, with links pointing at the other `.md` copies |
| `public/llms.txt` and `public/llms-full.txt` | the page list and the full text of every page, in [llms.txt](https://llmstxt.org) form |
| `public/agent-context.md` and `src/generated/agent-context.md`, the block the homepage's "Copy for your agent" button copies | `docs/agent-context.md`, with its links made absolute when `DOCS_SITE_URL` is set |
| `public/openapi.json` | `create_app().openapi()`, the document the service serves at `/openapi.json` |
| `src/generated/openapi.render.json`, which the API reference pages are rendered from | the same document, with each request body's own `$defs` moved into `components.schemas` and read operations given short ids |
| `src/generated/*.md`, `call.py` | the homepage's sections, cut from the README and the example |

A relative link in `docs/` to a page on the site becomes a link to that page. A link to any other file in the
repository becomes a link to it on GitHub. A link to a file that does not exist stops the build. To add a page,
add the file to `docs/` and a line for it to `site.config.mjs`.

Three of Starlight's components are replaced, in `src/components/`. `Head.astro` adds a link to each page's
markdown copy. `PageTitle.astro` puts a row of actions under each page's title: copy the page's markdown, copy
the address of that markdown, open it, or copy a short prompt pointing an agent at it and at `llms.txt`.
`Footer.astro` lists the files meant for agents. The copy buttons need JavaScript and are hidden without it;
the links beside them work either way.

The build fails on a broken link. `starlight-links-validator` checks the links and anchors in the documentation
pages, and `scripts/check-dist.mjs` then checks that every same-site link in the built HTML, markdown and
`llms.txt` files resolves to a file in `dist/`. CI runs `make site` on every push and pull request.

## Putting it online

The site is served at `https://haddon.dev` by a Cloudflare Worker that holds nothing but the built files.
`wrangler.jsonc` describes it: the Worker `haddon`, `dist/` as its static assets, and `haddon.dev` as its
custom domain. `public/_headers` makes Cloudflare serve the `.md` copies as `text/markdown`.

To connect it, in the Cloudflare dashboard open Workers & Pages, then Create, then Import a repository, and pick
this repository with these settings:

| Setting | Value |
|---|---|
| Project name | `haddon` (it must match `name` in `wrangler.jsonc`) |
| Root directory | `site` |
| Build command | `npm run build:cloudflare` |
| Deploy command | `npx wrangler deploy` |

Every push to `main` then builds and deploys. `scripts/cloudflare-build.sh` installs the Python package, which
the API reference is generated from, and builds with `DOCS_SITE_URL` set to `https://haddon.dev`. If the build
image's `python3` is older than 3.12, add a build variable `PYTHON_VERSION` set to `3.12`. To serve the site
somewhere else, change the route in `wrangler.jsonc` and set `DOCS_SITE_URL` as a build variable.

`DOCS_SITE_URL` is used for the sitemap, canonical links, `robots.txt` and the absolute links in `llms.txt`.
When it is unset, as in local builds, those use `http://localhost:4321` or relative links.
