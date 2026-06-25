export const INTERNAL_HEADER = "x-modastack-internal";

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
			[INTERNAL_HEADER]: env.INTERNAL_DO_SECRET,
		},
	});
}
