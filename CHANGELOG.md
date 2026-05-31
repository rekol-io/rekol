# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims to
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- Open-source scaffolding: Apache-2.0 license, README, CONTRIBUTING, Code of
  Conduct, issue/PR templates.
- Quality gate: Ruff (lint+format), mypy, pre-commit, GitHub Actions CI.

### Genericization & onboarding (Plan 2)
- Data-level names branded REKOL (`rekol.config.yaml`, `REKOL.md`, `rekol`
  skill) with back-compat reads of the legacy names (`memory.config.yaml`,
  `MEMORY.md`, `/memory` shim).
- `scope: private` frontmatter field reserved (parsed but not validated in v0.1; any value is accepted so existing files are never dropped from the index).
- Legacy migration is now opt-in (`install.sh --migrate`).
- `rekol import` gained `--include`/`--exclude` for file-type selection.
- Sync reframed as local-first; `REKOL_HOME` is any folder you own.
- New `rekol init` interactive onboarding (transcript indexing, corpus import,
  cloud-sync detection, opt-in migration).

### Changed
- Rebranded the project from `memory-tools` to **REKOL**: the Python package is
  now `rekol`, and the formerly separate `memory-*` console scripts are unified
  under a single `rekol` command (`rekol search`, `rekol index`, `rekol capture`,
  `rekol import`, etc.). Docs, hooks, skill, and templates are rebranded to match.
- The data-directory env var is now `REKOL_HOME`. `MEMORY_HOME` is still accepted
  as a fallback, so existing installs keep working without changes.

### Fixed
- `rekol search` crash on queries containing FTS5 operator characters
  (e.g. hyphens) — queries are now sanitized into safe FTS5 phrases.
