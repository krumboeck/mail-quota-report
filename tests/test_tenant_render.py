"""Pilot-Deployment-Test: Mandanten-Gruppierung im gerenderten Report."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mail_quota_report.report import ReportData, ReportRow
from mail_quota_report.template import render_message


class _MsgCfg:
    format = "both"
    template_dir = None
    templates = {"subject": "subject.txt", "text": "report.txt.j2",
                 "html": "report.html.j2"}


def _data(group_by_tenant, rows):
    return ReportData(
        deployment="t", date="2026-11-06", rows=rows, n_total=len(rows),
        warn_percent=80, critical_percent=95,
        group_by_tenant=group_by_tenant,
    )


def _rows():
    return [
        ReportRow(mailbox="a@x", name="A", mail_primary="a@x.example",
                  tenant="x.example",
                  used_kib=100, limit_kib=1000, percent=10.0, status="OK"),
        ReportRow(mailbox="b@y", name="B", mail_primary="b@y.example",
                  tenant="y.example",
                  used_kib=200, limit_kib=1000, percent=20.0, status="OK"),
    ]


def test_grouped_report_renders_sections():
    msg = render_message(_data(True, _rows()), _MsgCfg())
    assert msg.text_body is not None
    assert msg.html_body is not None
    assert "== Mandant x.example (1 Mailboxen) ==" in msg.text_body
    assert "== Mandant y.example (1 Mailboxen) ==" in msg.text_body
    assert "<h3>Mandant x.example (1 Mailboxen)</h3>" in msg.html_body
    assert "<h3>Mandant y.example (1 Mailboxen)</h3>" in msg.html_body


def test_ungrouped_report_flat():
    msg = render_message(_data(False, _rows()), _MsgCfg())
    assert msg.text_body is not None
    assert msg.html_body is not None
    assert "Mandant x.example (" not in msg.text_body
    assert "<h3>Mandant" not in msg.html_body
    assert "a@x.example" in msg.text_body  # Zeilen trotzdem da
