#!/usr/bin/env node

import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFileSync } from "node:fs";
import { connect } from "./cdp.js";
import { parseCommonPageArgs, resolveTargetId } from "./page-target.js";

const DEBUG = process.env.DEBUG === "1";
const log = DEBUG ? (...args) => console.error("[debug]", ...args) : () => {};

const { parsed } = parseCommonPageArgs(process.argv.slice(2));

const globalTimeout = setTimeout(() => {
  console.error("✗ Global timeout exceeded");
  process.exit(1);
}, Math.max(15000, parsed.timeoutMs + 2000));

try {
  log("connecting...");
  const cdp = await connect(5000);

  const targetId = await resolveTargetId(cdp, parsed);
  const sessionId = await cdp.attachToPage(targetId);

  const data = await cdp.screenshot(sessionId, parsed.timeoutMs);

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const filename = `screenshot-${timestamp}.png`;
  const filepath = join(tmpdir(), filename);

  writeFileSync(filepath, data);
  console.log(filepath);

  cdp.close();
} catch (e) {
  console.error("✗", e.message);
  process.exit(1);
} finally {
  clearTimeout(globalTimeout);
  setTimeout(() => process.exit(0), 80);
}
