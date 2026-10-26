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

Nothing is deployed until it is switched on. The `deploy-site` job in `.github/workflows/ci.yml` publishes
`site/dist` to Cloudflare Pages on a push to `main`, and is skipped unless all of the following are set in the
repository's settings (Settings, then Secrets and variables, then Actions).

| Name | Kind | Value |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret | an API token with the Cloudflare Pages: Edit permission |
| `CLOUDFLARE_ACCOUNT_ID` | secret | the Cloudflare account id |
| `DOCS_SITE_URL` | variable | the public address, such as `https://docs.example.org`, with no trailing slash |
| `DOCS_SITE_ENABLED` | variable | `true` |
| `DOCS_PAGES_PROJECT` | variable, optional | the Pages project name; `haddon-docs` if unset |

Once a domain is chosen:

1. Create the Pages project once: `npx wrangler pages project create haddon-docs --production-branch main`
   (or create a project with direct upload in the Cloudflare dashboard).
2. Set the secrets and variables above, then push to `main` or re-run the workflow. The site is then live at
   `https://haddon-docs.pages.dev`.
3. In the Pages project, open Custom domains and add the domain. If its DNS is on Cloudflare the record is
   created for you; otherwise add a `CNAME` from the domain to `haddon-docs.pages.dev` at your DNS provider.

`DOCS_SITE_URL` is used for the sitemap, canonical links, `robots.txt` and the absolute links in `llms.txt`.
When it is unset, as in pull requests and local builds, those use `http://localhost:4321` or relative links.
`public/_headers` makes Cloudflare serve the `.md` copies as `text/markdown`.

To turn deployment off again, set `DOCS_SITE_ENABLED` to anything other than `true`.
