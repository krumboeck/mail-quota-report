"""Unit-Tests report.py + template.py: Aufbereitung, Zahlen, Rendering."""

from __future__ import annotations

import pytest

from mail_quota_report.doveadm import QuotaRecord
from mail_quota_report.filter import STATUS_OK, STATUS_WARN
from mail_quota_report.ldap_source import LdapEntry
from mail_quota_report.report import (
    ReportData,
    ReportRow,
    build_rows,
    format_kib,
    format_percent,
    format_percent_exact,
    group_rows_by_tenant,
    sort_rows,
    summarize,
)
from mail_quota_report.template import (
    render_message,
    render_rows,
    render_subject,
    status_class,
    status_label,
)


# CANNED Daten — frei erfunden, NIE Produktivdaten (DSGVO).

# --- Zahlenformate ------------------------------------------------------------

@pytest.mark.parametrize("kib,expected", [
    (0, "0 kB"),
    (512, "512 kB"),
    (999_500, "999,50 MB"),             # MB-Bereich; ab 1 GB → GB
    (1_234_567, "1,23 GB"),              # ab 1 GB → GB mit zwei Nachkommastellen
    (51_200, "51,20 MB"),
    (9_820_254, "9,82 GB"),
    (10_000_000, "10,00 GB"),
    (500_000, "500,00 MB"),
])
def test_format_kib(kib, expected):
    assert format_kib(kib) == expected


def test_format_kib_negative():
    assert format_kib(-2_500_000) == "-2,50 GB"


@pytest.mark.parametrize("pct,expected", [
    (98.0, "98 %"),
    (80.0, "80 %"),
    (12.3, "12 %"),
    (None, "—"),
])
def test_format_percent(pct, expected):
    assert format_percent(pct) == expected


def test_format_percent_exact():
    assert format_percent_exact(98.4) == "98,4 %"
    assert format_percent_exact(None) == "—"


# --- build_rows: Vereinung + Sonderfälle ---------------------------------------

def _entry(dn="uid=anna,cd=x", mail="anna@example.org", last="Beispiel",
           first="Anna", aliases=None, tenant="example.org"):
    return LdapEntry(dn=dn, username="anna", mail_primary=mail,
                     first_name=first, last_name=last,
                     mail_aliases=aliases or [], tenant=tenant)


def test_build_rows_happy_path():
    entries = {"anna@example.org": _entry()}
    records = {"anna@example.org": QuotaRecord("anna@example.org", 8_000_000, 10_000_000)}
    rows = build_rows(["anna@example.org"], records, entries,
                      warn_percent=80, critical_percent=95)
    assert len(rows) == 1
    r = rows[0]
    assert r.name == "Beispiel, Anna"
    assert r.percent == pytest.approx(80.0)
    assert r.status == STATUS_WARN
    assert r.free_kib == 2_000_000


def test_build_rows_no_ldap_entry():
    records = {"ghost@example.org": QuotaRecord("ghost@example.org", 100, 1000)}
    rows = build_rows(["ghost@example.org"], records, {},
                      warn_percent=80, critical_percent=95)
    r = rows[0]
    assert r.status == "KEIN_LDAP"
    assert r.used_kib == 100  # Werte trotzdem zeigen


def test_build_rows_no_quota_record():
    entries = {"anna@example.org": _entry()}
    rows = build_rows(["anna@example.org"], {}, entries,
                      warn_percent=80, critical_percent=95)
    assert rows[0].status == "KEIN_QUOTA"
    assert rows[0].error == "kein Quota-Datensatz"


def test_build_rows_limit_zero_no_percent():
    entries = {"svc@example.org": _entry(mail="svc@example.org")}
    records = {"svc@example.org": QuotaRecord("svc@example.org", 0, 0)}
    rows = build_rows(["svc@example.org"], records, entries,
                      warn_percent=80, critical_percent=95)
    assert rows[0].percent is None
    assert rows[0].status == "LIMIT_0"


def test_build_rows_hide_zero_limit():
    entries = {"svc@example.org": _entry(mail="svc@example.org")}
    records = {"svc@example.org": QuotaRecord("svc@example.org", 0, 0)}
    rows = build_rows(["svc@example.org"], records, entries,
                      warn_percent=80, critical_percent=95,
                      hide_zero_limit=True)
    assert rows == []


def test_build_rows_tenant_grouping():
    entries = {"a@x.example": _entry(dn="uid=a,cd=x.example", mail="a@x.example",
                                     tenant="x.example")}
    records = {"a@x.example": QuotaRecord("a@x.example", 1, 100)}
    rows = build_rows(["a@x.example"], records, entries,
                      warn_percent=80, critical_percent=95, group_by_tenant=True,
                      tenant_of=lambda e: e.tenant)
    assert rows[0].tenant == "x.example"


def test_build_rows_no_ldap_and_no_quota():
    rows = build_rows(["x@example.org"], {}, {},
                      warn_percent=80, critical_percent=95)
    # KEIN_LDAP gewinnt über KEIN_QUOTA (Verzeichnis ist die primäre Quelle)
    assert rows[0].status == "KEIN_LDAP"


# --- Sortierung ----------------------------------------------------------------

def _row(mailbox, percent, limit=1000, name=""):
    from mail_quota_report.filter import classify
    status = classify(percent, 80, 95) if percent is not None else "LIMIT_0"
    r = ReportRow(mailbox=mailbox, name=name, used_kib=int(limit * percent / 100),
                  limit_kib=limit, percent=percent, status=status)
    return r


def test_sort_percent_desc_default():
    rows = [_row("a@x", 10), _row("b@x", 95), _row("c@x", 50)]
    out = sort_rows(rows)
    assert [r.mailbox for r in out] == ["b@x", "c@x", "a@x"]


def test_sort_percent_none_last():
    rows = [_row("a@x", 10), ReportRow(mailbox="z@x", percent=None, limit_kib=0)]
    out = sort_rows(rows)
    assert out[-1].mailbox == "z@x"


def test_sort_name_asc():
    rows = [_row("a@x", 10, name="Zebra"), _row("b@x", 90, name="Anton")]
    out = sort_rows(rows, "name_asc")
    assert [r.name for r in out] == ["Anton", "Zebra"]


def test_sort_limit_desc():
    rows = [_row("a@x", 10, limit=100), _row("b@x", 10, limit=500)]
    out = sort_rows(rows, "limit_desc")
    assert [r.mailbox for r in out] == ["b@x", "a@x"]


# --- Zusammenfassung ------------------------------------------------------------

def test_summarize_counts():
    rows = [
        _row("a@x", 96), _row("b@x", 85), _row("c@x", 50),
        ReportRow(mailbox="d@x", status="KEIN_LDAP"),
        ReportRow(mailbox="e@x", status="KEIN_QUOTA"),
    ]
    counts = summarize(rows)
    assert counts == {"n": 5, "warn": 1, "crit": 1, "no_ldap": 1, "no_quota": 1}


def test_group_rows_by_tenant():
    rows = [
        ReportRow(mailbox="a@x", tenant="x.example"),
        ReportRow(mailbox="b@x", tenant="x.example"),
        ReportRow(mailbox="c@y", tenant="y.example"),
    ]
    groups = group_rows_by_tenant(rows)
    assert set(groups) == {"x.example", "y.example"}
    assert len(groups["x.example"]) == 2


# --- Template-Rendering -----------------------------------------------------------

def _data(rows=None, n=2, warn=1, crit=0):
    return ReportData(
        deployment="universalnet", date="2026-11-06",
        rows=rows or [], n_total=n, n_no_ldap=0, n_no_quota=0,
        n_warn=warn, n_crit=crit, warn_percent=80, critical_percent=95,
    )


class _MsgCfg:
    format = "both"
    template_dir = None
    templates = {"subject": "subject.txt", "text": "report.txt.j2",
                 "html": "report.html.j2"}


def test_render_rows_fields():
    r = ReportRow(mailbox="a@x", name="Beispiel, Anna", mail_primary="a@x.example",
                  mail_aliases=["aa@x.example"], department="IT", company="Ex",
                  used_kib=9_820_254, limit_kib=10_000_000, free_kib=179_746,
                  percent=98.0, status="KRITISCH")
    out = render_rows([r])
    assert out[0]["used"] == "9,82 GB"
    assert out[0]["percent"] == "98,0 %"
    assert out[0]["status"] == "KRITISCH"
    assert out[0]["status_class"] == "critical"
    assert out[0]["free"] == "179,75 MB"


def test_render_subject_placeholders():
    subj = render_subject(
        "Quota-Report {deployment} {date}: {n} Mailboxen, {warn} Warnung, {crit} kritisch",
        _data())
    assert subj == ("Quota-Report universalnet 2026-11-06: 2 Mailboxen, "
                    "1 Warnung, 0 kritisch")


def test_render_message_both_bodies():
    msg = render_message(_data(), _MsgCfg())
    assert msg.text_body and msg.html_body
    assert "Quota-Report universalnet" in msg.text_body
    assert "<html" in msg.html_body
    assert "text/html" not in msg.text_body  # Text-Teil ist Plaintext


def test_render_message_text_only():
    class _T(_MsgCfg):
        format = "text"
    msg = render_message(_data(), _T())
    assert msg.text_body and not msg.html_body


def test_render_message_html_only():
    class _T(_MsgCfg):
        format = "html"
    msg = render_message(_data(), _T())
    assert msg.html_body and not msg.text_body


def test_render_message_german_numbers_and_highlight():
    row = ReportRow(mailbox="a@x", name="B, A", used_kib=9_820_254,
                    limit_kib=10_000_000, percent=98.0, status="KRITISCH",
                    free_kib=179_746)
    data = _data(rows=[row], n=1, warn=0, crit=1)
    msg = render_message(data, _MsgCfg())
    assert "9,82 GB" in msg.text_body
    assert "98,0 %" in msg.text_body
    assert "KRITISCH" in msg.text_body
    assert 'class="critical"' in msg.html_body
    assert "9,82 GB" in msg.html_body


def test_render_message_custom_dir_fallback(tmp_path):
    # template_dir ohne die Dateien → Fallback auf mitgelieferte Standard-Templates
    class _T(_MsgCfg):
        template_dir = str(tmp_path)
    msg = render_message(_data(), _T())
    assert msg.text_body and "Quota-Report" in msg.text_body


def test_render_message_threshold_mode():
    row = ReportRow(mailbox="a@x", name="B, A", used_kib=9_900_000,
                    limit_kib=10_000_000, percent=99.0, status="KRITISCH")
    data = _data(rows=[row], n=1, warn=0, crit=1)
    data.mode = "below_threshold"
    data.threshold_desc = "frei < 20 % des Limits"
    msg = render_message(data, _MsgCfg())
    assert "frei < 20 % des Limits" in msg.text_body


def test_status_labels():
    assert status_label("OK") == "OK"
    assert status_label("WARNUNG") == "WARNUNG"
    assert status_label("KEIN_LDAP") == "kein LDAP-Eintrag"
    assert status_class("WARNUNG") == "warn"
