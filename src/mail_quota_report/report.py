"""report — Aufbereitung, Sortierung, Zahlenformate (Phase 1).

Aufgaben:
- Report-Zeilen: Name (Nachname, Vorname) | Mail-Adressen (primär +
  Aliase) | Abteilung | Firma | Benutzt | Limit | Auslastung % | Status
- Deutsche Zahlenformate: Tausenderpunkte, Komma als Dezimaltrenner
  (9.820.254 kB → „9,82 GB von 10,00 GB — 98 %"); KB-Anzeige unter 1 GB,
  sonst GB mit zwei Nachkommastellen
- Schwellwert-Hervorhebung: ≥ 80 % WARNUNG, ≥ 95 % KRITISCH
  (konfigurierbar) — liefert die Status-Spalte; Sonderfälle: KEIN_LDAP,
  KEIN_QUOTA, LIMIT_0
- Sortierung: percent_desc (Standard) | name_asc | limit_desc
- Zusammenfassung: geprüft / ohne LDAP / ohne Quota-Datensatz / warn /
  kritisch — Basis für Betreff {n}/{warn}/{crit} und Leer-Report-
  Kurzfassung
- Kein Datenbestand über den Lauf hinaus
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .filter import (
    STATUS_CRIT,
    STATUS_LIMIT_0,
    STATUS_NO_LDAP,
    STATUS_NO_QUOTA,
    STATUS_OK,
    STATUS_WARN,
    classify,
    used_percent,
)

KIB_PER_MB = 1_000
KIB_PER_GB = 1_000_000


def format_kib(kib: int) -> str:
    """kB-Einheiten deutsch formatieren: 9820254 → „9,82 GB"; 51200 → „50,00 MB".

    Unter 1 GB: MB mit zwei Nachkommastellen; unter 1 MB: kB ganzzahlig.
    Tausenderpunkte bei kB. Negative Werte (Überziehung) mit Minus.
    """
    sign = "-" if kib < 0 else ""
    kib = abs(int(kib))
    if kib >= KIB_PER_GB:
        return f"{sign}{kib / KIB_PER_GB:.2f} GB".replace(".", ",")
    if kib >= KIB_PER_MB:
        return f"{sign}{kib / KIB_PER_MB:.2f} MB".replace(".", ",")
    return f"{sign}{kib:,} kB".replace(",", ".")


def format_percent(percent: float | None) -> str:
    """Prozent deutsch: 98.0 → „98 %"; None → „—" (Limit 0: kein Prozentwert)."""
    if percent is None:
        return "—"
    return f"{percent:.0f} %".replace(".", ",")


def format_percent_exact(percent: float | None) -> str:
    """Prozent mit einer Nachkommastelle (nur wenn sinnvoll): 98,4 %."""
    if percent is None:
        return "—"
    return f"{percent:.1f} %".replace(".", ",")


@dataclass
class ReportRow:
    """Eine Report-Zeile (Mailbox) im finalen Aufbereitungsformat."""

    mailbox: str
    name: str = ""
    mail_primary: str = ""
    mail_aliases: list[str] = field(default_factory=list)
    department: str = ""
    company: str = ""
    used_kib: int = 0
    limit_kib: int = 0
    percent: float | None = None
    free_kib: int = 0
    status: str = STATUS_OK
    tenant: str = ""
    error: str = ""


@dataclass
class ReportData:
    """Vollständig aufbereiteter Report (Eingabe für template.py)."""

    deployment: str
    date: str
    rows: list[ReportRow] = field(default_factory=list)
    n_total: int = 0
    n_no_ldap: int = 0
    n_no_quota: int = 0
    n_warn: int = 0
    n_crit: int = 0
    warn_percent: float = 80.0
    critical_percent: float = 95.0
    sort: str = "percent_desc"
    group_by_tenant: bool = False
    hide_zero_limit: bool = False
    mode: str = "all"
    threshold_desc: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return self.n_total - self.n_no_ldap - self.n_no_quota

    def summary_line(self) -> str:
        """Ein-Zeilen-Summary fürs Log (ohne Personendaten)."""
        return (f"Lauf-Ende: mailboxen={self.n_total} warn={self.n_warn} "
                f"kritisch={self.n_crit} ohne_ldap={self.n_no_ldap} "
                f"ohne_quota={self.n_no_quota}")


def build_rows(mailboxes: list[str], quota_records: dict, ldap_entries: dict,
               warn_percent: float, critical_percent: float,
               group_by_tenant: bool = False, hide_zero_limit: bool = False,
               tenant_of: Any = None) -> list[ReportRow]:
    """Mailboxen + Quota-Records + LDAP-Einträge zu Report-Zeilen vereinen.

    - ldap_entries: mailbox → LdapEntry (None-Wert = kein LDAP-Eintrag)
    - quota_records: mailbox → QuotaRecord (fehlt → KEIN_QUOTA)
    - Sonderfälle: kein LDAP-Eintrag (Werte trotzdem zeigen), kein
      Quota-Datensatz, Limit 0 (kein Prozentwert)
    - hide_zero_limit=True: Limit-0-Zeilen ausblenden (Service-Konten)
    - tenant_of: aufrufbar (ldap_entry) -> Mandant (nur bei
      group_by_tenant; sonst leer)
    """
    rows: list[ReportRow] = []
    for mb in mailboxes:
        entry = ldap_entries.get(mb)
        rec = quota_records.get(mb)
        row = ReportRow(mailbox=mb)
        if entry is not None:
            row.name = f"{entry.last_name}, {entry.first_name}".strip(", ")
            row.mail_primary = entry.mail_primary
            row.mail_aliases = list(getattr(entry, "mail_aliases", []) or [])
            row.department = entry.department
            row.company = entry.company
            if group_by_tenant and tenant_of:
                row.tenant = tenant_of(entry) or ""
        else:
            row.status = STATUS_NO_LDAP
        if rec is None:
            if row.status != STATUS_NO_LDAP:
                row.status = STATUS_NO_QUOTA
            row.error = "kein Quota-Datensatz"
        else:
            row.used_kib = rec.used_kib
            row.limit_kib = rec.limit_kib
            row.free_kib = rec.limit_kib - rec.used_kib
            pct = used_percent(rec.used_kib, rec.limit_kib)
            row.percent = pct
            if row.status != STATUS_NO_LDAP:
                row.status = classify(pct, warn_percent, critical_percent)
            else:
                # Quoten zeigen, Status bleibt KEIN_LDAP (Hervorhebung
                # trotzdem korrekt, classify liefert nur die Klasse)
                row.status = STATUS_NO_LDAP
        if hide_zero_limit and row.limit_kib == 0 and row.status != STATUS_NO_QUOTA:
            continue
        rows.append(row)
    return rows


def sort_rows(rows: list[ReportRow], sort: str = "percent_desc") -> list[ReportRow]:
    """Sortierung: percent_desc (kritischste zuerst) | name_asc | limit_desc."""
    if sort == "name_asc":
        return sorted(rows, key=lambda r: (r.name.lower(), r.mailbox.lower()))
    if sort == "limit_desc":
        return sorted(rows, key=lambda r: r.limit_kib, reverse=True)
    # Standard: Prozent absteigend; None (Limit 0) ans Ende
    return sorted(
        rows,
        key=lambda r: (r.percent is None, -(r.percent if r.percent is not None else 0)),
    )


def summarize(rows: list[ReportRow]) -> dict[str, int]:
    """Zähler für Zusammenfassung/Betreff: n, warn, crit, no_ldap, no_quota."""
    return {
        "n": len(rows),
        "warn": sum(1 for r in rows if r.status == STATUS_WARN),
        "crit": sum(1 for r in rows if r.status == STATUS_CRIT),
        "no_ldap": sum(1 for r in rows if r.status == STATUS_NO_LDAP),
        "no_quota": sum(1 for r in rows if r.status == STATUS_NO_QUOTA),
    }


def group_rows_by_tenant(rows: list[ReportRow]) -> dict[str, list[ReportRow]]:
    """Zeilen je Mandant gruppieren (group_by_tenant)."""
    groups: dict[str, list[ReportRow]] = {}
    for row in rows:
        groups.setdefault(row.tenant or "ohne Mandant", []).append(row)
    return groups
