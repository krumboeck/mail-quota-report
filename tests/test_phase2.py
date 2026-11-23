"""Unit-Tests Phase 2: Nd-Intervall-Scheduler, Leer-Report, Healthcheck."""

from __future__ import annotations

import os
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mail_quota_report.scheduler import (
    Schedule,
    ScheduleError,
    _INTERVAL_ANCHOR,
    next_run,
    parse_schedule,
)

TZ = ZoneInfo("Europe/Vienna")


# --- Intervall-Parsing --------------------------------------------------------

@pytest.mark.parametrize("raw,seconds", [
    ("12h", 12 * 3600),
    ("30m", 30 * 60),
    ("2d", 2 * 86400),
    ("45m12h", 12 * 3600 + 45 * 60),
    ("12h45m", 12 * 3600 + 45 * 60),   # Reihenfolge egal
    ("1d2h3m", 86400 + 2 * 3600 + 3 * 60),
    ("1H", 3600),                       # case-insensitiv
])
def test_parse_interval_valid(raw, seconds):
    s = parse_schedule(raw)
    assert s.interval is not None
    assert s.interval.total_seconds() == seconds
    assert s.cron is None


@pytest.mark.parametrize("bad", [
    "0h", "0m",           # 0-Werte
    "12h12h",              # Doppel-Token
    "12x",                 # unbekannte Einheit
    "12",                  # Zahl ohne Einheit
    "h", "m",              # Einheit ohne Zahl
    "12h30",               # Token ohne Einheit am Ende
    "12h-30m",             # Müllzeichen
])
def test_parse_interval_invalid(bad):
    with pytest.raises(ScheduleError):
        parse_schedule(bad)


def test_parse_schedule_aliases_still_work():
    assert parse_schedule("weekly").cron == "0 7 * * 1"
    assert parse_schedule("daily").cron == "0 7 * * *"


def test_parse_schedule_cron_still_works():
    s = parse_schedule("30 8 * * 5")
    assert s.cron == "30 8 * * 5"
    assert s.interval is None


def test_parse_schedule_empty_fail_closed():
    with pytest.raises(ScheduleError, match="SCHEDULE fehlt"):
        parse_schedule("")


# --- Intervall-Berechnung: exakt, kein Drift ------------------------------------

def test_next_run_interval_exact_raster():
    # 12h-Raster ab Anker: jetzt liegt zwischen zwei Rasterpunkten
    s = parse_schedule("12h")
    now = datetime(2026, 11, 6, 15, 30, tzinfo=TZ)
    nxt = next_run(s, now=now, _tz=TZ)
    # Rasterpunkte: ... 12:00, 24:00 — nächster ist heute 24:00 (= 00:00)
    assert (nxt.hour, nxt.minute) == (0, 0)
    assert nxt.day == 7  # 24:00 = nächster Tag 00:00
    # Exakt auf dem Raster:
    delta_to_anchor = (nxt - _INTERVAL_ANCHOR.replace(tzinfo=TZ)).total_seconds()
    assert delta_to_anchor % (12 * 3600) == 0


def test_next_run_interval_strictly_after_now():
    s = parse_schedule("30m")
    now = datetime(2026, 11, 6, 15, 0, 0, tzinfo=TZ)  # exakt AUF einem Rasterpunkt
    nxt = next_run(s, now=now, _tz=TZ)
    assert nxt > now
    assert (nxt - now).total_seconds() == 30 * 60


def test_next_run_interval_no_drift_on_restart():
    # Restart-Sicherheit: von zwei verschiedenen "jetzt"-Zeitpunkten
    # innerhalb desselben Rasterfensters ergibt sich derselbe nächste Punkt
    s = parse_schedule("6h")
    a = next_run(s, now=datetime(2026, 11, 6, 7, 0, tzinfo=TZ), _tz=TZ)
    b = next_run(s, now=datetime(2026, 11, 6, 11, 59, tzinfo=TZ), _tz=TZ)
    assert a == b


def test_next_run_interval_sequence():
    # Aufeinanderfolgende Berechnungen halten exakt den Abstand
    s = parse_schedule("45m")
    now = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
    first = next_run(s, now=now, _tz=TZ)
    second = next_run(s, now=first, _tz=TZ)
    assert (second - first).total_seconds() == 45 * 60


def test_next_run_interval_describe():
    assert "12" in parse_schedule("12h").describe()
    assert "30" in parse_schedule("30m").describe()


def test_next_run_combined_interval():
    s = parse_schedule("45m12h")
    now = datetime(2026, 11, 6, 15, 0, tzinfo=TZ)
    nxt = next_run(s, now=now, _tz=TZ)
    step = 12 * 3600 + 45 * 60
    delta = (nxt - _INTERVAL_ANCHOR.replace(tzinfo=TZ)).total_seconds()
    assert delta % step == 0
    assert nxt > now


# --- Leer-Report (beide Formate) -------------------------------------------------

def _render_with_rows(rows, mode="all", threshold_desc=""):
    from mail_quota_report.report import ReportData
    from mail_quota_report.template import render_message

    data = ReportData(
        deployment="t", date="2026-11-06", rows=rows, n_total=len(rows),
        warn_percent=80, critical_percent=95, mode=mode,
        threshold_desc=threshold_desc,
    )

    class _MsgCfg:
        format = "both"
        template_dir = None
        templates = {"subject": "subject.txt", "text": "report.txt.j2",
                     "html": "report.html.j2"}

    return render_message(data, _MsgCfg())


def test_empty_report_text_friendly_message():
    msg = _render_with_rows([])
    assert "Keine Mailboxen im Bericht" in msg.text_body
    assert "kein Handlungsbedarf" in msg.text_body
    # Keine leere Tabellenstruktur
    assert "|" not in msg.text_body or "von" not in msg.text_body


def test_empty_report_html_friendly_message():
    msg = _render_with_rows([])
    assert "Keine Mailboxen im Bericht" in msg.html_body
    assert "kein Handlungsbedarf" in msg.html_body
    # Kein leeres <table>-Gerüst ohne Zeilen
    assert "<table>" not in msg.html_body


def test_empty_report_below_threshold_wording():
    msg = _render_with_rows([], mode="below_threshold",
                            threshold_desc="frei < 20 % des Limits")
    assert "unter dem Schwellwert" in msg.text_body
    assert "frei < 20 % des Limits" in msg.text_body
    assert "unter dem Schwellwert" in msg.html_body


def test_nonempty_report_unchanged():
    from mail_quota_report.report import ReportRow
    row = ReportRow(mailbox="a@x", name="B, A", used_kib=8_000_000,
                    limit_kib=10_000_000, percent=80.0, status="WARNUNG",
                    free_kib=2_000_000)
    msg = _render_with_rows([row])
    assert "B, A" in msg.text_body
    assert "<table>" in msg.html_body
    assert "Keine Mailboxen" not in msg.text_body


# --- Healthcheck-Script -----------------------------------------------------------

def _run_healthcheck(env, heartbeat_file):
    """scripts/healthcheck.py als Subprozess mit gesetztem ENV ausführen."""
    script = str(Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py")
    import subprocess
    env_full = dict(os.environ)
    env_full.update(env)
    proc = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, env=env_full, timeout=30,
    )
    return proc.returncode, proc.stdout.strip()


def test_healthcheck_healthy(tmp_path):
    hb = tmp_path / "last-run"
    hb.write_text(datetime.now(TZ).isoformat(), encoding="utf-8")
    rc, out = _run_healthcheck(
        {"HEARTBEAT_FILE": str(hb), "HEALTHCHECK_MAX_AGE_H": "26"}, hb)
    assert rc == 0
    assert "HEALTHY" in out


def test_healthcheck_missing_file(tmp_path):
    rc, out = _run_healthcheck(
        {"HEARTBEAT_FILE": str(tmp_path / "gibt-es-nicht"),
         "HEALTHCHECK_MAX_AGE_H": "26"}, None)
    assert rc == 1
    assert "fehlt" in out


def test_healthcheck_too_old(tmp_path):
    hb = tmp_path / "last-run"
    old = datetime.now(TZ) - timedelta(hours=30)
    # mtime alt setzen: Datei anlegen und Zeitstempel zurückdrehen
    hb.write_text(old.isoformat(), encoding="utf-8")
    import time as _time
    stamp = old.timestamp()
    os.utime(hb, (stamp, stamp))
    rc, out = _run_healthcheck(
        {"HEARTBEAT_FILE": str(hb), "HEALTHCHECK_MAX_AGE_H": "26"}, hb)
    assert rc == 1
    assert "UNHEALTHY" in out


def test_healthcheck_max_age_env_scales(tmp_path):
    hb = tmp_path / "last-run"
    hb.write_text(datetime.now(TZ).isoformat(), encoding="utf-8")
    # Gleiche Datei, höhere Grenze (weekly → 170 h) → gesund
    rc, _ = _run_healthcheck(
        {"HEARTBEAT_FILE": str(hb), "HEALTHCHECK_MAX_AGE_H": "170"}, hb)
    assert rc == 0
