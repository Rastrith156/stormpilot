# GitHub Actions and publishing

The workflows in `.github/workflows` run the Python test suite, build and scan the container, validate both Compose profiles, and exercise the local Compose stack plus disposable kind recovery. The production profile is configuration-checked with fake runner-local secret files; CI does not authenticate against an actual identity provider or Redmine. Pull requests receive read-only repository permissions and do not publish images. A push to `main` publishes the verified image to GHCR with its full commit SHA, SBOM, and provenance.

The Redmine integration tests use mocked HTTP responses; GitHub Actions does not need a Redmine instance, API key, or project data. The deployment integration job checks that the application works with Redmine unset.

For publishing, enable GitHub Actions and package write access for the repository. Check the Actions tab for actual run results. Publishing does not deploy to a persistent cluster.
