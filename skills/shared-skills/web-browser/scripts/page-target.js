#!/usr/bin/env node

export function parseCommonPageArgs(argv) {
  const args = [...argv];
  const out = {
    targetId: "",
    urlContains: "",
    waitNewTab: false,
    timeoutMs: 45000,
  };

  function consume(flag) {
    const i = args.indexOf(flag);
    if (i === -1) return "";
    const v = args[i + 1] || "";
    args.splice(i, v ? 2 : 1);
    return v;
  }

  function consumeBool(flag) {
    const i = args.indexOf(flag);
    if (i === -1) return false;
    args.splice(i, 1);
    return true;
  }

  out.targetId = consume("--target-id").trim();
  out.urlContains = consume("--url-contains").trim();
  out.waitNewTab = consumeBool("--wait-new-tab");

  const timeoutRaw = consume("--timeout-ms").trim();
  if (timeoutRaw) {
    const n = Number(timeoutRaw);
    if (Number.isFinite(n) && n > 0) out.timeoutMs = Math.floor(n);
  }

  return { parsed: out, rest: args };
}

function pickBestPage(pages, urlContains = "") {
  if (!Array.isArray(pages) || pages.length === 0) return null;
  if (urlContains) {
    const needle = urlContains.toLowerCase();
    for (let i = pages.length - 1; i >= 0; i--) {
      const p = pages[i];
      const url = String(p?.url || "").toLowerCase();
      if (url.includes(needle)) return p;
    }
  }
  return pages[pages.length - 1] || null;
}

export async function resolveTargetId(cdp, options = {}) {
  const targetId = String(options.targetId || "").trim();
  if (targetId) return targetId;

  const urlContains = String(options.urlContains || "").trim();
  const timeoutMs = Number(options.timeoutMs || 45000);

  if (options.waitNewTab) {
    const known = new Set((await cdp.getPages()).map((p) => p.targetId));
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const pages = await cdp.getPages();
      const candidates = pages.filter((p) => !known.has(p.targetId));
      const picked = pickBestPage(candidates, urlContains);
      if (picked?.targetId) return picked.targetId;
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error("Timed out waiting for new tab/page");
  }

  const pages = await cdp.getPages();
  const picked = pickBestPage(pages, urlContains);
  if (!picked?.targetId) {
    throw new Error("No active tab found");
  }
  return picked.targetId;
}

export async function waitForCondition(cdp, sessionId, expr, timeoutMs = 30000, pollMs = 250) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const ok = await cdp.evaluate(sessionId, expr, Math.max(5000, pollMs + 1000));
    if (ok) return true;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return false;
}
