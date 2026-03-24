import fs from "node:fs/promises";
import path from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@mariozechner/pi-coding-agent";

type ParsedArgs = {
	tokens: string[];
	kv: Record<string, string>;
};

const tokenize = (input: string): string[] => {
	const out: string[] = [];
	let current = "";
	let quote: '"' | "'" | null = null;
	for (let i = 0; i < input.length; i++) {
		const ch = input[i];
		if (quote) {
			if (ch === quote) {
				quote = null;
				continue;
			}
			current += ch;
			continue;
		}
		if (ch === '"' || ch === "'") {
			quote = ch;
			continue;
		}
		if (/\s/.test(ch)) {
			if (current) {
				out.push(current);
				current = "";
			}
			continue;
		}
		current += ch;
	}
	if (current) out.push(current);
	return out;
};

const parseArgs = (raw: string): ParsedArgs => {
	const tokens = tokenize(raw.trim());
	const kv: Record<string, string> = {};
	for (const token of tokens) {
		const idx = token.indexOf("=");
		if (idx <= 0) continue;
		const key = token.slice(0, idx).trim().toLowerCase().replace(/^--/, "");
		const value = token.slice(idx + 1).trim();
		if (key) kv[key] = value;
	}
	return { tokens, kv };
};

const splitCsv = (value: string | undefined): string[] =>
	(value || "")
		.split(",")
		.map((v) => v.trim())
		.filter(Boolean)
		.filter((v, i, arr) => arr.findIndex((x) => x.toLowerCase() === v.toLowerCase()) === i);

const apiBase = (): string =>
	(process.env.PI_SESSION_VIEWER_INTERNAL_URL || "http://hub:10400/services/pi-session-viewer").replace(/\/$/, "");

const authHeaders = (): Record<string, string> => {
	const token =
		process.env.JUPYTERHUB_FULL_API_TOKEN || process.env.PI_SESSION_VIEWER_API_TOKEN || process.env.NEBARI_HUB_API_TOKEN || "";
	const user = process.env.JUPYTERHUB_USER || "";
	if (!token || !user) {
		throw new Error("Missing JUPYTERHUB_FULL_API_TOKEN or JUPYTERHUB_USER in environment.");
	}
	return {
		Authorization: `token ${token}`,
		"X-PI-USER": user,
	};
};

const requestJson = async <T>(url: string, init: RequestInit): Promise<T> => {
	const response = await fetch(url, init);
	const text = await response.text();
	let data: any = {};
	try {
		data = text ? JSON.parse(text) : {};
	} catch {
		if (!response.ok) {
			throw new Error(text || `HTTP ${response.status}`);
		}
	}
	if (!response.ok) {
		const err = data?.message || data?.error || text || `HTTP ${response.status}`;
		throw new Error(err);
	}
	return data as T;
};

const currentSessionPath = (ctx: ExtensionCommandContext): string | undefined => ctx.sessionManager.getSessionFile();

const resolveSharePath = (parsed: ParsedArgs, ctx: ExtensionCommandContext): string | undefined => {
	const explicit = parsed.kv.path || parsed.kv.session || parsed.tokens.find((t) => !t.includes("="));
	if (explicit) return explicit;
	return currentSessionPath(ctx);
};

const formatUsage =
	"Usage: /session-share [path] users=alice,bob groups=team-a title=\"My session\" expires=24\n" +
	"- path defaults to current session\n" +
	"- provide at least users= or groups=";

export default function sessionShareExtension(pi: ExtensionAPI) {
	pi.registerCommand("session-share", {
		description: "Share current (or specified) session via Pi Session Viewer",
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			try {
				const parsed = parseArgs(args);
				const sharePath = resolveSharePath(parsed, ctx);
				if (!sharePath) {
					ctx.ui.notify(`No session file found. ${formatUsage}`, "error");
					return;
				}

				const users = splitCsv(parsed.kv.users);
				const groups = splitCsv(parsed.kv.groups);
				if (users.length === 0 && groups.length === 0) {
					ctx.ui.notify(`Missing ACL. ${formatUsage}`, "error");
					return;
				}

				const expires = Number.parseInt(parsed.kv.expires || parsed.kv.exp || "", 10);
				const expiresHours = Number.isFinite(expires) && expires > 0 ? expires : undefined;
				const title = parsed.kv.title || path.basename(sharePath);

				const content = await fs.readFile(sharePath);
				const payload: any = {
					session_path: sharePath,
					title,
					share_with_users: users,
					share_with_groups: groups,
					content_base64: content.toString("base64"),
				};
				if (expiresHours) payload.expires_hours = expiresHours;

				const headers = authHeaders();
				const data = await requestJson<{ share?: { id: string; viewer_url?: string; expires_at?: string } }>(
					`${apiBase()}/api/shares`,
					{
						method: "POST",
						headers: {
							...headers,
							"Content-Type": "application/json",
						},
						body: JSON.stringify(payload),
					},
				);

				const share = data?.share;
				if (!share?.id) {
					ctx.ui.notify("Share created, but response was missing share id.", "warning");
					return;
				}
				ctx.ui.notify(
					`Shared ${share.id}${share.expires_at ? ` (expires ${share.expires_at})` : ""}\n${share.viewer_url || ""}`,
					"info",
				);
			} catch (error) {
				ctx.ui.notify(`session-share failed: ${(error as Error).message}`, "error");
			}
		},
	});

	pi.registerCommand("session-shares", {
		description: "List session shares (mine|with-me)",
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			const scopeRaw = (args || "").trim().toLowerCase();
			const scope = scopeRaw === "mine" || scopeRaw === "all" ? scopeRaw : "with-me";
			try {
				const headers = authHeaders();
				const data = await requestJson<{ shares?: Array<{ id: string; title: string; owner: string; expires_at: string; viewer_url?: string }> }>(
					`${apiBase()}/api/shares?scope=${encodeURIComponent(scope)}`,
					{
						method: "GET",
						headers,
					},
				);
				const shares = data.shares || [];
				if (shares.length === 0) {
					ctx.ui.notify(`No shares for scope=${scope}.`, "info");
					return;
				}
				const lines = shares.slice(0, 12).map((s) => `${s.id} · ${s.owner} · ${s.title} · expires ${s.expires_at}`);
				ctx.ui.notify(lines.join("\n"), "info");
			} catch (error) {
				ctx.ui.notify(`session-shares failed: ${(error as Error).message}`, "error");
			}
		},
	});

	pi.registerCommand("session-revoke", {
		description: "Revoke one of your shares: /session-revoke <share-id>",
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			const shareId = (args || "").trim();
			if (!shareId) {
				ctx.ui.notify("Usage: /session-revoke <share-id>", "error");
				return;
			}
			try {
				const headers = authHeaders();
				await requestJson(`${apiBase()}/api/shares/${encodeURIComponent(shareId)}/revoke`, {
					method: "POST",
					headers,
				});
				ctx.ui.notify(`Revoked share ${shareId}`, "info");
			} catch (error) {
				ctx.ui.notify(`session-revoke failed: ${(error as Error).message}`, "error");
			}
		},
	});
}
