# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] — 2026-11-23

### Changed

- Public release preparation: complete English documentation (README as
  a user guide), neutralized example configuration with documented
  deployment patterns (OpenLDAP multi-tenant, Active Directory, SSH
  collection fallback), `CONTRIBUTING.md` and this changelog.
- Dovecot quota output parsing extended for the Dovecot 2.4 table
  formatter (quota name prefix), alongside the flow format.
- Report version handling now consistent: Python module constant, image
  label and report footer all carry 0.2.

### Fixed

- Scheduler loop no longer fires an extra run after every intermediate
  hourly sleep while parking until the due time — the remaining time is
  re-measured against the same target after each sleep, so a run happens
  exactly at the scheduled time (previously a run could occur exactly
  one hour after scheduler start), and a single run is delivered even if
  a sleep skipped across the due time.
- Heartbeat file is written at scheduler start, before the first run is
  due — previously a fresh container could be killed by the container
  health check (heartbeat too old) before its first scheduled run, as
  the first run may be up to a day away (restart loop).
- Tenant grouping now effective in rendered reports: the grouping data
  structure is passed to the templates instead of the raw boolean flag,
  per-tenant sections with subtotals render as documented.
- doveadm client environment inheritance: configuration file variables
  of the report (`CONFIG_FILE`/`CONFIG_PATH`) are no longer passed to
  the doveadm subprocess, which previously tried to parse the report
  YAML as its own configuration; the shared doveadm password is handed
  to the client through a temporary 0600 configuration file instead of
  an environment variable (which the doveadm client ignores).

## [0.1.0] — 2026-11-06

### Added

- Initial release: periodic Dovecot mailbox quota reporting by email.
- Quota collection via the doveadm protocol — TCP collector against
  Dovecot's doveadm server (host-agnostic, container DNS) with an
  SSH + docker exec fallback collector.
- Mailbox enumeration and enrichment from OpenLDAP / Active Directory:
  configurable attribute mappings, match chains, group include/exclude
  filters, alias handling (`mailAddresses`, `proxyAddresses` SMTP
  prefix stripping).
- Report modes `all` and `below_threshold` (percentage or fixed
  remaining size), warning/critical highlighting thresholds, sorting
  and per-tenant grouping with subtotals; text, HTML or
  multipart/alternative delivery with configurable subject and Jinja2
  templates.
- Built-in scheduler (cron syntax, aliases, interval form), heartbeat
  health check, selftest / dry-run / once operating modes, fail-closed
  configuration validation, secrets exclusively from files (`*_FILE`),
  unprivileged container (Debian base, distribution packages only).
