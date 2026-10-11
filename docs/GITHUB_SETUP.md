# Publish under Rastrith156

The GitHub connection authenticated as Rastrith156, but repository creation returned HTTP 403 (Resource not accessible by integration). No repository was created by the assistant.

## Secure connection route
Create an empty private repository named `stormpilot` under Rastrith156 in GitHub. Grant the connected GitHub app access to this repository with Contents write access and workflow-file write access where supported. Then ask the assistant to upload the project. Do not paste access tokens into chat.

## Local GitHub CLI alternative
Requires Git, GitHub CLI and an authenticated local GitHub session. Run from the extracted stormpilot folder. Authentication uses your terminal/browser, not chat.

```bash
gh auth login
gh api user --jq .login
# Confirm the displayed login is Rastrith156 before continuing.
git init -b main
git add .
git commit -m "Build StormPilot reliability lab"
gh repo create Rastrith156/stormpilot --private --source=. --remote=origin --push
```

If you already created an empty repository, use this instead of the final command:
```bash
git remote add origin https://github.com/Rastrith156/stormpilot.git
git push -u origin main
```

The first main push runs tests, Compose smoke checks, Kubernetes recovery checks and image scanning. Image publishing waits for all these checks. Check the Actions tab for actual results. GitHub Actions availability, account quota and permissions depend on your account. SBOM/provenance creation uses Buildx. Packages are not made public by these workflows.

No production/cloud deployment is configured. The Kubernetes CI job deploys only to its disposable runner cluster, which disappears after the job. Private GHCR images require registry authentication when pulling locally.
