"""Unit-Tests doveadm.py: Parsing (canned table/flow-Ausgaben), Batch-Collect."""

from __future__ import annotations

import subprocess

import pytest

from mail_quota_report.doveadm import (
    CollectResult,
    QuotaRecord,
    collect_ssh,
    parse_quota_output,
)

# CANNED Daten — frei erfunden, NIE Produktivdaten (DSGVO).
# Beide Formatter sind Ausgabevarianten von doveadm 2.4 (table/flow).

TABLE_OUTPUT = """\
Quota name Type Value Limit %=
STORAGE STORAGE 9820254 (k) 10000000 (k) 98
"""

TABLE_OUTPUT_NO_LIMIT = """\
Quota name Type Type Value Limit %=
STORAGE STORAGE 9820254 (k) - (k) 98
"""

FLOW_OUTPUT = "Quota name: User quota\nSTORAGE=9820254/10000000/98/%\n"

FLOW_OUTPUT_NO_LIMIT = "STORAGE=9820254/?/98/%\n"

GARBAGE = "irgendein fehler\nweitere zeile\n"


# --- Parsing -----------------------------------------------------------------

def test_parse_table_formatter():
    rec = parse_quota_output(TABLE_OUTPUT, "user@example.org", "host1")
    assert rec is not None
    assert rec.mailbox == "user@example.org"
    assert rec.used_kib == 9_820_254
    assert rec.limit_kib == 10_000_000
    assert rec.source_host == "host1"


def test_parse_table_without_limit():
    rec = parse_quota_output(TABLE_OUTPUT_NO_LIMIT, "user@example.org")
    assert rec is not None
    assert rec.used_kib == 9_820_254
    assert rec.limit_kib == -1   # kein Limit geliefert


def test_parse_flow_formatter():
    rec = parse_quota_output(FLOW_OUTPUT, "user@example.org")
    assert rec is not None
    assert rec.used_kib == 9_820_254
    assert rec.limit_kib == 10_000_000


def test_parse_flow_without_limit():
    rec = parse_quota_output(FLOW_OUTPUT_NO_LIMIT, "user@example.org")
    assert rec is not None
    assert rec.limit_kib == -1


def test_parse_garbage_returns_none():
    assert parse_quota_output(GARBAGE, "user@example.org") is None


def test_parse_empty_returns_none():
    assert parse_quota_output("", "user@example.org") is None


# --- Batch-Collect über EINE SSH-Session (Wrapper-Weg) ------------------------

class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRunner:
    """Zeichnet ssh-Aufrufe auf; liefert canned Wrapper-Antworten."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.responses.pop(0)


def _ssh_cfg():
    from mail_quota_report.config import SSHConfig
    return SSHConfig(
        hosts=("mailhost.example",),
        container_filter="mail-imap-1",
        user="root",
        key_file="/run/secrets/mqr_ssh_key",
    )


def test_collect_wrapper_batch_success():
    # Wrapper antwortet tab-delimited je Mailbox
    stdout = "a@example.org\t0\t100\t1000\nb@example.org\t0\t900\t1000\n"
    runner = _FakeRunner([_FakeCompleted(0, stdout=stdout)])
    result = collect_ssh(["a@example.org", "b@example.org"], _ssh_cfg(), runner=runner)
    assert set(result.records) == {"a@example.org", "b@example.org"}
    assert result.records["a@example.org"].used_kib == 100
    assert result.records["b@example.org"].limit_kib == 1000
    assert result.errors == {}
    # EINE SSH-Session: genau ein ssh-Aufruf für den Batch
    assert len(runner.calls) == 1
    cmd = runner.calls[0][0]
    assert cmd[0] == "ssh"
    assert "mqr-doveadm-wrapper" in cmd
    # Mailboxen über stdin
    assert runner.calls[0][1]["input"] == "a@example.org\nb@example.org\n"


def test_collect_wrapper_single_error_no_abort():
    # Eine Mailbox fehlerhaft, andere erfolgreich — kein Abbruch
    stdout = ("a@example.org\t0\t100\t1000\n"
              "b@example.org\t1\tdoveadm: unknown user\n\t\n")
    runner = _FakeRunner([_FakeCompleted(0, stdout=stdout)])
    result = collect_ssh(["a@example.org", "b@example.org"], _ssh_cfg(), runner=runner)
    assert "a@example.org" in result.records
    assert "b@example.org" in result.errors
    assert "unknown user" in result.errors["b@example.org"]


def test_collect_fallback_per_mailbox():
    # Wrapper fehlt (rc!=0) → Fallback: doveadm je Mailbox über ControlMaster
    responses = [
        _FakeCompleted(127, stderr="wrapper nicht gefunden"),  # wrapper probe
        _FakeCompleted(0),                                     # master true
        _FakeCompleted(0, stdout=TABLE_OUTPUT),                # mb 1
        _FakeCompleted(0, stdout=TABLE_OUTPUT),                # mb 2
        _FakeCompleted(0),                                     # master exit
    ]
    runner = _FakeRunner(responses)
    result = collect_ssh(["a@example.org", "b@example.org"], _ssh_cfg(), runner=runner)
    assert set(result.records) == {"a@example.org", "b@example.org"}
    assert all(r.used_kib == 9_820_254 for r in result.records.values())
    # Fallback: je Mailbox ein doveadm-Aufruf (docker exec im Kommando);
    # Wrapper-Probe und Master-Steuerung zählen nicht mit.
    dove_calls = [c for c, _ in runner.calls
                  if "doveadm" in " ".join(c)
                  and "mqr-doveadm-wrapper" not in " ".join(c)
                  and "-O" not in c and "true" not in c]
    assert len(dove_calls) == 2


def test_collect_fallback_error_per_mailbox():
    responses = [
        _FakeCompleted(127, stderr="wrapper fehlt"),
        _FakeCompleted(0),
        _FakeCompleted(0, stdout=TABLE_OUTPUT),
        _FakeCompleted(1, stderr="doveadm(user@example.org): Unknown user"),
        _FakeCompleted(0),
    ]
    runner = _FakeRunner(responses)
    result = collect_ssh(["a@example.org", "user@example.org"], _ssh_cfg(), runner=runner)
    assert "a@example.org" in result.records
    assert "user@example.org" in result.errors
    assert "Unknown user" in result.errors["user@example.org"]


def test_collect_ssh_hardening_options():
    stdout = "a@example.org\t0\t1\t10\n"
    runner = _FakeRunner([_FakeCompleted(0, stdout=stdout)])
    collect_ssh(["a@example.org"], _ssh_cfg(), runner=runner)
    cmd = runner.calls[0][0]
    joined = " ".join(cmd)
    # Härtung: kein Agent/X11, BatchMode, accept-new (TOFU)
    assert "BatchMode=yes" in joined
    assert "StrictHostKeyChecking=accept-new" in joined
    assert "PasswordAuthentication=no" in joined
    assert "ForwardAgent=no" in joined
    assert "ForwardX11=no" in joined


def test_collect_empty_mailbox_list():
    runner = _FakeRunner([])
    result = collect_ssh([], _ssh_cfg(), runner=runner)
    assert result.records == {}
    assert result.errors == {}
    assert runner.calls == []


def test_collect_timeout_is_error_not_abort():
    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "mqr-doveadm-wrapper" in joined:
            return _FakeCompleted(127, stderr="fehlt")
        if "true" in joined or "-O" in joined:
            return _FakeCompleted(0)
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)
    result = collect_ssh(["a@example.org"], _ssh_cfg(), runner=runner)
    assert "a@example.org" in result.errors
    assert "Timeout" in result.errors["a@example.org"]
