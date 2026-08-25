# Pi Coding Agent Pack Documentation

This directory contains the [Astro](https://astro.build) + [Starlight](https://starlight.astro.build) site for the Pi Coding Agent Pack.

> **Experimental** — this site is a scaffold. Content is coming soon; the
> landing page is a placeholder.

## Prerequisites

- Node.js `>= 22` (enforced by the `engines` field in `package.json`)
- npm (bundled with Node.js)

## Install

```bash
cd docs
npm ci
```

## Local development

```bash
npm run dev
```

Starts the Astro dev server with hot reload on http://localhost:4321/.

## Production build

```bash
npm run build
```

Emits static files to `docs/dist/`.

## Preview the production build

```bash
npm run preview
```

Serves the contents of `docs/dist/` locally so you can verify the production output.

## Content

Pages live in `src/content/docs/`. Each `.md` or `.mdx` file becomes a page. The sidebar is configured in `astro.config.mjs` under `starlight.sidebar`.

## Link checking

```bash
bash ../scripts/check-links.sh
```

To test with the production base path: `BASE=/pi-coding-agent-pack/ bash ../scripts/check-links.sh`

## Deploy conventions

`astro.config.mjs` reads two environment variables so the same build works
locally, on preview deployments, and on the packs portal:

- `SITE` — canonical origin (defaults to `https://packs.nebari.dev`)
- `BASE` — base path (defaults to `/`; the portal serves this pack at `/pi-coding-agent-pack/`)

## CI

The [`Docs` workflow](../.github/workflows/docs.yml) builds the site, checks internal links, and deploys to [Cloudflare Pages](https://pages.cloudflare.com) on every push to `main` and every pull request that touches `docs/`. Pull requests get a preview URL posted as a comment; the [`Docs preview cleanup`](../.github/workflows/docs-preview-cleanup.yml) workflow removes it when the PR closes. Deploys require the `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` repository secrets.
