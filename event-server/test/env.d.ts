declare module "cloudflare:test" {
	interface ProvidedEnv extends Env {
		INTERNAL_DO_SECRET: string;
	}
}
