"""template — Nachrichtenvorlagen text/html per Jinja2 (Phase 1).

Aufgaben (Templates/Rendering, Feature-Entscheidungen 06.11.2026):
- python3-jinja2 (Debian-Paket) statt Eigenbau: Autoescaping für HTML
  (keine Injection über Verzeichnisdaten), strikteUndefined für
  fehlende Platzhalter (fail-closed auch im Rendering)
- Template-Verzeichnis konfigurierbar (message.template_dir); Fallback
  auf mitgelieferte Standard-Templates (templates/ im Image, /app/templates)
- Subjekt-Schema aus der Config (report.subject) mit denselben
  Platzhaltern wie die Vorlagen
- Deutsche Zahlenformate: format_kib/format_percent aus report.py
- Schwellwert-Hervorhebung: CSS-Klassen warn/critical (>=80 %/>=95 %,
  konfigurierbar) — die Vorlagen nutzen status_class/status_label
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import __version__
from .filter import STATUS_CRIT, STATUS_OK, STATUS_WARN
from .report import ReportData, ReportRow, format_kib, format_percent_exact

log = logging.getLogger(__name__)

#: Mitgelieferte Standard-Templates (Repo: templates/, Image: /app/templates)
BUILTIN_TEMPLATE_DIR = str(Path(__file__).resolve().parent.parent.parent / "templates")

#: CSS-Klassen je Status (Vorlagen: tr.warn / tr.critical)
_STATUS_CLASS = {
    STATUS_OK: "ok",
    STATUS_WARN: "warn",
    STATUS_CRIT: "critical",
    "KEIN_LDAP": "no-ldap",
    "KEIN_QUOTA": "no-quota",
    "LIMIT_0": "limit0",
}

#: Anzeige-Labels je Status (deutsch)
_STATUS_LABEL = {
    STATUS_OK: "OK",
    STATUS_WARN: "WARNUNG",
    STATUS_CRIT: "KRITISCH",
    "KEIN_LDAP": "kein LDAP-Eintrag",
    "KEIN_QUOTA": "kein Quota-Datensatz",
    "LIMIT_0": "0 (Sonderfall)",
}


@dataclass(frozen=True)
class RenderedMessage:
    """Fertig gerenderter Report: Subjekt + Text/HTML-Teile."""

    subject: str
    text_body: str | None
    html_body: str | None


def status_class(status: str) -> str:
    """Status → CSS-Klasse für die Hervorhebungs-Zeilen."""
    return _STATUS_CLASS.get(status, "ok")


def status_label(status: str) -> str:
    """Status → Anzeige-Label (Status-Spalte)."""
    return _STATUS_LABEL.get(status, status)


def _autoescape(template_name: str | None) -> bool:
    """Autoescape NUR für die HTML-Vorlage — Text-Vorlage bleibt Plaintext."""
    if template_name is None:
        return False
    return template_name.endswith(".html.j2")


def make_env(template_dir: str | None = None) -> Environment:
    """Jinja2-Umgebung: Autoescape für HTML, StrictUndefined, deutsche Filters."""
    search_path = template_dir or BUILTIN_TEMPLATE_DIR
    env = Environment(
        loader=FileSystemLoader(search_path),
        autoescape=_autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["kib"] = format_kib
    env.filters["percent"] = format_percent_exact
    return env


def render_rows(rows: list[ReportRow]) -> list[dict]:
    """Report-Zeilen in vorlagenfreundliche Dictionaries heben."""
    out = []
    for r in rows:
        out.append({
            "mailbox": r.mailbox,
            "name": r.name or r.mailbox,
            "mail_primary": r.mail_primary,
            "mail_aliases": r.mail_aliases,
            "department": r.department,
            "company": r.company,
            "used": format_kib(r.used_kib),
            "limit": format_kib(r.limit_kib),
            "free": format_kib(r.free_kib),
            "percent": format_percent_exact(r.percent),
            "status": status_label(r.status),
            "status_class": status_class(r.status),
            "tenant": r.tenant,
            "error": r.error,
        })
    return out


def render_subject(schema: str, data: ReportData) -> str:
    """Betreff-Schema mit Zusammenfassungs-Platzhaltern rendern.

    Platzhalter im str.format-Stil ({deployment}, {date}, {n}, {warn},
    {crit}, {no_ldap}, {no_quota}) — bewusst KEIN Jinja: das Schema kommt
    aus der Config und soll simple Platzhalter bleiben. Unbekannte
    Platzhalter → KeyError → fail-closed im CLI.
    """
    try:
        return schema.format(
            deployment=data.deployment,
            date=data.date,
            n=data.n_total,
            warn=data.n_warn,
            crit=data.n_crit,
            no_ldap=data.n_no_ldap,
            no_quota=data.n_no_quota,
        )
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"report.subject: ungültiges Platzhalter-Schema: {e}") from e


def render_message(data: ReportData, message_cfg) -> RenderedMessage:
    """Komplette Nachricht rendern: Subjekt + text/html je message.format.

    text → nur Text; html → nur HTML; both → beide (mailer baut daraus
    multipart/alternative). Fehlt eine Vorlagendatei im konfigurierten
    template_dir, greift der Fallback auf die mitgelieferten Standard-
    Templates (BUILTIN_TEMPLATE_DIR).
    """
    template_dir = message_cfg.template_dir or BUILTIN_TEMPLATE_DIR
    env = make_env(template_dir)

    def _get_template(name: str):
        try:
            return env.get_template(name)
        except Exception as first_err:  # noqa: BLE001 — Fallback bewusst
            if template_dir and template_dir != BUILTIN_TEMPLATE_DIR:
                log.warning("Template %s nicht in %s (%s) — Fallback auf "
                            "mitgelieferte Standard-Templates",
                            name, template_dir, first_err)
                fallback_env = make_env(BUILTIN_TEMPLATE_DIR)
                return fallback_env.get_template(name)
            raise

    rows = render_rows(data.rows)
    # Mandanten-Gruppierung (group_by_tenant): dict Mandant -> Zeilen.
    # Die Vorlagen rendern je Mandant einen Abschnitt ("Gruppierung
    # je Mandant mit Subtotalen"); ohne group_by_tenant bleibt groups leer
    # und die flache Zeilen-Liste greift (Feldbefund aus dem
    # Pilot-Deployment: groups war bislang der BOOL aus der Config —
    # Jinja sah "truthy" statt der Gruppierung, die Vorlagen nutzten
    # ihn nicht).
    groups: dict[str, list[dict]] = {}
    if data.group_by_tenant and rows:
        for row in rows:
            groups.setdefault(row.get("tenant") or "ohne Mandant", []).append(row)
    summary = {
        "deployment": data.deployment,
        "date": data.date,
        "n": data.n_total,
        "warn": data.n_warn,
        "crit": data.n_crit,
        "no_ldap": data.n_no_ldap,
        "no_quota": data.n_no_quota,
        "warn_percent": data.warn_percent,
        "critical_percent": data.critical_percent,
        "mode": data.mode,
        "threshold_desc": data.threshold_desc,
        "version": __version__,
    }

    text_body = None
    html_body = None
    if message_cfg.format in ("text", "both"):
        text_body = _get_template(message_cfg.templates["text"]).render(
            rows=rows, summary=summary, groups=groups,
            errors=data.errors,
        )
    if message_cfg.format in ("html", "both"):
        html_body = _get_template(message_cfg.templates["html"]).render(
            rows=rows, summary=summary, groups=groups,
            errors=data.errors,
        )
    return RenderedMessage(subject="", text_body=text_body,
                          html_body=html_body)
