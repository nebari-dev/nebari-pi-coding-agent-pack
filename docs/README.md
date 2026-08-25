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

## Deploy conventions

`astro.config.mjs` reads two environment variables so the same build works
locally, on preview deployments, and on the packs portal:

- `SITE` — canonical origin (defaults to `https://packs.nebari.dev`)
- `BASE` — base path (defaults to `/`; the portal serves this pack at `/pi-coding-agent-pack/`)

A CI publish workflow is not set up yet.
