#!/usr/bin/env node

import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId, waitForCondition } from "./page-target.js";

const { parsed, rest } = parseCommonPageArgs(process.argv.slice(2));
const selector = rest[0] || "";

function parseFlag(flag) {
  const idx = rest.indexOf(flag);
  if (idx === -1) return "";
  return rest[idx + 1] || "";
}

const textContains = parseFlag("--text");

if (!selector) {
  console.log("Usage: click.js <selector> [--text <contains>] [--url-contains <frag>] [--target-id <id>] [--wait-new-tab] [--timeout-ms <ms>]");
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
      const selector = ${JSON.stringify(selector)};
      const textNeedle = ${JSON.stringify(textContains || "")}.toLowerCase();
      const candidates = Array.from(document.querySelectorAll(selector));
      let target = candidates[0] || null;
      if (textNeedle) {
        target = candidates.find((el) => (el.textContent || '').toLowerCase().includes(textNeedle)) || null;
      }
      if (!target) {
        return { ok: false, reason: 'not_found' };
      }
      target.scrollIntoView({ block: 'center', inline: 'center' });
      const rect = target.getBoundingClientRect();
      target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
      target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
      target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
      target.click();
      return {
        ok: true,
        tag: target.tagName.toLowerCase(),
        text: (target.textContent || '').trim().slice(0, 160),
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
      };
    })()`,
    parsed.timeoutMs,
  );

  if (!result?.ok) throw new Error(`Click target not found for selector: ${selector}`);

  console.log(`✓ Clicked ${result.tag} '${result.text}'`);
  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
