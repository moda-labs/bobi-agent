declare module "cloudflare:test" {
	interface ProvidedEnv extends Env {
		INTERNAL_DO_SECRET: string;
		BOBI_RELEASE_VERSION: string;
		BOBI_RELEASE_SHA: string;
	}
}
