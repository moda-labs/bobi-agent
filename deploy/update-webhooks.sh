#!/bin/bash
# Update all webhooks to point to the current machine's public IP.
# Run this after deploying to EC2 or when the IP changes.
#
# Usage: ./deploy/update-webhooks.sh

set -euo pipefail

source "$(dirname "$0")/../.venv/bin/activate"

# Get public IP
if curl -s --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 &>/dev/null; then
    # EC2 instance metadata
    PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
    WEBHOOK_URL="http://${PUBLIC_IP}:8080"
elif command -v ngrok &>/dev/null && curl -s http://localhost:4040/api/tunnels &>/dev/null; then
    # Local with ngrok
    WEBHOOK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])")
else
    echo "Error: Can't determine public URL. Start ngrok or run on EC2."
    exit 1
fi

echo "Webhook URL: $WEBHOOK_URL"
echo ""

# --- GitHub ---
echo "=== GitHub Webhooks ==="
for repo in $(gh api user/repos --jq '.[].full_name' --limit 100 2>/dev/null); do
    hooks=$(gh api "repos/$repo/hooks" --jq '.[].id' 2>/dev/null || echo "")
    for hook_id in $hooks; do
        old_url=$(gh api "repos/$repo/hooks/$hook_id" --jq '.config.url' 2>/dev/null)
        if [[ "$old_url" == *"/webhooks/github"* ]]; then
            gh api "repos/$repo/hooks/$hook_id" --method PATCH \
                -f "config[url]=${WEBHOOK_URL}/webhooks/github" \
                -f "config[content_type]=json" \
                --jq '.config.url' 2>/dev/null
            echo "  Updated: $repo → ${WEBHOOK_URL}/webhooks/github"
        fi
    done
done

# --- Linear ---
echo ""
echo "=== Linear Webhooks ==="
python3 << PYEOF
import yaml, httpx, truststore
truststore.inject_into_ssl()
from pathlib import Path

WEBHOOK_URL = "${WEBHOOK_URL}"
creds = yaml.safe_load((Path.home() / ".modastack" / "credentials.yaml").read_text())
seen = set()
for name, entry in creds.items():
    key = entry.get("linear_api_key", "")
    if not key or key in seen:
        continue
    seen.add(key)

    # List existing webhooks
    r = httpx.post("https://api.linear.app/graphql",
        headers={"Authorization": key, "Content-Type": "application/json"},
        json={"query": "{ webhooks { nodes { id url enabled } } }"})
    webhooks = r.json().get("data", {}).get("webhooks", {}).get("nodes", [])

    for wh in webhooks:
        if "/webhooks/linear" in wh["url"]:
            # Update existing webhook
            new_url = f"{WEBHOOK_URL}/webhooks/linear"
            r2 = httpx.post("https://api.linear.app/graphql",
                headers={"Authorization": key, "Content-Type": "application/json"},
                json={"query": f'mutation {{ webhookUpdate(id: "{wh["id"]}", input: {{ url: "{new_url}" }}) {{ success }} }}'})
            print(f"  Updated: {name} → {new_url}")
            break
    else:
        # No existing webhook — create one
        new_url = f"{WEBHOOK_URL}/webhooks/linear"
        r3 = httpx.post("https://api.linear.app/graphql",
            headers={"Authorization": key, "Content-Type": "application/json"},
            json={
                "query": 'mutation(\$url: String!, \$r: [String!]!) { webhookCreate(input: { url: \$url, resourceTypes: \$r, allPublicTeams: true, enabled: true }) { success } }',
                "variables": {"url": new_url, "r": ["Issue", "Comment"]},
            })
        print(f"  Created: {name} → {new_url}")
PYEOF

echo ""
echo "Done. Webhooks pointing to: $WEBHOOK_URL"
