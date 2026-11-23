# mail-quota-report

Dovecot mailbox quota reporting, delivered by email.

> **This software comes without any warranty.** License: Apache-2.0
> (see [LICENSE](LICENSE)).

## Description

**mail-quota-report** is a small containerized Python 3 tool that
periodically collects the disk quota of every mailbox on a Dovecot mail
platform, enriches each row with directory data (display name, mail
addresses, department, company) from OpenLDAP or Active Directory, and
emails the result as a plain-text / HTML report through a local SMTP
relay. Capacity planning and mailbox housekeeping without logging into
any server.

```
[OpenLDAP / Active Directory] --enumerate + enrich--> +---------------------+
                                                      | mail-quota-report   |
[Dovecot] <--doveadm (TCP collector or SSH+exec)----- |      container      |
                                                      +----------+----------+
[Recipients] <--report-- [local SMTP relay] <-------------+
```

It targets **Dovecot 2.4** and runs as an unprivileged, port-less
container in Docker Swarm or docker compose.

## Features

**Collection**

- Per-mailbox quota via Dovecot's own doveadm protocol — the default
  **TCP collector** (`doveadm quota get -S host:port -u <mailbox>`)
  connects to the mail platform's doveadm server through the container
  network (Swarm/compose DNS), host-agnostic and without any SSH
  provisioning. Alternatively, an **SSH + docker exec fallback**
  (`collect.type: ssh_exec`) for environments where no doveadm listener
  can be configured.
- Shared `doveadm_password` handled strictly as a secret: passed to the
  doveadm client via a temporary 0600 config file — never in the process
  list, never as an environment variable.
- Per-mailbox errors never abort the run (unknown users, missing quota
  records); connection failures surface as a clear run error instead.

**Directory**

- Mailbox enumeration and enrichment from OpenLDAP or Active Directory:
  first/last name, primary mail address, aliases, department, company and
  a reference quota limit, all over configurable attribute mappings.
  Flexible match chains map mailboxes to directory entries (first hit
  wins); AD `proxyAddresses` `SMTP:`/`smtp:` prefixes are stripped
  automatically.
- Group include/exclude filters (OpenLDAP `member` resolution or AD
  `memberOf` attributes); `exclude` wins over `include`.
- Robust special cases without aborting: "no directory entry", "no quota
  record", zero limit (optionally hidden), ambiguous matches (logged
  without personal data).

**Report**

- Report mode `all`, or `below_threshold` — only mailboxes whose *free*
  space (limit − used) drops below a threshold, either as a percentage
  of the limit or as a fixed remaining size (`5GB`, `512MB`, …).
- Highlighting thresholds (default ≥ 80 % WARNING, ≥ 95 % CRITICAL),
  configurable sorting (`percent_desc`, `name_asc`, `limit_desc`) and
  per-tenant grouping with subtotals (`group_by_tenant`) for
  multi-tenant directory layouts.
- Output as `text`, `html` or `both` (multipart/alternative) with a
  configurable subject line and Jinja2 templates — the report wording
  lives entirely in the templates, so language is pure template
  configuration. The shipped set is German (`de`, `9,82 GB von 10,00 GB —
  98 %`); point `message.template_dir` at your own directory to adapt
  layout and language. Empty reports render a short "nothing to do"
  form.
- Privacy by design: only quota volumes and directory attributes are
  processed — no mail contents, no subjects, no headers, no traffic
  metadata, and nothing is persisted between runs (no trend history,
  no database).

**Operation**

- Built-in scheduler, no host cron needed: `SCHEDULE` accepts the
  aliases `weekly` (Mon 07:00), `daily` (07:00), `monthly` (1st 07:00),
  5-field cron expressions (`0 7 * * 1`) or plain intervals (`12h`,
  `30m`, `2d`, combined `45m12h`).
- Heartbeat health check: the scheduler writes a heartbeat file at start
  and after every real run; the container `HEALTHCHECK` verifies its age
  against `HEALTHCHECK_MAX_AGE_H` — scale it with your schedule
  (daily 26 h default, weekly 170, monthly 840).
- Operational modes: `--selftest` (config/secrets validation, optional
  network probes), `--dry-run` (full run, report to stdout, no mail),
  `--once` (full run with delivery outside the schedule).
- Fail-closed startup: a missing config file, unreadable or empty secret
  file, invalid `SCHEDULE` — the container refuses to start (exit 1).
  No default credentials, ever. Secrets are only ever read from files.
- Logging to stdout (plain or JSON via `LOG_FORMAT`), without personal
  data.

## Quick start

Five steps from clone to your first report:

1. **Build** the image:

   ```
   docker build -t mail-quota-report:0.2 .
   ```

2. **Create a config** — copy the commented reference and adapt it
   (Dovecot endpoint, directory, relay, recipients):

   ```
   mkdir -p config secrets
   cp config-example.yml config/config.yaml
   $EDITOR config/config.yaml
   ```

3. **Provide the secrets** (files, one value each — never in the YAML):

   ```
   echo 'cn=quota-report,ou=services,dc=example,dc=net' > secrets/mqr_ldap_bind_dn
   echo 'your-directory-bind-password'                  > secrets/mqr_ldap_bind_pw
   echo 'your-shared-doveadm-password'                  > secrets/mqr_doveadm_password
   # optional, when the SMTP relay requires AUTH:
   echo 'your-relay-password'                           > secrets/mqr_smtp_password
   ```

   Match the file names with the `*_FILE` paths in your config
   (`bind_dn_file`, `bind_pw_file`, `collect.tcp.password_file`,
   optional `smtp.password_file`).

4. **Verify the setup**:

   ```
   docker run --rm \
     -v $PWD/config:/etc/mail-quota-report:ro \
     -v $PWD/secrets:/run/secrets:ro \
     mail-quota-report:0.2 python3 -m mail_quota_report.cli --selftest

   docker run --rm \
     -v $PWD/config:/etc/mail-quota-report:ro \
     -v $PWD/secrets:/run/secrets:ro \
     mail-quota-report:0.2 python3 -m mail_quota_report.cli --dry-run
   ```

   `--selftest` validates config and secrets (and optionally LDAP/SMTP
   connectivity, see `selftest.network_probes`); `--dry-run` performs a
   complete collection and prints the report to stdout without sending
   mail.

5. **Run it** — a one-off report with delivery, or the scheduler:

   ```
   # one report now:
   docker run --rm \
     -v $PWD/config:/etc/mail-quota-report:ro \
     -v $PWD/secrets:/run/secrets:ro \
     mail-quota-report:0.2 python3 -m mail_quota_report.cli --once

   # long-running scheduler (report every Monday 07:00):
   docker run -d --name mail-quota-report \
     -v $PWD/config:/etc/mail-quota-report:ro \
     -v $PWD/secrets:/run/secrets:ro \
     -e SCHEDULE="0 7 * * 1" -e HEALTHCHECK_MAX_AGE_H=170 \
     mail-quota-report:0.2
   ```

The same image runs in Swarm services and compose files; a minimal
compose stack:

```yaml
services:
  mail-quota-report:
    image: mail-quota-report:0.2
    restart: unless-stopped
    environment:
      SCHEDULE: "0 7 * * 1"        # cron syntax; weekly interval: "7d" also works
      TZ: Europe/Vienna
      HEALTHCHECK_MAX_AGE_H: "170" # weekly schedule → scale the heartbeat window
    volumes:
      - ./config:/etc/mail-quota-report:ro
      - ./secrets:/run/secrets:ro
```

For updates, reference your image **pinned by digest** — the tag is
readable notation, the digest is the immutable reference:

```yaml
    image: "registry.example.net/your-team/mail-quota-report:0.2@sha256:<digest>"
```

## Configuration

Three separate layers:

1. **YAML per deployment** (mounted read-only, default path
   `/etc/mail-quota-report/config.yaml`; override via `--config` or
   `CONFIG_FILE`/`CONFIG_PATH`): deployment label, report settings
   (mode, threshold, subject, recipients, highlighting, sorting,
   grouping), message settings (format, templates), collection settings
   (collector type, endpoint, SSH fallback), directory settings (URI,
   bases, attribute mapping, match chain, groups) and SMTP relay.
   **[config-example.yml](config-example.yml) is the complete, commented
   reference** — including the three deployment patterns (OpenLDAP
   multi-tenant, Active Directory, SSH-based collection).
2. **ENV for runtime parameters**: `SCHEDULE`, `TZ`,
   `HEARTBEAT_FILE`, `HEALTHCHECK_MAX_AGE_H`, `LOG_LEVEL`,
   `LOG_FORMAT` (`plain`|`json`).
3. **Secrets as files only** (`*_FILE` pattern, mounted under
   `/run/secrets`): LDAP bind DN and password, shared doveadm password,
   optional SMTP password, optional SSH key for the fallback collector.
   Secrets never go into the YAML, the environment or the image — a
   missing, unreadable or empty secret file aborts startup (fail-closed).

The LDAP bind account should be a dedicated read-only service account.

## Configuration reference

Compact overview of all YAML options; the fully commented
[config-example.yml](config-example.yml) is the authoritative reference
(and shows the three deployment patterns). Required keys are marked;
everything else falls back to the built-in default.

| Block | Key | Values / default | Notes |
|---|---|---|---|
| `deployment` | — | string, **required** | Free-form site label; appears in subject and report header |
| `report` | `mode` | `all` \| `below_threshold` (default `all`) | `below_threshold` filters on *free* space |
| | `threshold` | `{type: percent, value: 0..100}` or `{type: fixed, value: "5GB"\|"512MB"\|kB-number}` | **Required with** `mode: below_threshold`; ignored otherwise |
| | `language` / `number_format` | `de` / `de_DE` | German in this version (wording lives in the templates) |
| | `subject` | string with `{placeholders}` | **required**; see [subject schema](#report-templates) below |
| | `recipients` | list of mail addresses | **required** |
| | `thresholds` | `{warn_percent: 80, critical_percent: 95}` | WARNING/CRITICAL highlighting |
| | `sort` | `percent_desc` \| `name_asc` \| `limit_desc` | |
| | `group_by_tenant` / `hide_zero_limit` | bool (default `false`) | per-tenant subtotals / hide limit-0 rows |
| `message` | `format` | `text` \| `html` \| `both` (default `both`) | `both` = multipart/alternative |
| | `template_dir` | directory path (optional) | built-in templates if unset; see below |
| | `templates` | `{subject: subject.txt, text: report.txt.j2, html: report.html.j2}` | per-purpose file names, defaults shown |
| `collect` | `type` | `doveadm_tcp` (default) \| `ssh_exec` | TCP collector or SSH+`docker exec` fallback |
| | `tcp.endpoint` | `host:port`, **required with `doveadm_tcp`** | Dovecot service DNS name in the container network |
| | `tcp.password_file` | path, **required with `doveadm_tcp`** | shared `doveadm_password` as a file, never inline |
| | `tcp.ssl` / `tcp.timeout` | `no` \| `ssl` \| `starttls` / seconds (30) | `no` only inside a dedicated isolated network |
| | `ssh.hosts` … | `hosts` (list), `container_filter`, `user`, `key_file`, `port` (22), `connect_timeout` (10), `timeout` (300), `known_hosts_file` | **required with `ssh_exec`**; `docker exec` is node-local — list every candidate host |
| | `enumeration` | `ldap` | mailbox list comes from the directory |
| `ldap` | `uri` | `ldap://…` / `ldaps://…`, **required** | StartTLS via `starttls: true` |
| | `bind_dn_file` / `bind_pw_file` | paths, **required** | dedicated read-only bind account, secrets as files |
| | `tenant_bases` | list of DNs, **required** | one per tenant (OpenLDAP) or single base (AD) |
| | `filter` | LDAP filter (default `(mail=*)`) | |
| | `attributes` | mapping report field → directory attribute; `username` and `mail_primary` **required** | fields: `username`, `first_name`, `last_name`, `mail_primary`, `mail_aliases`, `department`, `company`, `quota_limit` |
| | `match` | ordered list (default `[mail_primary, mail_aliases, username]`) | mailbox → entry match chain, first hit wins |
| | `groups` | `{match_mode: member_filter \| memberof_attr, include: [], exclude: []}` | `exclude` wins over `include`; empty `include` = all |
| `smtp` | `host` / `port` / `starttls` / `sender` | string / 25 / bool / address, `host`+`sender` **required** | local relay alias in the container network |
| | `password_file` | path (optional) | only if the relay requires AUTH |
| `selftest` | `network_probes` | bool (default `false`) | `--selftest` additionally probes LDAP bind and relay connectivity |

**Secrets follow the `*_FILE` pattern**: the YAML only ever contains
paths to files (`bind_dn_file`, `bind_pw_file`, `tcp.password_file`,
optional `smtp.password_file`, optional `ssh.key_file`). Values never
appear in the YAML, the environment or the image. A missing, unreadable
or empty secret file aborts startup — fail-closed, exit 1. There are no
default credentials.

**ENV overrides** (runtime parameters, not secrets):

| ENV | Default | Meaning |
|---|---|---|
| `SCHEDULE` | — (required for the scheduler) | `weekly`/`daily`/`monthly` aliases, 5-field cron, or intervals (`12h`, `30m`, `45m12h`, …) |
| `TZ` | `Europe/Vienna` | schedule reference timezone |
| `CONFIG_FILE` / `CONFIG_PATH` | `/etc/mail-quota-report/config.yaml` | config file location (`--config` wins) |
| `HEARTBEAT_FILE` | `/tmp/last-run` | heartbeat file for the health check |
| `HEALTHCHECK_MAX_AGE_H` | `26` | max heartbeat age in hours — scale with the schedule (weekly `170`, monthly `840`) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `plain` | `json` for structured logs |

## Report templates

The report wording lives entirely in Jinja2 templates — language, layout
and summary lines are pure template matter. Three files ship with the
image (`templates/` in the repo, `/app/templates` in the container):

| Purpose | Built-in file | Rendered as |
|---|---|---|
| `subject` | `subject.txt` | subject line (placeholders only, no layout) |
| `text` | `report.txt.j2` | plain-text body |
| `html` | `report.html.j2` | HTML body (inline CSS, no external resources) |

**Overrides.** Mount your own directory into the container (read-only)
and point `message.template_dir` at it; `message.templates` maps each
purpose to a file name relative to that directory — override all three
or just the ones you need:

```yaml
message:
  format: both
  template_dir: /etc/mail-quota-report/templates   # your mount
  templates:                                        # defaults shown
    subject: subject.txt
    text: report.txt.j2
    html: report.html.j2
```

Selection logic: each purpose is looked up by its mapped name in
`template_dir`; if a file is missing there (and `template_dir` differs
from the built-in directory), the renderer logs a warning and falls back
to the built-in templates — a typo degrades gracefully instead of
failing the run. Autoescaping applies to files ending in `.html.j2`
only (directory data cannot inject HTML); text templates render as
plain text. Jinja `StrictUndefined` is set: referencing an unknown
placeholder fails the render instead of silently producing blanks
(fail-closed). `trim_blocks`/`lstrip_blocks` keep template output tidy.

**Row placeholders** (`{{ row.<key> }}` inside `{% for row in rows %}`;
`rows` is the flat mailbox list, `groups` the per-tenant mapping
`tenant → rows` when `report.group_by_tenant` is on):

| Key | Content |
|---|---|
| `row.mailbox` | mailbox name (quota target) |
| `row.name` | display name (falls back to the mailbox) |
| `row.mail_primary` / `row.mail_aliases` | primary address / alias list |
| `row.department` / `row.company` | directory attributes |
| `row.used` / `row.limit` / `row.free` | sizes, pre-formatted (e.g. `9,82 GB`) |
| `row.percent` | exact usage percent (one decimal, `—` when limit is 0) |
| `row.status` | status label: `OK` / `WARNUNG` / `KRITISCH` / `kein LDAP-Eintrag` / `kein Quota-Datensatz` / `0 (Sonderfall)` |
| `row.status_class` | matching CSS class: `ok` / `warn` / `critical` / `no-ldap` / `no-quota` / `limit0` |
| `row.tenant` / `row.error` | tenant name / per-mailbox error text |

Templates may also use the filters `kib` (size formatting) and
`percent` (percent formatting) on raw numbers, and `groups.items()` for
tenant sections (see the built-in templates for idioms).

**Summary placeholders** (`{{ summary.<key> }}`):

| Key | Content |
|---|---|
| `summary.deployment` / `summary.date` | deployment label / report date |
| `summary.n` / `summary.warn` / `summary.crit` | mailboxes checked / WARNING / CRITICAL counts |
| `summary.no_ldap` / `summary.no_quota` | mailboxes without a directory entry / without a quota record |
| `summary.warn_percent` / `summary.critical_percent` | configured highlight thresholds |
| `summary.mode` / `summary.threshold_desc` | `all` / `below_threshold` (+ threshold description) |
| `summary.version` | tool version (report footer) |

Renderer context per body template: `rows`, `groups`, `summary`,
`errors` (list of per-mailbox collector errors, `{mailbox}: {error}`).

**Subject schema** (`report.subject`): plain `str.format` placeholders
— deliberately not Jinja, so the config stays simple; unknown
placeholders fail the run (fail-closed):

`{deployment}` `{date}` `{n}` `{warn}` `{crit}` `{no_ldap}` `{no_quota}`

## Operating modes and exit codes

| Mode | Effect |
|---|---|
| `--selftest` | Validate config and secrets; optionally probe LDAP/SMTP. No mail. |
| `--dry-run` | Full collection and rendering to stdout. No mail, no heartbeat. |
| `--once` | One full run with mail delivery, outside the schedule. |
| *(default)* | Scheduler loop according to `SCHEDULE`. |

Exit codes: `0` success · `1` configuration failure (fail-closed: bad
config, missing secrets, invalid schedule, failed selftest) · `2` run
failure (directory/collector/relay unreachable). Heartbeats are written
at scheduler start and after every real run (including failed delivery
attempts — the health check tracks scheduler liveness, not mail
delivery); `--dry-run`/`--selftest` deliberately never write one.

## Dovecot server setup (doveadm TCP collector)

The TCP collector talks to Dovecot's own doveadm server. On the mail
platform:

```
service doveadm {
  inet_listener doveadm {
    port = 4242
  }
}
doveadm_password = <shared secret>   # required; the SAME value on server and client
```

- Pick any free port (4242 is just an example) and set the same shared
  secret as `collect.tcp.password_file` on the report side.
- TLS via `doveadm_ssl = no|ssl|starttls` (client side: `collect.tcp.ssl`).
  Default `no` is fine **only** inside a dedicated container network;
  use `ssl`/`starttls` when the path is not isolated.
- The doveadm listener is an **administration protocol** — whoever
  reaches the port has doveadm rights. Keep it on a dedicated
  overlay/network shared only by Dovecot and the report container, do
  not publish it outside, and prefer TLS when in doubt.
- The report image ships a minimal client-side Dovecot config
  (`conf/dovecot.conf` in the repo) so the doveadm client registers the
  `quota` subcommand — nothing to configure on the client beyond the
  endpoint and the shared secret.

**SSH + docker exec fallback** (`collect.type: ssh_exec`): for
environments without a doveadm listener — the report connects to a list
of hosts over SSH and runs doveadm inside the Dovecot container via
`docker exec`. Note that `docker exec` is node-local: the report must
reach a node the container actually runs on (that placement coupling is
why the TCP collector is the default). Requires a dedicated SSH key
(`collect.ssh.key_file`) and the same config-validated fail-closed
guarantees.

## Deployment notes

- **Dovecot 2.4** with the quota plugin enabled is the reference
  platform.
- Runs as an unprivileged fixed user (UID 1000), no exposed ports and no
  persistent storage — a pure client. Recommended hardening in your
  stack file: `no-new-privileges`, modest CPU/memory limits (the tool is
  a periodic batch job, not a hot path).
- **Dovecot + report should share a dedicated network** in Swarm or
  compose; the report reaches the doveadm listener and the relay by
  container DNS names.
- Directory flavors: **OpenLDAP multi-tenant** (per-tenant search bases,
  tenant as part of the DN — `group_by_tenant` yields per-tenant
  subtotals) or **Active Directory** (single base, `proxyAddresses`
  aliases, `memberOf` group filters) — both patterns are commented in
  [config-example.yml](config-example.yml). If the directory server is
  not resolvable through container DNS, add a host mapping per your
  platform's means.
- Deliver the report to a small, accountable recipient list: it contains
  per-mailbox usage and directory metadata for your whole user base.

## Hardening / read-only operation

The container **can run with a read-only root filesystem**. It only
ever writes to `/tmp`:

- the scheduler heartbeat file (`/tmp/last-run`, override with
  `HEARTBEAT_FILE`),
- a temporary doveadm client config per collection run (created with
  `mkstemp`, mode 0600, holding the shared doveadm password — deleted
  after the run; the secret is never passed as an argument or
  environment variable),
- the SSH control socket (`/tmp/mqr-ssh-*`) for the `ssh_exec` fallback.

Config, templates, secrets and certificates are only ever read. The
process runs as the unprivileged user `mqr` (UID 1000) and the image
exposes no ports and has no persistent storage.

docker compose:

```yaml
services:
  mail-quota-report:
    image: mail-quota-report:0.2
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    environment:
      SCHEDULE: "0 7 * * 1"
      HEALTHCHECK_MAX_AGE_H: "170"
    volumes:
      - ./config:/etc/mail-quota-report:ro
      - ./secrets:/run/secrets:ro
```

Docker Swarm (stack file):

```yaml
version: "3.8"
services:
  mail-quota-report:
    image: "registry.example.net/your-team/mail-quota-report:0.2@sha256:<digest>"
    read_only: true
    tmpfs:
      - /tmp
    environment:
      SCHEDULE: "0 7 * * 1"
      HEALTHCHECK_MAX_AGE_H: "170"
    volumes:
      - type: bind
        source: /srv/mail-quota-report/config
        target: /etc/mail-quota-report
        read_only: true
      - type: bind
        source: /srv/mail-quota-report/secrets
        target: /run/secrets
        read_only: true
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 64M
```

**Health check note:** the built-in health check verifies the heartbeat
file's age against `HEALTHCHECK_MAX_AGE_H`. The scheduler writes the
heartbeat at start and after every real run, so the check turns healthy
within seconds of scheduler start — the image's HEALTHCHECK grants a
5-minute start period for exactly that ramp-up (`--start-period`). If
you override the health check in your compose/stack file, keep a
`start_period` that covers the container start→scheduler start gap;
and since `/tmp` is a tmpfs, a container restart loses the previous
heartbeat until the scheduler writes a fresh one.

## Development and CI

- Test gate: `python3 -m pytest tests/` — see
  [CONTRIBUTING.md](CONTRIBUTING.md) for requirements and conventions
  (Python 3 stdlib + Debian distribution packages only, no pip in the
  product image, no secrets/customer data policy).
- `scripts/check_image_consistency.py` cross-checks that every
  third-party import is covered by the Dockerfile package set.
- The [Jenkinsfile](Jenkinsfile) runs the same test gate, a fail-closed
  smoke test (selftest, refused start without config, doveadm subcommand
  registration), builds an SBOM (CycloneDX) per run and pushes the
  image.

## Security

- Fail-closed configuration, no default credentials, secrets exclusively
  from file mounts, unprivileged container, no ports, no persistent
  data. An SBOM is produced on every CI build.
- Responsible disclosure and support handling:
  see [SECURITY.md](SECURITY.md).

## Versioning

Releases are tagged `vX.Y.Z` (current: **v0.2.0**). The Python module
constant and the OCI image label carry `MAJOR.MINOR` (`0.2`); the report
footer shows the same version.

## Support

Best-effort community support, no SLA — this is open source.
Commercial support: visit [https://www.universalnet.at](https://www.universalnet.at)
or write to [office@universalnet.at](mailto:office@universalnet.at).

## License

Apache-2.0 — see [LICENSE](LICENSE).
