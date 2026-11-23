"""Gemeinsame Test-Fixtures: src-Pfad + canned Config + Secrets.

Alle Daten sind frei erfunden; Adressen @example.org/.at sind RFC-2606-
Reservierungen bzw. offensichtliche Platzhalter. NIE Produktivdaten.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

VALID_CONFIG = textwrap.dedent("""\
    deployment: universalnet
    report:
      mode: all
      language: de
      number_format: de_DE
      subject: "Quota-Report {deployment} {date}: {n} Mailboxen, {warn} Warnung, {crit} kritisch"
      recipients:
        - test@example.org
      thresholds: {warn_percent: 80, critical_percent: 95}
      sort: percent_desc
      group_by_tenant: true
      hide_zero_limit: false
    message:
      format: both
      template_dir: null
      templates:
        subject: subject.txt
        text: report.txt.j2
        html: report.html.j2
    collect:
      type: doveadm_tcp
      tcp:
        endpoint: mail-imap.example:4242
        password_file: /run/secrets/mqr_doveadm_password
        ssl: no
        timeout: 30
      enumeration: ldap
    ldap:
      uri: ldap://ldap.example
      starttls: false
      bind_dn_file: /run/secrets/mqr_ldap_bind_dn
      bind_pw_file: /run/secrets/mqr_ldap_bind_pw
      tenant_bases:
        - "ou=users,dc=example,dc=org"
      filter: "(mail=*)"
      attributes:
        username: uid
        first_name: givenName
        last_name: sn
        mail_primary: mail
        mail_aliases: mailAddresses
        department: departmentNumber
        company: o
        quota_limit: mailQuota
      match: [mail_primary, mail_aliases, username]
      groups:
        match_mode: member_filter
        include: []
        exclude: []
    smtp:
      host: smtp.example
      port: 25
      starttls: false
      sender: quota-report@example.org
    """)


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Validierte Config + lesbare Fake-Secrets in tmp_path."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    cfg_file = cfg_dir / "config.yaml"
    cfg_file.write_text(VALID_CONFIG, encoding="utf-8")

    (secrets / "ssh_key").write_text("FAKE-KEY\n", encoding="utf-8")
    (secrets / "bind_dn").write_text("cn=quota-report,ou=services,dc=example\n",
                                     encoding="utf-8")
    (secrets / "bind_pw").write_text("fake-password\n", encoding="utf-8")
    (secrets / "doveadm_pw").write_text("fake-doveadm-password\n",
                                        encoding="utf-8")

    content = (cfg_file.read_text(encoding="utf-8")
               .replace("/run/secrets/mqr_doveadm_password", str(secrets / "doveadm_pw"))
               .replace("/run/secrets/mqr_ldap_bind_dn", str(secrets / "bind_dn"))
               .replace("/run/secrets/mqr_ldap_bind_pw", str(secrets / "bind_pw")))
    cfg_file.write_text(content, encoding="utf-8")

    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    return cfg_file


@pytest.fixture
def loaded_config(config_env):
    """Bereits geladene Config (Validierung + Secrets)."""
    from mail_quota_report.config import load_config
    return load_config(str(config_env))
