#!/usr/bin/env node

import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId } from "./page-target.js";

const DEBUG = process.env.DEBUG === "1";
const log = DEBUG ? (...args) => console.error("[debug]", ...args) : () => {};

const { parsed, rest } = parseCommonPageArgs(process.argv.slice(2));
const code = rest.join(" ");
if (!code) {
  console.log("Usage: eval.js 'code' [--url-contains <frag>] [--target-id <id>] [--wait-new-tab] [--timeout-ms <ms>]");
  process.exit(1);
}

const globalTimeout = setTimeout(() => {
  console.error("✗ Global timeout exceeded");
  process.exit(1);
}, parsed.timeoutMs + 5000);

try {
  log("connecting...");
  const cdp = await connect(5000);

  const targetId = await resolveTargetId(cdp, parsed);
  const sessionId = await cdp.attachToPage(targetId);

  const expression = `(async () => { return (${code}); })()`;
  const result = await cdp.evaluate(sessionId, expression, parsed.timeoutMs);

  if (Array.isArray(result)) {
    for (let i = 0; i < result.length; i++) {
      if (i > 0) console.log("");
      for (const [key, value] of Object.entries(result[i])) {
        console.log(`${key}: ${value}`);
      }
    }
  } else if (typeof result === "object" && result !== null) {
    for (const [key, value] of Object.entries(result)) {
      console.log(`${key}: ${value}`);
    }
  } else {
    console.log(result);
  }

  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
