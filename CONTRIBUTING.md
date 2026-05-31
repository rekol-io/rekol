# Contributing to REKOL

Thanks for your interest! REKOL is a small project; the flow is simple.

## Development setup
```bash
git clone https://github.com/<you>/rekol   # your fork
cd rekol
python3.11 -m venv .venv-dev && . .venv-dev/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## The loop
1. Fork the repo and create a branch off `main`.
2. Make your change. Keep functions small, names descriptive, and add
   docstrings — the Ruff config enforces this.
3. Add or update tests. We use TDD: a change to behavior should come with a
   test that fails before and passes after.
4. Run the gate locally:
   ```bash
   ruff check . && ruff format --check . && mypy src/rekol && pytest -q
   ```
   (`pre-commit` runs ruff + mypy automatically on `git commit`.)
5. Push to your fork and open a Pull Request against `main`.
6. CI must be green and a maintainer must approve before merge.

## Commit messages
Conventional commits: `type: short summary` (`fix`, `feat`, `docs`, `refactor`,
`test`, `chore`, `ci`). Explain *why* in the body when it isn't obvious.

## Licensing of contributions
By submitting a contribution, you agree it is licensed under the project's
[Apache-2.0](./LICENSE) license (inbound = outbound). No separate CLA is required.

## Code of Conduct
This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md).
