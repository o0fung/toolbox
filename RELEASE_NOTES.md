# `lf-toolbox` v1.10.3

This release rounds out `lf-toolbox` into a complete multi-tool CLI and adds the GitHub Release -> PyPI publishing flow for simpler shipping and upgrades.

## Highlights

- Added/solidified five core CLI tools under `lf`:
  - `tree`: directory tree view + batch file processor
  - `youtube`: metadata, format listing, and download helpers
  - `clock`: full-screen clock, stopwatch, and countdown
  - `cheque`: HK cheque wording (Traditional Chinese + English)
  - `plot`: CSV/TSV plotting with pyqtgraph
- Improved YouTube workflow with selectable format support and configurable output directory.
- Expanded plotting workflow with better channel selection/axis handling and save/export options.
- Refactored CLI wiring and shared command infrastructure for cleaner command registration and output behavior.
- Added release automation docs/workflow so publishing a GitHub Release can trigger PyPI publishing reliably.

## Packaging and release

- Version bumped to `1.10.2`.
- Updated package/release metadata and distribution inputs (`pyproject`, manifest/workflow/readme alignment).
- Release process documented in `RELEASING.md` (tag/version matching, build checks, and PyPI trusted publishing).

## Install / upgrade

```bash
pipx install lf-toolbox
pipx upgrade lf-toolbox
```

Or with pip:

```bash
pip install -U lf-toolbox
```
