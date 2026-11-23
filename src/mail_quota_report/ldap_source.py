"""ldap_source — Verzeichnis-Anbindung OpenLDAP / Active Directory (Phase 1).

Aufgaben:
- Eine Abfrage je Lauf (Bind + Suche) über alle tenant_bases; Attribute
  per Mapping konfigurierbar (Vor-/Nachname, primäre Mail + Aliase,
  Abteilung, Firma, Quota-Limit nur Referenz)
- Aliase: OpenLDAP mailAddresses (Multi-Value) vs. AD proxyAddresses
  (SMTP:-Präfixe werden gefiltert/gestrippt)
- Gruppen-Filter als Templates: member_filter (OpenLDAP groupOfNames,
  member-Attribut der Gruppe auflösen) vs. memberof_attr (AD/memberOf-
  Overlay: Attribut am User-Eintrag lesen); exclude gewinnt über include
- Match-Kette Mailbox → LDAP-Eintrag (erster Treffer gewinnt):
  OpenLDAP mail → mailAddresses → uid; AD mail/proxyAddresses →
  sAMAccountName → userPrincipalName (sAMAccountName kann von der
  vollqualifizierten Adresse abweichen — Feldbefund aus dem
  Pilot-Deployment)
- StartTLS/LDAPS konfigurierbar; dedizierter RO-Bind aus Secret-Dateien
  (nicht der Dovecot-Bind cn=imap)
- Mailboxen ohne LDAP-Eintrag: gekennzeichnet, KEIN Abbruch
- Mehrfachzuordnung: erster Treffer, Warnung im Log ohne Personendaten

python3-ldap wird LAZY importiert (Sandbox/Unit-Tests ohne System-Libs:
conn_factory injizierbar, Fake-Connection mit canned Daten — NIE
Produktivdaten). Im Image ist python3-ldap ein Distro-Paket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

#: OpenLDAP: Mandant steckt im DN als cd=<domain> (Multi-Tenant-Schema)
_SCOPE_SUBTREE = 2
_SCOPE_BASE = 0


@dataclass
class LdapEntry:
    """Angereicherte Stammdaten eines Verzeichnis-Eintrags."""

    dn: str
    username: str = ""              # sAMAccountName / uid / mail je Mapping
    first_name: str = ""
    last_name: str = ""
    mail_primary: str = ""
    mail_aliases: list[str] = field(default_factory=list)
    department: str = ""
    company: str = ""
    quota_limit_kib: int | None = None
    tenant: str = ""
    member_of: list[str] = field(default_factory=list)


def _first_str(value: Any) -> str:
    """LDAP-Wert (bytes/str/Liste) → erster String-Wert, dekodiert."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _all_str(value: Any) -> list[str]:
    """LDAP-Wert (bytes/str/Liste) → Liste von Strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out = []
    for v in value:
        if isinstance(v, bytes):
            out.append(v.decode("utf-8", errors="replace"))
        else:
            out.append(str(v))
    return out


def tenant_from_dn(dn: str) -> str:
    """Mandant aus cd=<domain>-Bestandteil des DN (Multi-Tenant-Schema)."""
    for part in dn.split(","):
        part = part.strip()
        if part.lower().startswith("cd="):
            return part[3:]
    return ""


def strip_proxy_prefix(value: str) -> str | None:
    """AD proxyAddresses: 'SMTP:user@x' → 'user@x'; Nicht-SMTP → None.

    Sowohl 'SMTP:' (primär, großgeschrieben) als auch 'smtp:' (Alias)
    sind Mail-Adressen; X400/X500/CAL-Adressen fallen weg.
    """
    low = value.lower()
    if low.startswith("smtp:"):
        stripped = value[5:].strip()
        return stripped or None
    return None


class LdapSource:
    """Verzeichnis-Zugriff: Aufzählung, Anreicherung, Match-Kette, Gruppen.

    conn_factory ist injizierbar (Unit-Tests: Fake-Connection mit canned
    Daten). Produktiv: python3-ldap (Distro-Paket im Image).
    """

    def __init__(self, cfg, conn_factory: Callable[[], Any] | None = None):
        self.cfg = cfg
        self._conn_factory = conn_factory

    # -- Verbindung ---------------------------------------------------------

    def _connect(self):
        if self._conn_factory is not None:
            return self._conn_factory()
        import ldap  # lazy: Sandbox/Tests kommen ohne python-ldap aus

        conn = ldap.initialize(self.cfg.uri)
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.protocol_version = 3
        if self.cfg.starttls and self.cfg.uri.lower().startswith("ldap://"):
            conn.start_tls_s()
        conn.simple_bind_s(self.cfg.bind_dn, self.cfg.bind_pw)
        return conn

    # -- Aufzählung ---------------------------------------------------------

    def enumerate_entries(self, conn=None) -> list[LdapEntry]:
        """Alle Mailboxen tragenden Einträge über alle tenant_bases suchen."""
        own = conn is None
        if own:
            conn = self._connect()
        entries: list[LdapEntry] = []
        attrs = list(dict.fromkeys(self.cfg.attributes.values()))
        if self.cfg.groups_mode == "memberof_attr":
            attrs.append("memberOf")
        attrs = [a for a in attrs if a]
        try:
            for base in self.cfg.tenant_bases:
                for dn, data in conn.search_s(base, _SCOPE_SUBTREE,
                                              self.cfg.search_filter, attrs) or []:
                    if not dn:
                        continue
                    entry = self._entry_from_ldap(dn, data or {})
                    if entry:
                        entries.append(entry)
        finally:
            if own:
                try:
                    conn.unbind_s()
                except Exception:  # noqa: BLE001 — Unbind-Fehler ist unkritisch
                    pass
        return entries

    def _entry_from_ldap(self, dn: str, data: dict) -> LdapEntry | None:
        """Roh-Eintrag (DN, Attribut-Werte) → LdapEntry über das Mapping."""

        def attr(report_field: str) -> Any:
            ldap_name = self.cfg.attributes.get(report_field)
            if not ldap_name:
                return None
            return data.get(ldap_name)

        primary = _first_str(attr("mail_primary"))
        username = _first_str(attr("username"))
        if not primary and not username:
            return None  # Eintrag trägt keine Mailbox

        aliases: list[str] = []
        for raw in _all_str(attr("mail_aliases")):
            if self.cfg.attributes.get("mail_aliases", "").lower() == "proxyaddresses":
                cleaned = strip_proxy_prefix(raw)
            else:
                cleaned = raw.strip() or None
            if cleaned and cleaned != primary:
                aliases.append(cleaned)

        quota_limit = attr("quota_limit")
        quota_kib: int | None = None
        if quota_limit is not None:
            try:
                # Quota-Limits zählen je Schema unterschiedlich (kB oder
                # GiB) — die Umrechnung dokumentiert der Config-Kommentar
                # je Deployment; hier: Rohwert.
                quota_kib = int(_first_str(quota_limit))
            except (TypeError, ValueError):
                quota_kib = None

        return LdapEntry(
            dn=dn,
            username=username,
            first_name=_first_str(attr("first_name")),
            last_name=_first_str(attr("last_name")),
            mail_primary=primary,
            mail_aliases=aliases,
            department=_first_str(attr("department")),
            company=_first_str(attr("company")),
            quota_limit_kib=quota_kib,
            tenant=tenant_from_dn(dn),
            member_of=_all_str(data.get("memberOf")),
        )

    # -- Match-Kette --------------------------------------------------------

    def build_match_index(self, entries: list[LdapEntry]) -> dict[str, LdapEntry]:
        """Match-Index mailbox → entry gemäß cfg.match (erster Treffer gewinnt).

        Mögliche Match-Felder: mail_primary, mail_aliases, username.
        Mehrfachzuordnung: erster Eintrag gewinnt, Warnung
        im Log — ohne Personendaten, nur der Mailbox-Name.
        """
        index: dict[str, LdapEntry] = {}
        for entry in entries:
            for field_name in self.cfg.match:
                if field_name == "mail_primary":
                    values = [entry.mail_primary] if entry.mail_primary else []
                elif field_name == "mail_aliases":
                    values = entry.mail_aliases
                elif field_name == "username":
                    values = [entry.username] if entry.username else []
                else:
                    log.warning("Unbekanntes Match-Feld %r — übersprungen",
                                field_name)
                    continue
                for value in values:
                    key = value.strip().lower()
                    if not key:
                        continue
                    if key in index and index[key] is not entry:
                        log.warning("Mehrfachzuordnung für Mailbox %s — "
                                    "erster Treffer gewinnt", value)
                        continue
                    index[key] = entry
        return index

    def match_mailboxes(self, entries: list[LdapEntry],
                        mailboxes: list[str]) -> dict[str, LdapEntry | None]:
        """Match-Kette auf die geprüften Mailboxen anwenden.

        Rückgabe: mailbox → LdapEntry | None (None = kein LDAP-Eintrag,
        gekennzeichnet, kein Abbruch).
        """
        index = self.build_match_index(entries)
        result: dict[str, LdapEntry | None] = {}
        for mb in mailboxes:
            hit = index.get(mb.strip().lower())
            result[mb] = hit
            if hit is None:
                log.info("Kein LDAP-Eintrag für Mailbox %s", mb)
        return result

    # -- Gruppen-Filter -----------------------------------------------------

    def group_member_filter_template(self, group_dn: str) -> str:
        """OpenLDAP-Mitgliedschafts-Filter-Template (per User)."""
        cn = ""
        for part in group_dn.split(","):
            part = part.strip()
            if part.lower().startswith("cn="):
                cn = part[3:]
                break
        return (f"(&(objectClass=groupOfNames)(cn={cn})(member={{dn}}))")

    def resolve_groups(self, conn, group_dns: list[str]) -> set[str]:
        """Mitglieder-DNs der Gruppen auflösen (Auflösung per Gruppe).

        Liest das member-Attribut je Gruppe — funktioniert für OpenLDAP
        groupOfNames und AD-Gruppen. memberof_attr-Modus: zusätzlich
        trägt jeder Entry member_of (siehe enumerate_entries); die
        Filterung selbst macht filter.apply_group_filter mit den hier
        aufgelösten Mengen ODER cli.py über entry.member_of.
        """
        members: set[str] = set()
        for dn in group_dns:
            try:
                res = conn.search_s(dn, _SCOPE_BASE, "(objectClass=*)", ["member"])
            except Exception as e:  # noqa: BLE001 — fehlende Gruppe: kein Abbruch
                log.warning("Gruppe %s nicht auflösbar: %s", dn, e)
                continue
            for _dn, data in res or []:
                for m in _all_str((data or {}).get("member")):
                    members.add(m.strip().lower())
        return members

    def filter_by_groups(self, entries: list[LdapEntry], conn) -> list[LdapEntry]:
        """Include/Exclude aus der Config anwenden (exclude gewinnt).

        member_filter: Gruppen-Mitglieder per resolve_groups auflösen,
        Filter über die DN-Mengen. memberof_attr: Filter über das
        memberOf-Attribut der Entries (AD pflegt es; OpenLDAP-Overlay).
        include leer = alle; exclude gewinnt.
        """
        if not self.cfg.include and not self.cfg.exclude:
            return list(entries)

        if self.cfg.groups_mode == "memberof_attr":
            include = {d.strip().lower() for d in self.cfg.include}
            exclude = {d.strip().lower() for d in self.cfg.exclude}
            out = []
            for e in entries:
                groups = {g.strip().lower() for g in e.member_of}
                if groups & exclude:
                    continue
                if include and not (groups & include):
                    continue
                out.append(e)
            return out

        include_members = self.resolve_groups(conn, list(self.cfg.include))
        exclude_members = self.resolve_groups(conn, list(self.cfg.exclude))
        out = []
        for e in entries:
            dn = e.dn.strip().lower()
            if dn in exclude_members:
                continue
            if include_members and dn not in include_members:
                continue
            out.append(e)
        return out
