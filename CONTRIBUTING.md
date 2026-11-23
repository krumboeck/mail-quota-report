# Contributing

Thanks for your interest in improving **mail-quota-report**!

## Build & test requirements

- **Python 3.11+** (the project targets the Python 3 of Debian stable;
  the container image runs the Debian distribution Python).
- **Debian distribution packages** — the runtime deliberately uses only
  the Python standard library plus Debian-packaged modules:
  `python3-yaml`, `python3-jinja2`, `python3-ldap`; tests need
  `python3-pytest`. On Debian/Ubuntu:

  ```
  sudo apt install python3-yaml python3-jinja2 python3-ldap python3-pytest
  ```

- **No pip.** There is no `requirements.txt` and the product image
  installs nothing via pip — keep it that way (see conventions below).

## Test gate (before every commit)

Run the full suite from the repository root:

```
python3 -m pytest tests/ -v
```

**Rule: commits only with a green gate.** A pull request is only
mergeable when all tests pass — report the numbers of your final run
(`N passed`) in the PR description. New features and bug fixes come
with tests that prove them.

Additionally, `scripts/check_image_consistency.py` cross-checks that
every third-party import in `src/` is covered by the Dockerfile package
set (the same test stage runs it in CI). Run it when you add imports:

```
python3 scripts/check_image_consistency.py
```

Note: adding a third-party import means adding the Debian package to
**both** the `Dockerfile` and the Jenkins test stage — until then the
consistency check fails the gate. Stdlib-only solutions are preferred
where reasonable.

## Pull request flow

1. Branch off `main` (e.g. `fix/…`, `feat/…`, anything consistent).
2. One logical change per PR; keep the diff focused (no drive-by
   reformatting of unrelated files).
3. Make sure the test gate is green, including for any tests you
   touched, and attach the numbers to your PR.
4. Open a pull request against `main` describing **what** changed and
   **why**.

## Project conventions

- Documentation and **comments are English**; commit messages follow
  conventional-commits style (`fix:`, `feat:`, `docs:`, …).
- The runtime is **Python 3 stdlib + Debian distribution packages** —
  never add a pip dependency or vendor code into the image; if you need
  a library, use the Debian package and extend
  `scripts/check_image_consistency.py` accordingly.
- Version bumps move **three** places together: `__version__` in
  `src/mail_quota_report/__init__.py`, the `VERSION` build argument
  default in the `Dockerfile`, and the version literal passed to the
  report templates (`src/mail_quota_report/template.py`); releases are
  tagged `vX.Y.Z` and documented in `CHANGELOG.md`
  (Keep-a-Changelog style). Tests in `tests/test_placeholder.py`
  pin the module constant — update them with the bump.
- Fail-closed is a core property: missing configuration, unreadable or
  empty secret files and invalid schedules must abort startup — never
  add a fallback default that silently keeps the container running.
- Secrets are read exclusively from files (`*_FILE` pattern); they are
  never logged, never passed as command-line arguments or environment
  variables.

## No secrets, no customer data

- **Never commit secrets**: no passwords, bind credentials, shared
  doveadm passwords, API tokens, private keys or connection strings
  with credentials — not even in comments, test fixtures or example
  configs. Use placeholder values (`verysecret`) and file-path
  references like the shipped `config-example.yml` does.
- **Never commit customer or production data**: no real mailbox names,
  directory entries, person names, domains, IP segments, hostnames or
  report samples from any deployment. Tests use synthetic data only —
  freely invented values on the reserved `example.com`/`example.net`/
  `example.org` domains (RFC 2606) — never production data.
- Example values in docs, configs and tests are **generic**: invented
  domains (`dovecot`, `ldap.example.net`, `dc1.example.com`), reserved
  namespaces and obvious placeholders. If a configuration value would
  identify a real deployment, it does not belong in the repository.
