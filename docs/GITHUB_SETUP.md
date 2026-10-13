# GitHub Actions and publishing

The workflows in `.github/workflows` run the Python test suite, build and scan the container, and exercise Compose plus disposable kind recovery. Pull requests receive read-only repository permissions and do not publish images. A push to `main` publishes the verified image to GHCR with its full commit SHA, SBOM, and provenance.

The OpenProject integration tests use mocked HTTP responses; GitHub Actions does not need an OpenProject instance, API token, or project data. The deployment integration job checks that the application works with OpenProject unset.

For publishing, enable GitHub Actions and package write access for the repository. Check the Actions tab for actual run results. Publishing does not deploy to a persistent cluster.
