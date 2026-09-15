# Self-host Bobi on Kubernetes with a Cloudflare Worker

This example is the smallest public consumer proof: Terraform deploys the
durable event-server Worker into your Cloudflare account, and Kubernetes runs
the published Bobi image under its supervisor sidecar. The operator checks and
restart command run from outside the cluster through the Worker's `/fleet` API.

It uses no Moda deployment repository or hosted control plane.

## Requirements

- A Cloudflare account on the Workers Paid plan. Bobi uses a SQLite-backed
  Durable Object.
- A Cloudflare API token with Workers Scripts Write and Workers KV Storage
  Write for the target account.
- Terraform 1.8+, Node 22+, `npm`, `kubectl`, `curl`, `jq`, `openssl`, and a
  Kubernetes cluster.
- A released Bobi image tag from
  [GHCR](https://github.com/moda-labs/bobi-agent/pkgs/container/bobi).

The Terraform state contains the Worker secrets supplied below. Keep the state
in a protected backend, or use an isolated local state and destroy it when the
proof is complete. Never commit it.

## 1. Build the public Worker source

From a clean clone of this repository:

```bash
cd event-server
npm ci --no-audit --no-fund
mkdir -p ../.tmp/self-host-worker
npx --no-install wrangler deploy --dry-run \
  --config worker/wrangler.jsonc \
  --outdir ../.tmp/self-host-worker
cd ..
```

Wrangler only bundles the TypeScript here. Terraform owns the Cloudflare
resources and deployment.

## 2. Deploy the Worker with Terraform

Use unique, randomly generated values for both credentials:

```bash
export CLOUDFLARE_API_TOKEN=...
export TF_VAR_cloudflare_account_id=...
export TF_VAR_worker_name="bobi-self-host-$(openssl rand -hex 4)"
export TF_VAR_worker_bundle_path="$PWD/.tmp/self-host-worker/index.js"
export TF_VAR_release_version=self-host-example
export TF_VAR_release_sha="$(git rev-parse HEAD)"
export TF_VAR_internal_do_secret="$(openssl rand -hex 32)"
export TF_VAR_fleet_operator_token="$(openssl rand -hex 32)"

terraform -chdir=examples/self-host/terraform init
terraform -chdir=examples/self-host/terraform apply
export BOBI_EVENT_SERVER="$(terraform -chdir=examples/self-host/terraform output -raw worker_url)"
```

The module creates one Worker, one isolated KV namespace, the
`DEPLOYMENT_SESSION` Durable Object with the shipped `v1` SQLite migration, and
the public workers.dev route. It keeps the compatibility date and
`nodejs_compat` flag aligned with `event-server/worker/wrangler.jsonc`.

Verify the deployed server before starting Kubernetes:

```bash
curl -fsS "$BOBI_EVENT_SERVER/health" | jq
```

## 3. Run the released image on Kubernetes

This example team uses Bobi's test-only stub brain so the proof exercises
deployment and remote control without spending a model call. Use a real team
and its matching credentials for an actual installation.

```bash
export BOBI_VERSION="$(tr -d '[:space:]' < VERSION)"
export BOBI_IMAGE="ghcr.io/moda-labs/bobi:$BOBI_VERSION"
export BOBI_FLEET=self-host-proof
export BOBI_INSTANCE=self-host-consumer

kubectl create configmap bobi-self-host-team \
  --from-file=agent.yaml=examples/self-host/kubernetes/team/agent.yaml \
  --from-file=ROLE.md=examples/self-host/kubernetes/team/roles/manager/ROLE.md

kubectl create configmap bobi-self-host-runtime \
  --from-literal=event_server_url="$BOBI_EVENT_SERVER" \
  --from-literal=fleet="$BOBI_FLEET" \
  --from-literal=instance="$BOBI_INSTANCE" \
  --from-literal=image="$BOBI_IMAGE"

sed "s|ghcr.io/moda-labs/bobi:latest|$BOBI_IMAGE|" \
  examples/self-host/kubernetes/deployment.yaml | kubectl apply -f -
kubectl rollout status deployment/bobi-self-host-consumer --timeout=5m
```

`shareProcessNamespace: true` supplies the pod-level process handling required
by the reference image. The health port stays inside the cluster; only the
outbound Worker connection is public.

## 4. Observe and restart from outside the cluster

The runner, laptop, or operator host needs only the Worker URL and operator
token. It does not need Kubernetes network access:

```bash
auth="Authorization: Bearer $TF_VAR_fleet_operator_token"
detail="$BOBI_EVENT_SERVER/fleet/instances/$BOBI_FLEET/$BOBI_INSTANCE"

curl -fsS -H "$auth" "$detail" | jq

command_url="$BOBI_EVENT_SERVER/fleet/instances/$BOBI_FLEET/$BOBI_INSTANCE/commands"
status_path="$(curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' \
  -d '{"command":"restart","args":{}}' "$command_url" | jq -r .status_url)"

curl -fsS -H "$auth" "$BOBI_EVENT_SERVER$status_path" | jq
```

The first detail response must report `deployment.platform: "k8s"` plus the
pod and node identity. After the command resolves, a later heartbeat must carry
a different `manager.pid` and `manager.last_restart_reason: "operator"`.

## Cleanup

```bash
kubectl delete deployment bobi-self-host-consumer
kubectl delete configmap bobi-self-host-team bobi-self-host-runtime
terraform -chdir=examples/self-host/terraform destroy
```

The live repository lane performs this same flow with a disposable kind
cluster and uniquely named Cloudflare resources, and attempts Terraform cleanup
even when the proof fails.
