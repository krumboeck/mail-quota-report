"""config — Konfiguration laden, validieren, Secrets lesen (fail-closed).

Aufbau:
- YAML je Deployment; Pfad via --config, ENV CONFIG_FILE/CONFIG_PATH oder
  Default /etc/mail-quota-report/config.yaml
- ENV-Overrides nur für Laufzeitparameter (SCHEDULE, TZ, LOG_LEVEL,
  LOG_FORMAT — siehe scheduler.py/cli.py); Secrets ausschließlich aus
  Dateien (*_FILE-Muster) — nie ENV, nie Image-Layer
- Fail-closed: fehlende Datei, unlesbares Secret, unbekannter/leerer
  Pflichtwert → ConfigError mit Klartext-Diagnose → Exit 1 im CLI

Validierungsregeln der neuen Features (06.11.2026):
- report.mode: all | below_threshold
- report.threshold: {type: percent, value: 0..100} oder
  {type: fixed, value: "5GB" | 512000 (kB-Einheiten)} — nur bei
  below_threshold Pflicht
- message.format: text | html | both
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "/etc/mail-quota-report/config.yaml"

#: Größenkonvention (deckt sich mit der Report-Anzeige):
#: dovecot STORAGE/mailQuota zählen in kB-Einheiten; MB = 1.000 kB,
#: GB = 1.000.000 kB. Umrechnung bewusst decimal-styling, damit der
#: Schwellwert "5GB" exakt dem entspricht, was der Report als "5,00 GB"
#: anzeigt.
KIB_PER_MB = 1_000
KIB_PER_GB = 1_000_000


class ConfigError(Exception):
    """Fail-closed: Konfiguration oder Secrets unbrauchbar (→ Exit 1)."""


def resolve_config_path(explicit: str | None = None) -> str:
    """Config-Pfad: --config schlägt ENV CONFIG_FILE/CONFIG_PATH schlägt Default."""
    if explicit:
        return explicit
    for var in ("CONFIG_FILE", "CONFIG_PATH"):
        value = os.environ.get(var)
        if value:
            return value
    return DEFAULT_CONFIG_PATH


def _as_bool(value: Any, path: str) -> bool:
    """YAML-Strings on/off/yes/no true/false robust in bool überführen."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{path}: boolescher Wert erwartet (on/off/yes/no/true/false), got: {value!r}")


def _require_mapping(data: dict, key: str, errors: list[str]) -> dict:
    section = data.get(key)
    if not isinstance(section, dict):
        errors.append(f"{key}: Abschnitt fehlt")
        return {}
    return section


def _require_str(section: dict, key: str, path: str, errors: list[str]) -> str | None:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: Pflichtfeld fehlt oder leer")
        return None
    return value.strip()


def _require_str_list(section: dict, key: str, path: str, errors: list[str],
                      min_len: int = 1) -> list[str] | None:
    value = section.get(key)
    if not isinstance(value, list) or len(value) < min_len or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        errors.append(f"{path}: Liste mit mindestens {min_len} Einträgen erwartet")
        return None
    return [v.strip() for v in value]


def read_secret_file(path: str, what: str) -> str:
    """Secret-Datei lesen (Inhalt gestrippt); fehlend/unlesbar → ConfigError."""
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Secret-Datei fehlt: {path} ({what})") from None
    except OSError as e:
        raise ConfigError(f"Secret-Datei nicht lesbar: {path} ({what}): {e}") from None
    content = content.strip()
    if not content:
        raise ConfigError(f"Secret-Datei ist leer: {path} ({what})")
    return content


def check_readable_file(path: str, what: str) -> None:
    """Existenz/Lesbarkeit prüfen (z. B. SSH-Key), Inhalt NICHT laden."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Datei fehlt: {path} ({what})")
    if not os.access(p, os.R_OK):
        raise ConfigError(f"Datei nicht lesbar: {path} ({what})")


# ---------------------------------------------------------------------------
# Datenklassen (validierte Konfiguration)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThresholdConfig:
    """Schwellwert für report.mode: below_threshold (Filter auf FREIEN Speicher)."""

    type: str          # percent | fixed
    value: float      # percent: 0..100 | fixed: kB-Einheiten


@dataclass(frozen=True)
class ReportConfig:
    mode: str
    threshold: ThresholdConfig | None
    language: str
    number_format: str
    subject: str
    recipients: tuple[str, ...]
    warn_percent: float
    critical_percent: float
    sort: str
    group_by_tenant: bool
    hide_zero_limit: bool


@dataclass(frozen=True)
class MessageConfig:
    format: str                     # text | html | both
    template_dir: str | None        # None = nur mitgelieferte Standard-Templates
    templates: dict[str, str]       # subject/text/html -> Dateiname


@dataclass(frozen=True)
class SSHConfig:
    hosts: tuple[str, ...]
    container_filter: str
    user: str
    key_file: str
    port: int = 22
    connect_timeout: int = 10
    timeout: int = 300
    known_hosts_file: str | None = None   # None → ssh -o StrictHostKeyChecking=accept-new


@dataclass(frozen=True)
class DoveadmTcpConfig:
    """doveadm-TCP-Collector (host-agnostisch, Architektur-Entscheidung
    2026-11-06): Verbindung per Swarm/compose-DNS-Namen statt docker exec
    auf einem bestimmten Host.

    Protokoll-Syntax (verifiziert, s. README „Architektur-Entscheidung"):
    - Client: doveadm quota get -S <hostname>:<port> -u <mailbox>
      (doveadm-quota(1): "-S socket_path ... hostname and port (hostname:port)
      ... connect a remote host via a TCP socket")
    - Server: service doveadm { inet_listener doveadm { port = <port> } }
      + doveadm_password (Pflicht, gilt für Server UND Client)
    - TLS: doveadm_ssl = no|ssl|starttls (Default no — Hinweis:
      unverschlüsselt nur innerhalb eines isolierten Overlays betreiben)
    """

    endpoint: str                    # "host:port" — z. B. mail-shared_imap:4242
    password: str | None             # doveadm_password (aus Secret-Datei, nie loggen)
    password_file: str | None         # Referenz für Doku/Selftest
    ssl: str = "no"                  # no | ssl | starttls
    timeout: int = 30


@dataclass(frozen=True)
class CollectConfig:
    type: str            # doveadm_tcp (Default) | ssh_exec (Fallback)
    enumeration: str     # ldap
    tcp: DoveadmTcpConfig | None
    ssh: SSHConfig | None


@dataclass(frozen=True)
class LDAPConfig:
    uri: str
    starttls: bool
    bind_dn: str          # aus Secret-Datei aufgelöst (nie loggen!)
    bind_pw: str          # aus Secret-Datei aufgelöst (nie loggen!)
    tenant_bases: tuple[str, ...]
    search_filter: str
    attributes: dict[str, str]
    match: tuple[str, ...]
    groups_mode: str      # member_filter | memberof_attr
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    starttls: bool
    sender: str
    password: str | None    # None = kein AUTH; sonst aus Secret-Datei


@dataclass(frozen=True)
class Config:
    deployment: str
    report: ReportConfig
    message: MessageConfig
    collect: CollectConfig
    ldap: LDAPConfig
    smtp: SMTPConfig
    selftest_network_probes: bool = False


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------

_ALLOWED_SORT = ("percent_desc", "name_asc", "limit_desc")
_MATCH_FIELDS = ("username", "mail_primary", "mail_aliases", "first_name",
                 "last_name", "department", "company", "quota_limit")


def _validate_report(section: dict, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mode = section.get("mode", "all")
    if mode not in ("all", "below_threshold"):
        errors.append(f"report.mode: 'all' oder 'below_threshold' erwartet, got {mode!r}")
    out["mode"] = mode

    threshold = None
    if mode == "below_threshold":
        th = section.get("threshold")
        if not isinstance(th, dict):
            errors.append("report.threshold: Pflicht bei mode: below_threshold "
                          "{type: percent|fixed, value: ...}")
        else:
            ttype = th.get("type")
            if ttype not in ("percent", "fixed"):
                errors.append(f"report.threshold.type: 'percent' oder 'fixed' erwartet, got {ttype!r}")
            else:
                from .filter import parse_threshold  # lokal: Zyklus vermeiden
                try:
                    threshold = parse_threshold(th)
                except ValueError as e:
                    errors.append(f"report.threshold: {e}")
    out["threshold"] = threshold

    language = section.get("language", "de")
    if language != "de":
        errors.append(f"report.language: 'de' implementiert, got {language!r}")
    out["language"] = language
    number_format = section.get("number_format", "de_DE")
    if number_format != "de_DE":
        errors.append(f"report.number_format: 'de_DE' implementiert, got {number_format!r}")
    out["number_format"] = number_format

    subject = _require_str(section, "subject", "report.subject", errors)
    out["subject"] = subject or ""

    recipients = _require_str_list(section, "recipients", "report.recipients", errors)
    if recipients and not all("@" in r for r in recipients):
        errors.append("report.recipients: jeder Eintrag muss eine Mail-Adresse sein")
    out["recipients"] = tuple(recipients or ())

    thresholds = section.get("thresholds") or {}
    try:
        warn = float(thresholds.get("warn_percent", 80))
        critical = float(thresholds.get("critical_percent", 95))
    except (TypeError, ValueError):
        errors.append("report.thresholds: warn_percent/critical_percent müssen Zahlen sein")
        warn, critical = 80.0, 95.0
    if not (0 <= warn <= 100 and 0 <= critical <= 100):
        errors.append("report.thresholds: warn_percent/critical_percent in 0..100 erwartet")
    elif warn >= critical:
        errors.append("report.thresholds: warn_percent muss kleiner als critical_percent sein")
    out["warn_percent"] = warn
    out["critical_percent"] = critical

    sort = section.get("sort", "percent_desc")
    if sort not in _ALLOWED_SORT:
        errors.append(f"report.sort: einer von {_ALLOWED_SORT} erwartet, got {sort!r}")
    out["sort"] = sort

    try:
        out["group_by_tenant"] = _as_bool(section.get("group_by_tenant", False),
                                          "report.group_by_tenant")
    except ConfigError as e:
        errors.append(str(e))
        out["group_by_tenant"] = False
    try:
        out["hide_zero_limit"] = _as_bool(section.get("hide_zero_limit", False),
                                          "report.hide_zero_limit")
    except ConfigError as e:
        errors.append(str(e))
        out["hide_zero_limit"] = False
    return out


def _validate_message(section: dict, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fmt = section.get("format", "both")
    if fmt not in ("text", "html", "both"):
        errors.append(f"message.format: 'text', 'html' oder 'both' erwartet, got {fmt!r}")
    out["format"] = fmt

    template_dir = section.get("template_dir")
    if template_dir is not None and not isinstance(template_dir, str):
        errors.append("message.template_dir: Pfad als String erwartet")
        template_dir = None
    out["template_dir"] = template_dir

    templates = section.get("templates") or {}
    if not isinstance(templates, dict):
        errors.append("message.templates: Mapping Zweck -> Dateiname erwartet")
        templates = {}
    defaults = {"subject": "subject.txt", "text": "report.txt.j2",
                "html": "report.html.j2"}
    merged = dict(defaults)
    for key in ("subject", "text", "html"):
        if key in templates:
            if not isinstance(templates[key], str) or not templates[key].strip():
                errors.append(f"message.templates.{key}: Dateiname als String erwartet")
            else:
                merged[key] = templates[key].strip()
    out["templates"] = merged
    return out


_ENDPOINT_RE = None  # unten initialisiert (re-Import-Sicherheit)


def _validate_endpoint(endpoint: str, path: str) -> str:
    """host:port-Endpoint validieren (leerer Host, Port 1..65535)."""
    import re as _re

    global _ENDPOINT_RE
    if _ENDPOINT_RE is None:
        _ENDPOINT_RE = _re.compile(r"^(?P<host>[A-Za-z0-9._-]+):(?P<port>\d{1,5})$")
    m = _ENDPOINT_RE.match(endpoint.strip())
    if not m:
        raise ConfigError(f"{path}: 'host:port' erwartet, got {endpoint!r} "
                          "(z. B. mail-shared_imap:4242)")
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise ConfigError(f"{path}: Port {port} außerhalb 1..65535")
    return f"{m.group('host')}:{port}"


def _validate_collect(section: dict, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ctype = section.get("type", "doveadm_tcp")
    if ctype not in ("doveadm_tcp", "ssh_exec"):
        errors.append(f"collect.type: 'doveadm_tcp' oder 'ssh_exec' erwartet, "
                      f"got {ctype!r}")
    out["type"] = ctype
    enumeration = section.get("enumeration", "ldap")
    if enumeration != "ldap":
        errors.append(f"collect.enumeration: 'ldap' implementiert (config/doveadm_a "
                      f"folgen später), got {enumeration!r}")
    out["enumeration"] = enumeration

    # --- doveadm_tcp (Default): host-agnostisch per Swarm/compose-DNS ---
    tcp_section = section.get("tcp") or {}
    if not isinstance(tcp_section, dict):
        # Shorthand: collect.tcp als String "host:port" erlaubt
        if isinstance(section.get("tcp"), str):
            tcp_section = {"endpoint": section["tcp"]}
        else:
            tcp_section = {}
    tcp: dict[str, Any] = {}
    if ctype == "doveadm_tcp":
        endpoint = tcp_section.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            errors.append("collect.tcp.endpoint: Pflicht bei type: doveadm_tcp "
                          "('host:port', z. B. mail-shared_imap:4242)")
        else:
            try:
                tcp["endpoint"] = _validate_endpoint(endpoint, "collect.tcp.endpoint")
            except ConfigError as e:
                errors.append(str(e))
                tcp["endpoint"] = endpoint.strip()
        pw_file = tcp_section.get("password_file")
        if pw_file is not None and not isinstance(pw_file, str):
            errors.append("collect.tcp.password_file: Pfad als String erwartet")
        tcp["password_file"] = pw_file if isinstance(pw_file, str) else None
        ssl = tcp_section.get("ssl", "no")
        # YAML 1.1-Falle: `ssl: no` parst als Boolean False (wie on/off/
        # yes/no) — normalisieren; True ist kein gültiger Modus.
        if ssl is False:
            ssl = "no"
        if ssl not in ("no", "ssl", "starttls"):
            errors.append(f"collect.tcp.ssl: 'no', 'ssl' oder 'starttls' erwartet, "
                          f"got {ssl!r}")
        tcp["ssl"] = ssl
        try:
            tcp["timeout"] = int(tcp_section.get("timeout", 30))
        except (TypeError, ValueError):
            errors.append("collect.tcp.timeout: Zahl erwartet")
            tcp["timeout"] = 30
    out["tcp"] = tcp

    # --- ssh_exec (Fallback): SSH + docker exec, unverändert ---
    ssh_section = section.get("ssh") or {}
    if not isinstance(ssh_section, dict):
        if ctype == "ssh_exec":
            errors.append("collect.ssh: Abschnitt fehlt (Pflicht bei type: ssh_exec)")
        ssh_section = {}
    ssh_present = bool(ssh_section)
    ssh: dict[str, Any] = {}
    if ctype == "ssh_exec":
        hosts = _require_str_list(ssh_section, "hosts", "collect.ssh.hosts", errors)
        ssh = {
            "hosts": tuple(hosts or ()),
            "container_filter": _require_str(ssh_section, "container_filter",
                                              "collect.ssh.container_filter", errors),
            "user": _require_str(ssh_section, "user", "collect.ssh.user", errors) or "root",
            "key_file": _require_str(ssh_section, "key_file", "collect.ssh.key_file", errors),
            "port": int(ssh_section.get("port", 22)),
            "connect_timeout": int(ssh_section.get("connect_timeout", 10)),
            "timeout": int(ssh_section.get("timeout", 300)),
            "known_hosts_file": ssh_section.get("known_hosts_file"),
        }
    elif ssh_present and ssh_section.get("key_file"):
        # Fallback-Block vorhanden aber nicht aktiv: nur Key-Lesbarkeit prüfen
        key_file = ssh_section.get("key_file")
        if isinstance(key_file, str) and key_file.strip():
            ssh = {"hosts": tuple(ssh_section.get("hosts") or ()),
                   "container_filter": ssh_section.get("container_filter") or "",
                   "user": ssh_section.get("user") or "root",
                   "key_file": key_file.strip(),
                   "port": int(ssh_section.get("port", 22)),
                   "connect_timeout": int(ssh_section.get("connect_timeout", 10)),
                   "timeout": int(ssh_section.get("timeout", 300)),
                   "known_hosts_file": ssh_section.get("known_hosts_file")}
    out["ssh"] = ssh
    return out


def _validate_ldap(section: dict, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["uri"] = _require_str(section, "uri", "ldap.uri", errors)
    try:
        out["starttls"] = _as_bool(section.get("starttls", False), "ldap.starttls")
    except ConfigError as e:
        errors.append(str(e))
        out["starttls"] = False

    bind_dn_file = _require_str(section, "bind_dn_file", "ldap.bind_dn_file", errors)
    bind_pw_file = _require_str(section, "bind_pw_file", "ldap.bind_pw_file", errors)
    out["bind_dn_file"] = bind_dn_file
    out["bind_pw_file"] = bind_pw_file

    bases = _require_str_list(section, "tenant_bases", "ldap.tenant_bases", errors)
    out["tenant_bases"] = tuple(bases or ())
    out["search_filter"] = section.get("filter", "(mail=*)") or "(mail=*)"

    attributes = section.get("attributes") or {}
    if not isinstance(attributes, dict):
        errors.append("ldap.attributes: Mapping Report-Feld -> LDAP-Attribut erwartet")
        attributes = {}
    for fkey in ("username", "mail_primary"):
        if not attributes.get(fkey):
            errors.append(f"ldap.attributes.{fkey}: Pflicht-Mapping fehlt")
    for fkey, aval in attributes.items():
        if fkey not in _MATCH_FIELDS:
            errors.append(f"ldap.attributes.{fkey}: unbekanntes Report-Feld")
        if not isinstance(aval, str) or not aval.strip():
            errors.append(f"ldap.attributes.{fkey}: Attributname als String erwartet")
    out["attributes"] = {k: v for k, v in attributes.items()
                         if k in _MATCH_FIELDS and isinstance(v, str)}

    match = _require_str_list(section, "match", "ldap.match", errors)
    if match:
        for fkey in match:
            if fkey not in _MATCH_FIELDS:
                errors.append(f"ldap.match: unbekanntes Feld {fkey!r}")
    else:
        match = ["mail_primary", "mail_aliases", "username"]
    out["match"] = tuple(match)

    groups = section.get("groups") or {}
    if not isinstance(groups, dict):
        errors.append("ldap.groups: Abschnitt als Mapping erwartet")
        groups = {}
    gmode = groups.get("match_mode", "member_filter")
    if gmode not in ("member_filter", "memberof_attr"):
        errors.append(f"ldap.groups.match_mode: 'member_filter' oder 'memberof_attr' "
                      f"erwartet, got {gmode!r}")
    out["groups_mode"] = gmode
    include = groups.get("include") or []
    exclude = groups.get("exclude") or []
    if not isinstance(include, list) or not isinstance(exclude, list):
        errors.append("ldap.groups.include/exclude: Listen von Gruppen-DNs erwartet")
        include = exclude = []
    out["include"] = tuple(include)
    out["exclude"] = tuple(exclude)
    return out


def _validate_smtp(section: dict, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["host"] = _require_str(section, "host", "smtp.host", errors)
    try:
        out["port"] = int(section.get("port", 25))
    except (TypeError, ValueError):
        errors.append("smtp.port: Zahl erwartet")
        out["port"] = 25
    try:
        out["starttls"] = _as_bool(section.get("starttls", False), "smtp.starttls")
    except ConfigError as e:
        errors.append(str(e))
        out["starttls"] = False
    out["sender"] = _require_str(section, "sender", "smtp.sender", errors)
    out["password_file"] = section.get("password_file")
    return out


def validate_config(data: Any) -> dict[str, Any]:
    """Struktur-Validierung (ohne Secret-Dateien). Sammelt ALLE Fehler."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError("Config: YAML-Mapping erwartet")
    out: dict[str, Any] = {}
    out["deployment"] = _require_str(data, "deployment", "deployment", errors)

    report = _require_mapping(data, "report", errors)
    message = _require_mapping(data, "message", errors)
    collect = _require_mapping(data, "collect", errors)
    ldap = _require_mapping(data, "ldap", errors)
    smtp = _require_mapping(data, "smtp", errors)

    out["report"] = _validate_report(report, errors)
    out["message"] = _validate_message(message, errors)
    out["collect"] = _validate_collect(collect, errors)
    out["ldap"] = _validate_ldap(ldap, errors)
    out["smtp"] = _validate_smtp(smtp, errors)

    selftest = data.get("selftest") or {}
    if not isinstance(selftest, dict):
        errors.append("selftest: Abschnitt als Mapping erwartet")
        selftest = {}
    try:
        out["selftest_network_probes"] = _as_bool(selftest.get("network_probes", False),
                                                  "selftest.network_probes")
    except ConfigError as e:
        errors.append(str(e))
        out["selftest_network_probes"] = False

    if errors:
        raise ConfigError("Config ungültig:\n  - " + "\n  - ".join(errors))
    return out


# ---------------------------------------------------------------------------
# Laden (inkl. Secrets — fail-closed)
# ---------------------------------------------------------------------------

def load_config(path: str | None = None) -> Config:
    """Config laden, validieren, Secrets aus Dateien auflösen.

    Raises ConfigError bei: fehlender Datei, YAML-Fehler, Strukturfehlern,
    fehlenden/unlesbaren/leeren Secret-Dateien.
    """
    cfg_path = resolve_config_path(path)
    p = Path(cfg_path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config-Datei fehlt: {cfg_path} "
                          f"(--config, ENV CONFIG_FILE/CONFIG_PATH)") from None
    except OSError as e:
        raise ConfigError(f"Config-Datei nicht lesbar: {cfg_path}: {e}") from None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"Config-Datei kein gültiges YAML: {cfg_path}: {e}") from None
    if data is None:
        raise ConfigError(f"Config-Datei ist leer: {cfg_path}")

    valid = validate_config(data)

    # --- Secrets aus Dateien (nie ENV, nie Image) --------------------------
    ldap_v = valid["ldap"]
    bind_dn = read_secret_file(ldap_v["bind_dn_file"], "LDAP-Bind-DN")
    bind_pw = read_secret_file(ldap_v["bind_pw_file"], "LDAP-Bind-Passwort")
    smtp_v = valid["smtp"]
    smtp_password = None
    if smtp_v.get("password_file"):
        smtp_password = read_secret_file(smtp_v["password_file"], "SMTP-Passwort")

    collect_v = valid["collect"]
    tcp_v = collect_v["tcp"]
    ssh_v = collect_v["ssh"]

    tcp_cfg = None
    if collect_v["type"] == "doveadm_tcp":
        tcp_password = None
        if tcp_v.get("password_file"):
            # doveadm_password: Pflicht für TCP-Verbindung (Server UND Client,
            # doc.dovecot.org settings.html#doveadm_password)
            tcp_password = read_secret_file(tcp_v["password_file"],
                                            "doveadm_password (doveadm-TCP)")
        elif tcp_v.get("endpoint"):
            errors_hint = ("collect.tcp.password_file fehlt — doveadm-TCP braucht "
                           "doveadm_password (Server-seitig gesetzt, gleicher Wert "
                           "im Client; siehe README Architektur-Entscheidung)")
            raise ConfigError(f"Config ungültig:\n  - {errors_hint}")
        tcp_cfg = DoveadmTcpConfig(
            endpoint=tcp_v.get("endpoint", ""),
            password=tcp_password,
            password_file=tcp_v.get("password_file"),
            ssl=tcp_v.get("ssl", "no"),
            timeout=tcp_v.get("timeout", 30),
        )

    ssh_cfg = None
    if collect_v["type"] == "ssh_exec":
        check_readable_file(ssh_v["key_file"], "SSH-Key (mqr_ssh_key)")
        ssh_cfg = SSHConfig(**ssh_v)

    return Config(
        deployment=valid["deployment"],
        report=ReportConfig(**valid["report"]),
        message=MessageConfig(**valid["message"]),
        collect=CollectConfig(
            type=collect_v["type"],
            enumeration=collect_v["enumeration"],
            tcp=tcp_cfg,
            ssh=ssh_cfg,
        ),
        ldap=LDAPConfig(
            uri=ldap_v["uri"],
            starttls=ldap_v["starttls"],
            bind_dn=bind_dn,
            bind_pw=bind_pw,
            tenant_bases=ldap_v["tenant_bases"],
            search_filter=ldap_v["search_filter"],
            attributes=ldap_v["attributes"],
            match=ldap_v["match"],
            groups_mode=ldap_v["groups_mode"],
            include=ldap_v["include"],
            exclude=ldap_v["exclude"],
        ),
        smtp=SMTPConfig(
            host=smtp_v["host"],
            port=smtp_v["port"],
            starttls=smtp_v["starttls"],
            sender=smtp_v["sender"],
            password=smtp_password,
        ),
        selftest_network_probes=valid["selftest_network_probes"],
    )
