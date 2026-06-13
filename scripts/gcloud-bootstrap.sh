#!/usr/bin/env bash
set -euo pipefail

# Configurable project (override via: PROJECT=my-project bash scripts/gcloud-bootstrap.sh)
PROJECT="${PROJECT:-green-jet-499318-i3}"
CREDENTIALS_FILE="${HOME}/.config/verified-googledocs-mcp/credentials.json"

# ──────────────────────────────────────────────────────
# Step 1 — Check gcloud is installed
# ──────────────────────────────────────────────────────
if ! command -v gcloud >/dev/null 2>&1; then
  echo "✗ gcloud not found."
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf "Install via Homebrew? (y/n): "
    read -r answer
    if [[ "$answer" == "y" || "$answer" == "Y" ]]; then
      brew install --cask google-cloud-sdk
      echo ""
      echo "✓ google-cloud-sdk installed. Restart your shell to use gcloud:"
      echo "  exec -l \$SHELL"
      exit 0
    fi
  fi
  echo "Install gcloud from: https://cloud.google.com/sdk/docs/install"
  exit 1
fi

echo "✓ gcloud found: $(gcloud version --format="value(Google Cloud SDK)" 2>/dev/null || echo "installed")"

# ──────────────────────────────────────────────────────
# Step 2 — Check gcloud is authenticated
# ──────────────────────────────────────────────────────
ACCOUNT=$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null)

if [[ -z "$ACCOUNT" ]]; then
  echo "✗ No active gcloud account. Run:"
  echo "  gcloud auth login"
  echo "Then re-run this script."
  exit 1
fi

echo "✓ Authenticated as: $ACCOUNT"

# ──────────────────────────────────────────────────────
# Step 3 — Set GCP project
# ──────────────────────────────────────────────────────
gcloud config set project "$PROJECT"
echo "✓ Project set: $PROJECT"

# ──────────────────────────────────────────────────────
# Step 4 — Enable APIs
# ──────────────────────────────────────────────────────
echo "→ Enabling Docs and Drive APIs..."
gcloud services enable docs.googleapis.com drive.googleapis.com
gcloud services list --enabled \
  --filter="config.name:(docs.googleapis.com drive.googleapis.com)" \
  --format="table(config.name,state)"
echo "✓ APIs enabled."

# ──────────────────────────────────────────────────────
# Step 5 — Check if credentials already exist
# ──────────────────────────────────────────────────────
if [[ -f "$CREDENTIALS_FILE" ]]; then
  echo "✓ credentials.json already present at $CREDENTIALS_FILE"
  echo "✓ Setup complete. Run: uv run verified-googledocs-mcp auth"
  exit 0
fi

cat <<EOF

✓ GCP project and APIs are ready.

──────────────────────────────────────────────────────
  3 MANUAL STEPS REQUIRED (console-only — no public API)
  These take ~3 minutes total.
──────────────────────────────────────────────────────

Step 1 — Configure OAuth Consent Screen
  Open: https://console.cloud.google.com/auth/overview?project=$PROJECT
  • User Type: External
  • Publishing status: Testing
  • Add your Google account as a test user
  • Click Save

Step 2 — Create a Desktop OAuth Client ID
  Open: https://console.cloud.google.com/apis/credentials?project=$PROJECT
  • Click "Create Credentials" → "OAuth client ID"
  • Application type: Desktop app
  • Name it anything (e.g. verified-googledocs-mcp)
  • Click Create

Step 3 — Download and place the client secret
  • Click the download icon (↓) next to your new client
  • Save the file to:
      $CREDENTIALS_FILE

Then authorize the MCP (one-time browser consent):
  uv run verified-googledocs-mcp auth

Re-run this script to confirm setup is complete.
──────────────────────────────────────────────────────
EOF
