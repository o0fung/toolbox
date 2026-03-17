# Releasing `lf-toolbox` to PyPI (via GitHub Release)

This project is configured to publish to PyPI automatically from GitHub Actions when a GitHub Release is published.

Workflow file: `.github/workflows/publish-pypi.yml`

## One-time setup

### 1) Configure PyPI trusted publishing

In PyPI, configure a Trusted Publisher for this project:

- PyPI project name: `lf-toolbox`
- Owner: `o0fung` (or your GitHub org/user if this repo moves)
- Repository name: `toolbox`
- Workflow name: `publish-pypi.yml`
- Environment name: `pypi`

Notes:

- No `PYPI_API_TOKEN` secret is needed with trusted publishing.
- If `lf-toolbox` does not exist yet on PyPI, create a Pending Publisher first, then run the first release.

### 2) Configure GitHub environment

In GitHub repo settings, create environment `pypi` (same name as workflow).  
Optional: add required reviewers for manual approval before publish.

### 3) Ensure local install tooling exists (for verification)

```sh
python -m pip install --user pipx
pipx ensurepath
```

## Per-release workflow

### 1) Bump version in `pyproject.toml`

```toml
[project]
version = "X.Y.Z"
```

PyPI does not allow re-uploading the same version.

### 2) Optional local preflight check

```sh
python -m venv .venv-release
source .venv-release/bin/activate
python -m pip install -U pip
python -m pip install -e ".[publish]"
rm -rf dist build *.egg-info
python -m build
python -m twine check dist/*
```

### 3) Commit, tag, and push

```sh
git add pyproject.toml
git commit -m "Release X.Y.Z"
git tag vX.Y.Z
git push origin main --tags
```

Tag rule: the workflow expects tag `vX.Y.Z` to match `project.version` exactly (without the leading `v`).

### 4) Publish GitHub Release

Create a GitHub Release from tag `vX.Y.Z` and click **Publish release**.

This triggers `.github/workflows/publish-pypi.yml`, which:

1. Builds sdist and wheel.
2. Runs `twine check`.
3. Publishes to PyPI using OIDC trusted publishing.

### 5) Verify end-user install path

```sh
pipx install lf-toolbox
lf --help
```

Upgrade path:

```sh
pipx upgrade lf-toolbox
```

## Troubleshooting

- `invalid-publisher` / `permission denied` on publish:
  - Trusted Publisher settings in PyPI do not match repo/workflow/environment.
- Workflow fails with tag/version mismatch:
  - Git tag and `pyproject.toml` version differ.
- `File already exists` on upload:
  - That version is already on PyPI; bump version and release again.
- `No matching distribution found` right after release:
  - Wait for index propagation and retry in a minute.
