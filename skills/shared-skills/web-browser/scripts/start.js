#!/usr/bin/env node

import { spawn, execSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync, readdirSync } from "node:fs";

const useProfile = process.argv[2] === "--profile";

if (process.argv[2] && process.argv[2] !== "--profile") {
  console.log("Usage: start.js [--profile]");
  process.exit(1);
}

const candidates = [
  process.env.CHROME_BIN,
  "chromium",
  "chromium-browser",
  "google-chrome",
  "google-chrome-stable",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

// Add Playwright-managed Chromium if available.
try {
  const pwRoot = process.env.PLAYWRIGHT_BROWSERS_PATH || "/opt/ms-playwright";
  if (existsSync(pwRoot)) {
    for (const entry of readdirSync(pwRoot)) {
      if (!entry.startsWith("chromium-")) continue;
      const p = join(pwRoot, entry, "chrome-linux", "chrome");
      if (existsSync(p)) candidates.push(p);
      const hs = join(pwRoot, entry, "chrome-linux", "headless_shell");
      if (existsSync(hs)) candidates.push(hs);
    }
  }
} catch {}

let chromeBin = null;
for (const c of candidates) {
  try {
    if (c.includes("/")) {
      execSync(`test -x "${c}"`);
      chromeBin = c;
      break;
    }
    execSync(`command -v ${c}`);
    chromeBin = c;
    break;
  } catch {}
}

if (!chromeBin) {
  console.error("✗ No Chrome/Chromium binary found (set CHROME_BIN if needed)");
  process.exit(1);
}

// Best-effort stop old browser instances
for (const proc of ["chromium", "chromium-browser", "google-chrome", "Google Chrome"]) {
  try {
    execSync(`killall "${proc}"`, { stdio: "ignore" });
  } catch {}
}

await new Promise((r) => setTimeout(r, 800));

execSync("mkdir -p ~/.cache/scraping", { stdio: "ignore" });

if (useProfile) {
  // Keep compatibility for mac profile copying. No-op elsewhere.
  try {
    execSync(
      `rsync -a --delete "${process.env["HOME"]}/Library/Application Support/Google/Chrome/" ~/.cache/scraping/`,
      { stdio: "pipe" },
    );
  } catch {}
}

const headless = (process.env.PI_BROWSER_HEADLESS || "1") !== "0";

const chromeArgs = [
  "--remote-debugging-port=9222",
  "--remote-debugging-address=127.0.0.1",
  `--user-data-dir=${process.env["HOME"]}/.cache/scraping`,
  "--profile-directory=Default",
  "--no-first-run",
  "--disable-search-engine-choice-screen",
  "--disable-features=ProfilePicker",
  "--no-sandbox",
  "--disable-dev-shm-usage",
];

if (headless) chromeArgs.push("--headless=new");

spawn(chromeBin, chromeArgs, { detached: true, stdio: "ignore" }).unref();

let connected = false;
for (let i = 0; i < 40; i++) {
  try {
    const response = await fetch("http://localhost:9222/json/version");
    if (response.ok) {
      connected = true;
      break;
    }
  } catch {}
  await new Promise((r) => setTimeout(r, 500));
}

if (!connected) {
  console.error("✗ Failed to connect to Chrome DevTools on :9222");
  process.exit(1);
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const watcherPath = join(scriptDir, "watch.js");
spawn(process.execPath, [watcherPath], { detached: true, stdio: "ignore" }).unref();

console.log(`✓ Browser started (${chromeBin}) on :9222${headless ? " [headless]" : ""}`);
