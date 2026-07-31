declare module "cloudflare:test" {
	interface ProvidedEnv extends Env {
		INTERNAL_DO_SECRET: string;
		BOBI_RELEASE_VERSION: string;
		BOBI_RELEASE_SHA: string;
		FLEET_OPERATOR_TOKEN: string;
		FLEET_LIVE_WINDOW_S: string;
		FLEET_STALE_WINDOW_S: string;
	}
}
