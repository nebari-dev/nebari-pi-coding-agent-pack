import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { nebari } from '@nebari/starlight';
import remarkBaseLinks from './src/plugins/remark-base-links';

// Deploy conventions (PACK_SLUG: pi-coding-agent-pack):
//
//   main    SITE=https://packs.nebari.dev                       BASE=/pi-coding-agent-pack/
//   preview SITE=https://<branch>.pi-coding-agent-pack.pages.dev  BASE=/
//
// The `site` default mirrors the production origin so a plain `npm run build`
// still emits correct canonical URLs and a sitemap. `base` stays `/` by default
// so the dev server and local previews serve from the root.
const SITE = process.env.SITE || 'https://packs.nebari.dev';
const BASE = process.env.BASE || '/';

export default defineConfig({
  base: BASE,
  site: SITE,
  integrations: [
    starlight({
      title: 'Pi Coding Agent Pack',
      description:
        'Extends Nebari’s JupyterHub stack with a Pi coding-agent workflow: launcher service, named-server profiles, and a browser terminal.',
      // Shared Nebari identity (brand colors, fonts, logo, favicon, footer, and
      // GitHub social link) comes from the @nebari/starlight theme plugin. On the
      // portal the header logo returns users to the pack catalog.
      plugins: [nebari({ logoHref: 'https://packs.nebari.dev/' })],
      editLink: {
        // Starlight appends the source path (src/content/docs/<file>.md) to this
        // base, so it must point at the Astro project root inside the repo.
        baseUrl: 'https://github.com/nebari-dev/pi-coding-agent-pack/edit/main/docs/',
      },
      sidebar: [
        {
          label: 'Getting Started',
          items: [{ label: 'Introduction', link: '/' }],
        },
      ],
    }),
  ],
  markdown: {
    remarkPlugins: [[remarkBaseLinks, { base: BASE }]],
  },
});
