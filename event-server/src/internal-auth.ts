export const INTERNAL_HEADER = "x-modastack-internal";
export const INTERNAL_WS_PROTOCOL_PREFIX = "modastack-internal.";

interface InternalEnv {
	INTERNAL_DO_SECRET: string;
}

export function internalEventRequest(env: InternalEnv, url: string, body: string): Request {
	return new Request(url, {
		method: "POST",
		headers: { [INTERNAL_HEADER]: env.INTERNAL_DO_SECRET },
		body,
	});
}

export function internalWebSocketRequest(env: InternalEnv, url: string): Request {
	return new Request(url, {
		headers: {
			Upgrade: "websocket",
			"Sec-WebSocket-Protocol": internalWebSocketProtocol(env.INTERNAL_DO_SECRET),
		},
	});
}

export function internalWebSocketProtocol(secret: string): string {
	return `${INTERNAL_WS_PROTOCOL_PREFIX}${base64UrlEncode(secret)}`;
}

export function internalSecretFromWebSocketProtocols(header: string | null): string | null {
	if (!header) return null;
	for (const part of header.split(",")) {
		const token = part.trim();
		if (!token.startsWith(INTERNAL_WS_PROTOCOL_PREFIX)) continue;
		return base64UrlDecode(token.slice(INTERNAL_WS_PROTOCOL_PREFIX.length));
	}
	return null;
}

function base64UrlEncode(value: string): string {
	return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlDecode(value: string): string {
	const padded = value.replace(/-/g, "+").replace(/_/g, "/")
		+ "=".repeat((4 - (value.length % 4)) % 4);
	return atob(padded);
}
