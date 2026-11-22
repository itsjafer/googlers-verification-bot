# Contributing to Googlers Verification Bot

Contributions and PRs from moderators and community members are welcome!

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running Checks Locally

Before opening a PR, run the same checks that GitHub Actions runs in `.github/workflows/ci.yml`:

```bash
ruff check app tests scripts
ruff format --check app tests scripts
pytest --cov=app --cov-report=term-missing
```

If you modified files under `infra/`, also run:

```bash
cd infra
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

## How CI/CD Works

1. **Pull Requests:** GitHub Actions runs linting (`ruff`), unit tests (`pytest` with coverage), Terraform validation, and a `docker build` check. PRs **cannot** authenticate to GCP or deploy.
2. **Merges to `main`:** Changes under `app/`, `Dockerfile`, or `requirements.txt` automatically build a slim container image, push it to Artifact Registry via keyless Workload Identity Federation (no static GCP keys), deploy a new Cloud Run revision, and verify `/health`.
3. **Secrets & Infrastructure:** Bot tokens and the internal form secret live strictly in GCP Secret Manager (`bot-secrets`) and are never committed to Git or Terraform state. Infrastructure changes in `infra/` are applied manually by a project maintainer with `terraform plan` / `terraform apply`.

## Key Files
- `app/main.py` — FastAPI routes (`/health`, `/interactions`, `/tasks/*`, `/internal-verify`)
- `app/interactions.py` — Discord HTTP interaction handlers (buttons, modals, slash commands)
- `app/workflows.py` — Auto-verification, 1-click mod actions, and internal `@google.com` form verification
- `app/verifier.py` — Vertex AI (`gemini-3.5-flash-lite`) verification prompt & strict JSON schema
- `app/discord_api.py` — Discord REST API v10 client + Ed25519 signature verification + SSRF guard
- `infra/` — Terraform managing GCP APIs, Cloud Run, Cloud Tasks, Artifact Registry cleanup policy, \$1/mo budget alert, and GitHub WIF
