#!/usr/bin/env node

import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId, waitForCondition } from "./page-target.js";

const DEBUG = process.env.DEBUG === "1";
const log = DEBUG ? (...args) => console.error("[debug]", ...args) : () => {};

const { parsed, rest } = parseCommonPageArgs(process.argv.slice(2));
const url = rest[0];
const newTab = rest.includes("--new");

if (!url) {
  console.log("Usage: nav.js <url> [--new] [--url-contains <frag>] [--target-id <id>] [--wait-new-tab] [--timeout-ms <ms>]");
  console.log("\nExamples:");
  console.log("  nav.js https://example.com");
  console.log("  nav.js https://example.com --new");
  console.log("  nav.js https://example.com --url-contains keycloak");
  process.exit(1);
}

const globalTimeout = setTimeout(() => {
  console.error("✗ Global timeout exceeded");
  process.exit(1);
}, parsed.timeoutMs + 5000);

try {
  log("connecting...");
  const cdp = await connect(5000);

  let targetId;
  if (newTab) {
    log("creating new tab...");
    const created = await cdp.send("Target.createTarget", { url: "about:blank" });
    targetId = created?.targetId;
  }

  if (!targetId) {
    targetId = await resolveTargetId(cdp, parsed);
  }

  log("attaching to page...", targetId);
  const sessionId = await cdp.attachToPage(targetId);

  await cdp.send("Page.enable", {}, sessionId);
  await cdp.navigate(sessionId, url, parsed.timeoutMs);
  await waitForCondition(
    cdp,
    sessionId,
    "() => document.readyState === 'interactive' || document.readyState === 'complete'",
    Math.min(parsed.timeoutMs, 15000),
    250,
  );

  console.log(newTab ? "✓ Opened:" : "✓ Navigated to:", url);

  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
