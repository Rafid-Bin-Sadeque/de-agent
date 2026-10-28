# Vendored dependencies

- **sqlglot 30.25.0** (https://github.com/tobymao/sqlglot), MIT license (see `SQLGLOT_LICENSE`).
  Pure-python install, no compiled extras. Needed so the guard hooks run with any bare `python3` (3.9+).

Update:

```bash
rm -rf scripts/_vendor/sqlglot scripts/_vendor/sqlglot-*.dist-info
cli/.venv/bin/pip install --no-deps --target scripts/_vendor sqlglot==<version>
rm -rf scripts/_vendor/bin; find scripts/_vendor -name __pycache__ -prune -exec rm -rf {} +
```

Then refresh the version here and in `NOTICE`, and run the full test suite.
