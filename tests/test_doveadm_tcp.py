"""Unit-Tests Phase 3: doveadm_tcp-Collector, Config-Validierung, Auswahl.

CANNED Daten — frei erfunden, NIE Produktivdaten (DSGVO).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mail_quota_report.config import (
    ConfigError,
    DoveadmTcpConfig,
)
from mail_quota_report.doveadm import (
    CollectorError,
    DoveadmSshCollector,
    DoveadmTcpCollector,
    make_collector,
)

# --- Hilfs-Objekte -------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRunner:
    """Zeichnet doveadm-Aufrufe auf; liefert je Mailbox eine canned Antwort."""

    def __init__(self, responses_by_rc=None, default=None):
        self.calls = []
        self.responses_by_rc = responses_by_rc or {}
        self.default = default or _FakeCompleted(
            0, stdout="Quota name Type Value Limit %=\n"
                      "STORAGE STORAGE 9820254 (k) 10000000 (k) 98\n")

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        # Antwort je nach aufgerufener Mailbox (letztes argv-Element)
        mailbox = cmd[-1]
        if mailbox in self.responses_by_rc:
            return self.responses_by_rc[mailbox]
        return self.default


def _tcp_cfg(endpoint="mail-imap.example:4242", ssl="no",
             password: str | None = "secret"):
    return DoveadmTcpConfig(endpoint=endpoint, password=password,
                            password_file="/run/secrets/mqr_doveadm_password",
                            ssl=ssl, timeout=5)


# --- Verifizierte Kommandozeile (doveadm-quota(1)-Syntax) ------------------------

def test_tcp_cmdline_verified_syntax():
    """doveadm quota get -S <host>:<port> -u <mailbox> (doveadm-quota(1)).

    Mit password (Default) kommt -c <temp-conf> hinzu (PW-Übergabe);
    ohne password bleibt die reine Syntax.
    """
    runner = _RecordingRunner()
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    collector.collect(["a@example.org"])
    cmd, kwargs = runner.calls[0]
    assert cmd[0] == "doveadm"
    assert cmd[1] == "-c" and os.path.basename(cmd[2]).startswith("mqr-doveadm-")
    assert cmd[3:] == ["quota", "get", "-S", "mail-imap.example:4242",
                       "-u", "a@example.org"]
    # Ohne PW: keine Temp-Config, reine Syntax
    runner2 = _RecordingRunner()
    DoveadmTcpCollector(_tcp_cfg(password=None), runner=runner2).collect(
        ["a@example.org"])
    cmd2, _ = runner2.calls[0]
    assert cmd2 == ["doveadm", "quota", "get", "-S", "mail-imap.example:4242",
                   "-u", "a@example.org"]


def test_tcp_cmdline_ssl_options():
    """ssl: starttls → -o doveadm_ssl=starttls; no → KEIN -o (Default)."""
    runner = _RecordingRunner()
    collector = DoveadmTcpCollector(_tcp_cfg(ssl="starttls"), runner=runner)
    collector.collect(["a@example.org"])
    cmd = runner.calls[0][0]
    assert "-o" in cmd and "doveadm_ssl=starttls" in cmd

    runner2 = _RecordingRunner()
    collector2 = DoveadmTcpCollector(_tcp_cfg(ssl="no"), runner=runner2)
    collector2.collect(["a@example.org"])
    cmd2 = runner2.calls[0][0]
    assert not any("doveadm_ssl" in part for part in cmd2)


def test_tcp_password_via_client_config_never_argv():
    """doveadm_password über Temp-Client-Config (-c) — nie in der
    Prozessliste, nie im ENV (DOVEADM_PASSWORD wird von doveadm 2.4
    ignoriert — Feldbefund aus dem Pilot-Deployment)."""
    runner = _RecordingRunner()
    collector = DoveadmTcpCollector(_tcp_cfg(password="geheim"), runner=runner)
    collector.collect(["a@example.org"])
    cmd, kwargs = runner.calls[0]
    assert "geheim" not in " ".join(cmd)          # nie argv
    assert cmd[1] == "-c" and os.path.basename(cmd[2]).startswith("mqr-doveadm-")
    assert "geheim" not in str(kwargs["env"])     # nie ENV
    assert not os.path.exists(cmd[2])             # Temp-Config gelöscht


def test_tcp_parsing_table_output():
    runner = _RecordingRunner()
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    result = collector.collect(["a@example.org", "b@example.org"])
    assert set(result.records) == {"a@example.org", "b@example.org"}
    rec = result.records["a@example.org"]
    assert rec.used_kib == 9_820_254
    assert rec.limit_kib == 10_000_000
    assert rec.source_host == "mail-imap.example:4242"
    assert result.errors == {}


def test_tcp_per_mailbox_error_no_abort():
    """Einzel-Fehler (User existiert nicht, doveadm rc=67) reißt den Lauf nicht."""
    responses = {
        "b@example.org": _FakeCompleted(67, stderr="doveadm(b@example.org): "
                                                   "Unknown user"),
    }
    runner = _RecordingRunner(responses_by_rc=responses)
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    result = collector.collect(["a@example.org", "b@example.org"])
    assert "a@example.org" in result.records
    assert "b@example.org" in result.errors
    assert "Unknown user" in result.errors["b@example.org"]


def test_tcp_connection_refused_is_collector_error():
    """Exit 76 (Protocol error during remote host connection) → CollectorError
    für den GESAMTEN Lauf (Endpoint down), nicht still per-Mailbox."""
    responses = {
        "a@example.org": _FakeCompleted(76, stderr="doveadm: localhost:4242: "
                                                    "Connection refused"),
    }
    runner = _RecordingRunner(responses_by_rc=responses)
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    with pytest.raises(CollectorError, match="nicht erreichbar"):
        collector.collect(["a@example.org"])


def test_tcp_timeout_is_per_mailbox_error():
    import subprocess

    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    result = collector.collect(["a@example.org"])
    assert "a@example.org" in result.errors
    assert "Timeout" in result.errors["a@example.org"]


def test_tcp_no_storage_line_is_error():
    runner = _RecordingRunner(default=_FakeCompleted(0, stdout="irgendwas\n"))
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    result = collector.collect(["a@example.org"])
    assert "a@example.org" in result.errors
    assert "STORAGE" in result.errors["a@example.org"]


def test_tcp_empty_mailbox_list():
    runner = _RecordingRunner()
    collector = DoveadmTcpCollector(_tcp_cfg(), runner=runner)
    result = collector.collect([])
    assert result.records == {} and result.errors == {}
    assert runner.calls == []


# --- Config-Validierung: beide Collector-Typen -----------------------------------

def _collect_section(**overrides):
    base = {
        "type": "doveadm_tcp",
        "tcp": {"endpoint": "mail-imap.example:4242",
                "password_file": "/run/secrets/mqr_doveadm_password"},
        "enumeration": "ldap",
    }
    base.update(overrides)
    return base


def _validate_collect_section(section):
    from mail_quota_report.config import validate_config
    data = {
        "deployment": "t",
        "report": {"subject": "s", "recipients": ["a@example.org"]},
        "message": {},
        "collect": section,
        "ldap": {
            "uri": "ldap://x", "bind_dn_file": "/f", "bind_pw_file": "/f",
            "tenant_bases": ["ou=x"],
            "attributes": {"username": "uid", "mail_primary": "mail"},
            "match": ["mail_primary"],
        },
        "smtp": {"host": "h", "sender": "s@x"},
    }
    return validate_config(data)


def test_config_tcp_valid():
    valid = _validate_collect_section(_collect_section())
    assert valid["collect"]["type"] == "doveadm_tcp"
    assert valid["collect"]["tcp"]["endpoint"] == "mail-imap.example:4242"


def test_config_tcp_missing_endpoint():
    section = _collect_section(tcp={"password_file": "/f"})
    with pytest.raises(ConfigError, match="endpoint"):
        _validate_collect_section(section)


@pytest.mark.parametrize("bad", [
    "nurhost",              # kein Port
    ":4242",                # kein Host
    "host:0",               # Port 0
    "host:99999",           # Port > 65535
    "host:port",            # Port keine Zahl
    "host with space:4242", # Leerzeichen
])
def test_config_tcp_invalid_endpoint(bad):
    section = _collect_section(tcp={"endpoint": bad,
                                    "password_file": "/f"})
    with pytest.raises(ConfigError, match="host:port|Port"):
        _validate_collect_section(section)


def test_config_invalid_type():
    section = _collect_section(type="carrier_pigeon")
    with pytest.raises(ConfigError, match="collect.type"):
        _validate_collect_section(section)


def test_config_ssh_exec_valid():
    section = _collect_section(
        type="ssh_exec",
        ssh={"hosts": ["h1"], "container_filter": "imap", "user": "root",
             "key_file": "/k"})
    valid = _validate_collect_section(section)
    assert valid["collect"]["type"] == "ssh_exec"
    assert valid["collect"]["ssh"]["hosts"] == ("h1",)


def test_config_ssh_exec_missing_ssh_section():
    section = _collect_section(type="ssh_exec")
    with pytest.raises(ConfigError, match="collect.ssh"):
        _validate_collect_section(section)


def test_config_tcp_password_file_required_by_load(config_env):
    """load_config: doveadm_tcp ohne password_file → fail-closed."""
    from pathlib import Path
    cfg_text = Path(str(config_env)).read_text(encoding="utf-8")
    cfg_text = cfg_text.replace(
        "password_file: /run/secrets/mqr_doveadm_password", "")
    # Der Fixture-Pfad wurde auf tmp umgeschrieben — greife den generischen Teil:
    import re as _re
    cfg_text = _re.sub(r"password_file: \S+\n", "", cfg_text)
    Path(str(config_env)).write_text(cfg_text, encoding="utf-8")
    from mail_quota_report.config import load_config
    with pytest.raises(ConfigError, match="password_file"):
        load_config(str(config_env))


def test_config_tcp_password_loaded(loaded_config):
    assert loaded_config.collect.type == "doveadm_tcp"
    assert loaded_config.collect.tcp.password == "fake-doveadm-password"
    assert loaded_config.collect.ssh is None


# --- Collector-Auswahl --------------------------------------------------------

def test_make_collector_selection(loaded_config):
    collector = make_collector(loaded_config.collect)
    assert isinstance(collector, DoveadmTcpCollector)


def test_make_collector_ssh(loaded_config):
    from dataclasses import replace
    from mail_quota_report.config import CollectConfig, SSHConfig
    ssh_cfg = CollectConfig(
        type="ssh_exec", enumeration="ldap", tcp=None,
        ssh=SSHConfig(hosts=("h1",), container_filter="imap", user="root",
                      key_file="/k"))
    collector = make_collector(ssh_cfg)
    assert isinstance(collector, DoveadmSshCollector)


def test_make_collector_unknown_type():
    from mail_quota_report.config import CollectConfig
    bogus = CollectConfig(type="bogus", enumeration="ldap", tcp=None, ssh=None)
    with pytest.raises(CollectorError, match="collect.type"):
        make_collector(bogus)
