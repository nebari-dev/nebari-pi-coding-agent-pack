#!/usr/bin/env node

import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId, waitForCondition } from "./page-target.js";

const { parsed, rest } = parseCommonPageArgs(process.argv.slice(2));

function valueAfter(flag) {
  const i = rest.indexOf(flag);
  if (i === -1) return "";
  return rest[i + 1] || "";
}

const urlContains = valueAfter("--url-contains") || parsed.urlContains;
const selector = valueAfter("--selector");
const textContains = valueAfter("--text");

if (!urlContains && !selector && !textContains) {
  console.log("Usage: wait.js [--url-contains <frag>] [--selector <css>] [--text <contains>] [--target-id <id>] [--wait-new-tab] [--timeout-ms <ms>]");
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

  let expr = "() => true";
  if (urlContains) {
    expr = `(function(){ return String(location.href || '').toLowerCase().includes(${JSON.stringify(urlContains.toLowerCase())}); })()`;
  } else if (selector) {
    expr = `(function(){ return document.querySelector(${JSON.stringify(selector)}) != null; })()`;
  } else if (textContains) {
    expr = `(function(){ return (document.body && document.body.innerText ? document.body.innerText.toLowerCase() : '').includes(${JSON.stringify(textContains.toLowerCase())}); })()`;
  }

  const ok = await waitForCondition(cdp, sessionId, expr, parsed.timeoutMs, 250);
  if (!ok) {
    throw new Error("Condition not met before timeout");
  }

  const finalUrl = await cdp.evaluate(sessionId, "(function(){ return location.href; })()", 5000);
  console.log(`✓ Wait condition met on ${finalUrl}`);
  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
