"""Unit-Tests ldap_source.py: Mapping, Aliase, Match-Kette, Gruppen."""

from __future__ import annotations

import pytest

from mail_quota_report.ldap_source import (
    LdapEntry,
    LdapSource,
    strip_proxy_prefix,
    tenant_from_dn,
)


# CANNED Daten — frei erfunden (example.org), NIE Produktivdaten.

def _cfg(match=None, groups_mode="member_filter", include=(), exclude=(),
         aliases_attr="mailAddresses"):
    from mail_quota_report.config import LDAPConfig
    return LDAPConfig(
        uri="ldap://ldap.example",
        starttls=False,
        bind_dn="cn=quota-report,ou=services,dc=example",
        bind_pw="fake",
        tenant_bases=("ou=users,cd=example.org,dc=ldap,dc=example",),
        search_filter="(mail=*)",
        attributes={
            "username": "uid",
            "first_name": "givenName",
            "last_name": "sn",
            "mail_primary": "mail",
            "mail_aliases": aliases_attr,
            "department": "departmentNumber",
            "company": "o",
            "quota_limit": "mailQuota",
        },
        match=match or ("mail_primary", "mail_aliases", "username"),
        groups_mode=groups_mode,
        include=tuple(include),
        exclude=tuple(exclude),
    )


# --- Wert-Dekodierung --------------------------------------------------------

def test_first_str_variants():
    from mail_quota_report.ldap_source import _first_str, _all_str
    assert _first_str(["Anna"]) == "Anna"
    assert _first_str(b"Anna") == "Anna"
    assert _first_str(None) == ""
    assert _all_str(["a", b"b"]) == ["a", "b"]
    assert _all_str(None) == []


def test_strip_proxy_prefix():
    assert strip_proxy_prefix("SMTP:anna@example.org") == "anna@example.org"
    assert strip_proxy_prefix("smtp:anna@example.org") == "anna@example.org"
    assert strip_proxy_prefix("X400:c=at") is None
    assert strip_proxy_prefix("anna@example.org") is None


def test_tenant_from_dn():
    dn = "uid=anna,ou=persons,ou=users,cd=example.org,dc=ldap,dc=example"
    assert tenant_from_dn(dn) == "example.org"
    assert tenant_from_dn("cn=x,dc=plain") == ""


# --- Eintrag-Aufbau über das Mapping ------------------------------------------

def test_entry_from_ldap_openldap():
    src = LdapSource(_cfg())
    data = {
        "uid": [b"anna"],
        "givenName": [b"Anna"],
        "sn": [b"Beispiel"],
        "mail": [b"anna@example.org"],
        "mailAddresses": [b"anna.b@example.org", b"anna.alias@example.org"],
        "departmentNumber": [b"IT"],
        "o": [b"Example GmbH"],
        "mailQuota": [b"1048576"],
    }
    entry = src._entry_from_ldap("uid=anna,ou=persons,cd=example.org,dc=x", data)
    assert entry is not None
    assert entry.first_name == "Anna"
    assert entry.last_name == "Beispiel"
    assert entry.mail_primary == "anna@example.org"
    assert entry.mail_aliases == ["anna.b@example.org", "anna.alias@example.org"]
    assert entry.department == "IT"
    assert entry.company == "Example GmbH"
    assert entry.quota_limit_kib == 1_048_576
    assert entry.tenant == "example.org"


def test_entry_from_ldap_ad_proxy_addresses():
    cfg = _cfg(aliases_attr="proxyAddresses")
    src = LdapSource(cfg)
    data = {
        "sAMAccountName": [b"anna"],
        "givenName": [b"Anna"],
        "sn": [b"Beispiel"],
        "mail": [b"anna@example.org"],
        "proxyAddresses": [b"SMTP:anna@example.org",
                           b"smtp:anna.b@example.org",
                           b"X400:c=at;"],
        "department": [b"IT"],
        "company": [b"Example GmbH"],
    }
    # AD-Mapping: username sAMAccountName
    cfg.attributes["username"] = "sAMAccountName"
    entry = src._entry_from_ldap("CN=Anna,DC=example,DC=local", data)
    assert entry is not None
    assert entry.username == "anna"
    # Primäre Adresse nicht als Alias; X400 gefallen; SMTP: gestrippt
    assert entry.mail_aliases == ["anna.b@example.org"]


def test_entry_without_mail_and_username_skipped():
    src = LdapSource(_cfg())
    entry = src._entry_from_ldap("cn=server,dc=x", {"cn": [b"server"]})
    assert entry is None


# --- Aufzählung über Fake-Connection ------------------------------------------

class _FakeConn:
    """Fake-Connection: search_s liefert canned Ergebnisse je Base."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def search_s(self, base, scope, filterstr, attrlist=None):
        self.calls.append((base, scope, filterstr, attrlist))
        return self.results.get(base, [])

    def unbind_s(self):
        pass


def test_enumerate_entries():
    cfg = _cfg()
    base = cfg.tenant_bases[0]
    results = {base: [
        ("uid=anna,ou=persons,cd=example.org,dc=x",
         {"mail": [b"anna@example.org"], "sn": [b"Beispiel"], "givenName": [b"Anna"]}),
        ("uid=bert,ou=persons,cd=example.org,dc=x",
         {"mail": [b"bert@example.org"], "sn": [b"B"]}),
    ]}
    src = LdapSource(cfg, conn_factory=lambda: _FakeConn(results))
    entries = src.enumerate_entries()
    assert len(entries) == 2
    assert entries[0].mail_primary == "anna@example.org"
    # Attributliste aus dem Mapping (dedupliziert)
    conn = _FakeConn(results)
    src.enumerate_entries(conn=conn)
    attrlist = conn.calls[0][3]
    assert "mail" in attrlist and "uid" in attrlist
    assert attrlist.count("mail") == 1


def test_enumerate_memberof_requested_for_ad_mode():
    cfg = _cfg(groups_mode="memberof_attr", aliases_attr="proxyAddresses")
    results = {cfg.tenant_bases[0]: [
        ("CN=Anna,DC=x", {"mail": [b"anna@example.org"],
                          "memberOf": [b"CN=mail,DC=x"]}),
    ]}
    conn = _FakeConn(results)
    src = LdapSource(cfg)
    src.enumerate_entries(conn=conn)
    assert "memberOf" in conn.calls[0][3]


# --- Match-Kette ---------------------------------------------------------------

def _entries():
    return [
        LdapEntry(dn="uid=anna", username="anna", mail_primary="anna@example.org",
                  mail_aliases=["anna.b@example.org"], last_name="Beispiel"),
        LdapEntry(dn="uid=bob", username="bob", mail_primary="",
                  mail_aliases=["bob@example.org"]),
    ]


def test_match_chain_primary_first():
    src = LdapSource(_cfg())
    idx = src.build_match_index(_entries())
    assert idx["anna@example.org"].dn == "uid=anna"
    assert idx["anna.b@example.org"].dn == "uid=anna"


def test_match_chain_alias_hit():
    src = LdapSource(_cfg())
    idx = src.build_match_index(_entries())
    assert idx["bob@example.org"].dn == "uid=bob"


def test_match_chain_username_fallback():
    src = LdapSource(_cfg())
    idx = src.build_match_index(_entries())
    assert idx["anna"].dn == "uid=anna"


def test_match_chain_no_hit_is_none():
    src = LdapSource(_cfg())
    matched = src.match_mailboxes(_entries(), ["nobody@example.org"])
    assert matched["nobody@example.org"] is None


def test_match_chain_duplicate_first_wins():
    e1 = LdapEntry(dn="uid=first", mail_primary="dup@example.org")
    e2 = LdapEntry(dn="uid=second", mail_primary="dup@example.org")
    src = LdapSource(_cfg())
    idx = src.build_match_index([e1, e2])
    assert idx["dup@example.org"].dn == "uid=first"


def test_match_chain_case_insensitive():
    src = LdapSource(_cfg())
    matched = src.match_mailboxes(_entries(), ["ANNA@example.org"])
    assert matched["ANNA@example.org"] is not None


# --- Gruppen -------------------------------------------------------------------

def test_group_filter_memberfilter_resolves_members():
    cfg = _cfg(include=["cn=mail-report,dc=x"])
    group_dn = "cn=mail-report,dc=x"
    base = cfg.tenant_bases[0]
    results = {
        base: [
            ("uid=anna,cd=example.org,dc=x", {"mail": [b"anna@example.org"]}),
            ("uid=bob,cd=example.org,dc=x", {"mail": [b"bob@example.org"]}),
        ],
        group_dn: [(group_dn, {"member": [b"uid=anna,cd=example.org,dc=x"]})],
    }
    src = LdapSource(cfg, conn_factory=lambda: _FakeConn(results))
    entries = [
        LdapEntry(dn="uid=anna,cd=example.org,dc=x", mail_primary="anna@example.org"),
        LdapEntry(dn="uid=bob,cd=example.org,dc=x", mail_primary="bob@example.org"),
    ]
    conn = _FakeConn(results)
    out = src.filter_by_groups(entries, conn)
    assert [e.dn for e in out] == ["uid=anna,cd=example.org,dc=x"]


def test_group_filter_exclude_wins_over_include():
    cfg = _cfg(include=["cn=all,dc=x"], exclude=["cn=bob-out,dc=x"])
    results = {
        "cn=all,dc=x": [("cn=all,dc=x", {"member": [b"uid=anna,dc=x", b"uid=bob,dc=x"]})],
        "cn=bob-out,dc=x": [("cn=bob-out,dc=x", {"member": [b"uid=bob,dc=x"]})],
    }
    src = LdapSource(cfg)
    entries = [
        LdapEntry(dn="uid=anna,dc=x", mail_primary="anna@example.org"),
        LdapEntry(dn="uid=bob,dc=x", mail_primary="bob@example.org"),
    ]
    out = src.filter_by_groups(entries, _FakeConn(results))
    assert [e.dn for e in out] == ["uid=anna,dc=x"]


def test_group_filter_memberof_attr_mode():
    cfg = _cfg(groups_mode="memberof_attr",
               include=["cn=mail,dc=x"], exclude=["cn=out,dc=x"])
    src = LdapSource(cfg)
    entries = [
        LdapEntry(dn="uid=anna,dc=x", mail_primary="anna@example.org",
                  member_of=["CN=MAIL,DC=x"]),
        LdapEntry(dn="uid=bob,dc=x", mail_primary="bob@example.org",
                  member_of=[]),
        LdapEntry(dn="uid=ce,dc=x", mail_primary="ce@example.org",
                  member_of=["cn=out,dc=x"]),
    ]
    out = src.filter_by_groups(entries, _FakeConn({}))
    assert [e.dn for e in out] == ["uid=anna,dc=x"]


def test_group_filter_empty_config_returns_all():
    src = LdapSource(_cfg())
    entries = _entries()
    out = src.filter_by_groups(entries, _FakeConn({}))
    assert len(out) == 2
