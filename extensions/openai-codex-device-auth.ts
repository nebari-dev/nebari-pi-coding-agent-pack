import { createHash, randomBytes } from "node:crypto";

const CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann";
const ISSUER = "https://auth.openai.com";
const AUTHORIZE_URL = `${ISSUER}/oauth/authorize`;
const TOKEN_URL = `${ISSUER}/oauth/token`;
const DEVICE_USERCODE_URL = `${ISSUER}/api/accounts/deviceauth/usercode`;
const DEVICE_TOKEN_URL = `${ISSUER}/api/accounts/deviceauth/token`;
const DEVICE_VERIFICATION_URL = `${ISSUER}/codex/device`;

const JWT_CLAIM_PATH = "https://api.openai.com/auth";
const DEVICE_AUTH_TIMEOUT_MS = 15 * 60 * 1000;

type OAuthLoginCallbacks = {
  onAuth: (info: { url: string; instructions?: string }) => void;
  onPrompt: (prompt: { message: string; placeholder?: string }) => Promise<string>;
  onProgress?: (message: string) => void;
  signal?: AbortSignal;
};

type OAuthCredentials = {
  access: string;
  refresh: string;
  expires: number;
  accountId?: string;
};

function base64Url(input: Buffer) {
  return input
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function generatePKCE() {
  const verifier = base64Url(randomBytes(32));
  const challenge = base64Url(createHash("sha256").update(verifier).digest());
  return { verifier, challenge };
}

function createState() {
  return randomBytes(16).toString("hex");
}

function parseAuthorizationInput(input: string) {
  const value = (input || "").trim();
  if (!value) return {};

  try {
    const url = new URL(value);
    return {
      code: url.searchParams.get("code") || undefined,
      state: url.searchParams.get("state") || undefined,
    };
  } catch {
    // continue to alternate formats
  }

  if (value.includes("#")) {
    const [code, state] = value.split("#", 2);
    return { code, state };
  }

  if (value.includes("code=")) {
    const params = new URLSearchParams(value);
    return {
      code: params.get("code") || undefined,
      state: params.get("state") || undefined,
    };
  }

  return { code: value };
}

function decodeJwt(token: string) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = parts[1] || "";
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    return JSON.parse(Buffer.from(padded, "base64").toString("utf8"));
  } catch {
    return null;
  }
}

function extractAccountId(accessToken: string) {
  const payload = decodeJwt(accessToken) as any;
  const accountId = payload?.[JWT_CLAIM_PATH]?.chatgpt_account_id;
  if (!accountId || typeof accountId !== "string") {
    throw new Error("Failed to extract accountId from token");
  }
  return accountId;
}

async function exchangeAuthorizationCode({
  code,
  verifier,
  redirectUri,
  signal,
}: {
  code: string;
  verifier: string;
  redirectUri: string;
  signal?: AbortSignal;
}) {
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      code_verifier: verifier,
      redirect_uri: redirectUri,
    }),
    signal,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Token exchange failed (${response.status}): ${text || response.statusText}`);
  }

  const json = (await response.json()) as any;
  if (!json?.access_token || !json?.refresh_token || typeof json?.expires_in !== "number") {
    throw new Error("Token exchange returned invalid payload");
  }

  return {
    access: json.access_token,
    refresh: json.refresh_token,
    expires: Date.now() + json.expires_in * 1000,
  };
}

async function refreshAccessToken(credentials: OAuthCredentials, signal?: AbortSignal): Promise<OAuthCredentials> {
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: credentials.refresh,
      client_id: CLIENT_ID,
    }),
    signal,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`Token refresh failed (${response.status}): ${text || response.statusText}`);
  }

  const json = (await response.json()) as any;
  if (!json?.access_token || !json?.refresh_token || typeof json?.expires_in !== "number") {
    throw new Error("Token refresh returned invalid payload");
  }

  return {
    access: json.access_token,
    refresh: json.refresh_token,
    expires: Date.now() + json.expires_in * 1000,
    accountId: extractAccountId(json.access_token),
  };
}

async function requestDeviceCode(signal?: AbortSignal) {
  const response = await fetch(DEVICE_USERCODE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: CLIENT_ID }),
    signal,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    const err = new Error(`Device code request failed (${response.status}): ${text || response.statusText}`) as Error & {
      status?: number;
    };
    err.status = response.status;
    throw err;
  }

  const json = (await response.json()) as any;
  if (!json?.device_auth_id || !json?.user_code) {
    throw new Error("Device code response missing fields");
  }

  const intervalSeconds = Number.parseInt(String(json.interval ?? "5"), 10);
  return {
    deviceAuthId: String(json.device_auth_id),
    userCode: String(json.user_code),
    intervalSeconds: Number.isFinite(intervalSeconds) && intervalSeconds > 0 ? intervalSeconds : 5,
  };
}

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) {
    throw new Error("Login cancelled");
  }
}

async function sleep(ms: number, signal?: AbortSignal) {
  await new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("Login cancelled"));
      return;
    }

    const timeout = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(timeout);
          reject(new Error("Login cancelled"));
        },
        { once: true },
      );
    }
  });
}

async function pollForDeviceAuthorization({
  deviceAuthId,
  userCode,
  intervalSeconds,
  callbacks,
}: {
  deviceAuthId: string;
  userCode: string;
  intervalSeconds: number;
  callbacks: OAuthLoginCallbacks;
}) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < DEVICE_AUTH_TIMEOUT_MS) {
    throwIfAborted(callbacks.signal);

    const response = await fetch(DEVICE_TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_auth_id: deviceAuthId,
        user_code: userCode,
      }),
      signal: callbacks.signal,
    });

    if (response.ok) {
      const json = (await response.json()) as any;
      if (!json?.authorization_code || !json?.code_verifier) {
        throw new Error("Device token response missing authorization code or verifier");
      }
      return {
        code: String(json.authorization_code),
        verifier: String(json.code_verifier),
        redirectUri: `${ISSUER}/deviceauth/callback`,
      };
    }

    if (response.status === 403 || response.status === 404) {
      await sleep(intervalSeconds * 1000, callbacks.signal);
      continue;
    }

    const text = await response.text().catch(() => "");
    throw new Error(`Device authorization failed (${response.status}): ${text || response.statusText}`);
  }

  throw new Error("Device authorization timed out after 15 minutes");
}

async function loginWithDeviceCode(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials> {
  const { deviceAuthId, userCode, intervalSeconds } = await requestDeviceCode(callbacks.signal);

  callbacks.onAuth({
    url: DEVICE_VERIFICATION_URL,
    instructions: `Open this URL, sign in, and enter code: ${userCode}`,
  });
  callbacks.onProgress?.("Waiting for device authorization...");

  const grant = await pollForDeviceAuthorization({
    deviceAuthId,
    userCode,
    intervalSeconds,
    callbacks,
  });

  callbacks.onProgress?.("Exchanging authorization code for tokens...");
  const token = await exchangeAuthorizationCode({
    code: grant.code,
    verifier: grant.verifier,
    redirectUri: grant.redirectUri,
    signal: callbacks.signal,
  });

  return {
    access: token.access,
    refresh: token.refresh,
    expires: token.expires,
    accountId: extractAccountId(token.access),
  };
}

async function loginWithBrowserPaste(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials> {
  const { verifier, challenge } = generatePKCE();
  const state = createState();

  const url = new URL(AUTHORIZE_URL);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", CLIENT_ID);
  url.searchParams.set("redirect_uri", "http://localhost:1455/auth/callback");
  url.searchParams.set("scope", "openid profile email offline_access");
  url.searchParams.set("code_challenge", challenge);
  url.searchParams.set("code_challenge_method", "S256");
  url.searchParams.set("state", state);
  url.searchParams.set("id_token_add_organizations", "true");
  url.searchParams.set("codex_cli_simplified_flow", "true");
  url.searchParams.set("originator", "pi");

  callbacks.onAuth({
    url: url.toString(),
    instructions: "After login, paste the full redirect URL below.",
  });

  const input = await callbacks.onPrompt({
    message: "Paste the authorization code (or full redirect URL):",
    placeholder: "http://localhost:1455/auth/callback?code=...",
  });

  const parsed = parseAuthorizationInput(input);
  if (!parsed.code) {
    throw new Error("Missing authorization code");
  }
  if (parsed.state && parsed.state !== state) {
    throw new Error("State mismatch");
  }

  const token = await exchangeAuthorizationCode({
    code: parsed.code,
    verifier,
    redirectUri: "http://localhost:1455/auth/callback",
    signal: callbacks.signal,
  });

  return {
    access: token.access,
    refresh: token.refresh,
    expires: token.expires,
    accountId: extractAccountId(token.access),
  };
}

async function loginOpenAICodex(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials> {
  try {
    return await loginWithDeviceCode(callbacks);
  } catch (error) {
    const status = typeof error === "object" && error && "status" in error ? (error as any).status : undefined;
    if (status === 404) {
      callbacks.onProgress?.("Device-code login not enabled; falling back to browser flow...");
      return loginWithBrowserPaste(callbacks);
    }
    throw error;
  }
}

export default function openaiCodexDeviceAuthExtension(pi: {
  registerProvider: (
    name: string,
    config: {
      oauth: {
        name: string;
        login: (callbacks: OAuthLoginCallbacks) => Promise<OAuthCredentials>;
        refreshToken: (credentials: OAuthCredentials, signal?: AbortSignal) => Promise<OAuthCredentials>;
        getApiKey: (credentials: OAuthCredentials) => string;
      };
    },
  ) => void;
}) {
  pi.registerProvider("openai-codex", {
    oauth: {
      name: "ChatGPT Plus/Pro (Codex Subscription)",
      login: loginOpenAICodex,
      refreshToken: refreshAccessToken,
      getApiKey(credentials) {
        return credentials.access;
      },
    },
  });
}
