"""Unit-Tests config.py: Validierung + fail-closed-Fälle."""

from __future__ import annotations

import textwrap

import pytest

from mail_quota_report.config import (
    ConfigError,
    load_config,
    read_secret_file,
    validate_config,
)
from conftest import VALID_CONFIG


def _mutate(base: str, old: str, new: str) -> str:
    assert old in base, f"Fixture-Text fehlt: {old!r}"
    return base.replace(old, new)


# --- Validierung: Struktur --------------------------------------------------

def test_validate_ok():
    import yaml
    data = yaml.safe_load(VALID_CONFIG)
    valid = validate_config(data)
    assert valid["deployment"] == "universalnet"
    assert valid["report"]["mode"] == "all"
    assert valid["message"]["format"] == "both"


def test_validate_missing_deployment():
    import yaml
    data = yaml.safe_load(VALID_CONFIG)
    del data["deployment"]
    with pytest.raises(ConfigError, match="deployment"):
        validate_config(data)


def test_validate_bad_mode():
    import yaml
    data = yaml.safe_load(_mutate(VALID_CONFIG, "mode: all", "mode: whatever"))
    with pytest.raises(ConfigError, match="report.mode"):
        validate_config(data)


def test_validate_below_threshold_requires_threshold():
    import yaml
    data = yaml.safe_load(_mutate(VALID_CONFIG, "mode: all", "mode: below_threshold"))
    with pytest.raises(ConfigError, match="threshold"):
        validate_config(data)


def test_validate_below_threshold_percent_ok():
    import yaml
    raw = VALID_CONFIG.replace("mode: all", "mode: below_threshold")
    raw = raw.replace(
        "  sort: percent_desc",
        "  threshold:\n    type: percent\n    value: 80\n  sort: percent_desc")
    data = yaml.safe_load(raw)
    valid = validate_config(data)
    assert valid["report"]["mode"] == "below_threshold"
    assert valid["report"]["threshold"].type == "percent"
    assert valid["report"]["threshold"].value == 80


def test_validate_bad_format():
    import yaml
    data = yaml.safe_load(_mutate(VALID_CONFIG, "format: both", "format: rtf"))
    with pytest.raises(ConfigError, match="message.format"):
        validate_config(data)


def test_validate_bad_thresholds_relation():
    import yaml
    data = yaml.safe_load(_mutate(
        VALID_CONFIG,
        "thresholds: {warn_percent: 80, critical_percent: 95}",
        "thresholds: {warn_percent: 96, critical_percent: 95}"))
    with pytest.raises(ConfigError, match="warn_percent muss kleiner"):
        validate_config(data)


# --- Laden: fail-closed -----------------------------------------------------

def test_load_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    with pytest.raises(ConfigError, match="Config-Datei fehlt"):
        load_config(str(tmp_path / "gibt-es-nicht.yaml"))


def test_load_yaml_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("report: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="kein gültiges YAML"):
        load_config(str(p))


def test_load_empty_file(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="leer"):
        load_config(str(p))


def test_load_secrets_resolved(loaded_config):
    assert loaded_config.ldap.bind_dn.startswith("cn=quota-report")
    assert loaded_config.ldap.bind_pw == "fake-password"
    assert loaded_config.smtp.password is None


def test_load_missing_secret_file(config_env):
    from pathlib import Path
    # Bind-PW-Secret entfernen → fail-closed
    pw = Path(str(config_env)).parent.parent / "secrets" / "bind_pw"
    pw.unlink()
    with pytest.raises(ConfigError, match="Secret-Datei fehlt"):
        load_config(str(config_env))


def test_load_empty_secret_file(config_env):
    from pathlib import Path
    pw = Path(str(config_env)).parent.parent / "secrets" / "bind_pw"
    pw.write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="leer"):
        load_config(str(config_env))


def test_load_ssh_key_must_exist(config_env):
    """ssh_exec-Fallback: Key-Prüfung nur bei type: ssh_exec (Phase 3)."""
    from pathlib import Path
    import re
    from mail_quota_report.config import load_config
    cfg_path = Path(str(config_env))
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace("type: doveadm_tcp", "type: ssh_exec")
    # tcp-Block (dedentet im Config-File: 2/4 Spaces) → ssh-Block umschreiben
    text = re.sub(
        r"  tcp:\n(?:    .*\n)+",
        "  ssh:\n"
        "    hosts: [mailhost.example]\n"
        "    container_filter: mail-imap-1\n"
        "    user: root\n"
        "    key_file: /gibt/es/nicht/key\n",
        text)
    cfg_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="SSH-Key"):
        load_config(str(config_env))


def test_load_via_env_var(config_env, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", str(config_env))
    from mail_quota_report.config import load_config
    cfg = load_config()
    assert cfg.deployment == "universalnet"


def test_read_secret_file_missing():
    with pytest.raises(ConfigError, match="fehlt"):
        read_secret_file("/gibt/es/nicht", "Test")


def test_env_override_config_path(config_env, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(config_env))
    cfg = load_config()
    assert cfg.deployment == "universalnet"
