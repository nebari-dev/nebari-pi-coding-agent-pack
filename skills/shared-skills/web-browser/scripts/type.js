#!/usr/bin/env node

import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId, waitForCondition } from "./page-target.js";

const { parsed, rest } = parseCommonPageArgs(process.argv.slice(2));
const selector = rest[0] || "";
const text = rest[1] || "";
const append = rest.includes("--append");

if (!selector || !text) {
  console.log("Usage: type.js <selector> <text> [--append] [--url-contains <frag>] [--target-id <id>] [--wait-new-tab] [--timeout-ms <ms>]");
  process.exit(1);
}

const globalTimeout = setTimeout(() => {
  console.error("✗ Global timeout exceeded");
  process.exit(1);
}, parsed.timeoutMs + 5000);

try {
  const cdp = await connect(5000);
  const targetId = await resolveTargetId(cdp, parsed);
  const sessionId = await cdp.attachToPage(targetId);

  const waitOk = await waitForCondition(
    cdp,
    sessionId,
    `(function(){ return document.querySelector(${JSON.stringify(selector)}) != null; })()`,
    parsed.timeoutMs,
    250,
  );
  if (!waitOk) throw new Error(`Selector not found: ${selector}`);

  const result = await cdp.evaluate(
    sessionId,
    `(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return { ok: false, reason: 'not_found' };
      el.scrollIntoView({ block: 'center', inline: 'center' });
      el.focus();
      const previous = typeof el.value === 'string' ? el.value : '';
      const next = ${append ? "previous + " : ""}${JSON.stringify(text)};
      if ('value' in el) {
        el.value = next;
      }
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, length: next.length, tag: el.tagName.toLowerCase() };
    })()`,
    parsed.timeoutMs,
  );

  if (!result?.ok) throw new Error(`Type target not found: ${selector}`);

  console.log(`✓ Typed ${result.length} chars into ${result.tag}`);
  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
