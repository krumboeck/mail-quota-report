"""Feldbefunde aus dem Pilot-Deployment: doveadm-ENV-Vererbung und PW-Übergabe.

Live im Pilot-Deployment bewiesen:
1. CONFIG_FILE/CONFIG_PATH (Report-YAML) dürfen NICHT an doveadm vererbt
   werden — doveadm parst die YAML sonst als Dovecot-Config.
2. DOVEADM_PASSWORD-ENV wird von doveadm 2.4 ignoriert; das PW gehört in
   eine temporäre Client-Config (-c, 0600), nie in argv.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mail_quota_report.config import DoveadmTcpConfig
from mail_quota_report.doveadm import DoveadmTcpCollector


class _RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))

        class _P:
            returncode = 0
            stdout = ("Quota name Type Value Limit %\n"
                      "STORAGE STORAGE 1000 (k) 2000 (k) 50\n")
            stderr = ""
        return _P()


def _cfg(password="geheim"):
    return DoveadmTcpConfig(endpoint="imap:4242", password=password,
                            password_file="/run/secrets/x", ssl="no",
                            timeout=30)


def test_env_config_vars_stripped():
    """CONFIG_FILE/CONFIG_PATH dürfen NICHT an doveadm vererbt werden."""
    os.environ["CONFIG_FILE"] = "/etc/mail-quota-report/config.yaml"
    os.environ["CONFIG_PATH"] = "/etc/mail-quota-report/config.yaml"
    try:
        runner = _RecordingRunner()
        DoveadmTcpCollector(_cfg(), runner=runner).collect(["a@example.org"])
        _, kwargs = runner.calls[0]
        assert "CONFIG_FILE" not in kwargs["env"]
        assert "CONFIG_PATH" not in kwargs["env"]
    finally:
        os.environ.pop("CONFIG_FILE", None)
        os.environ.pop("CONFIG_PATH", None)


def test_password_via_client_config():
    """PW per Temp-Client-Config (-c): nie argv, nie ENV, Datei gelöscht."""
    runner = _RecordingRunner()
    result = DoveadmTcpCollector(_cfg(), runner=runner).collect(["a@example.org"])
    cmd, kwargs = runner.calls[0]
    assert "geheim" not in " ".join(cmd), "PW nie in argv"
    assert "geheim" not in str(kwargs.get("env", {})), "PW nie im ENV"
    assert cmd[1] == "-c" and os.path.basename(cmd[2]).startswith("mqr-doveadm-")
    assert len(result.records) == 1
    assert not os.path.exists(cmd[2]), "Temp-Config muss gelöscht sein"
