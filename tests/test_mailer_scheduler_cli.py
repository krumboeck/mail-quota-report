"""Unit-Tests mailer.py + scheduler.py + cli.py (Modi, Exit-Codes)."""

from __future__ import annotations

import pytest

from mail_quota_report.mailer import MailError, build_message, send_message
from mail_quota_report.scheduler import (
    ScheduleError,
    next_run,
    parse_schedule,
    write_heartbeat,
    heartbeat_age_hours,
)


# CANNED Daten — frei erfunden, NIE Produktivdaten (DSGVO).

# --- Mail-Aufbau ---------------------------------------------------------------

class _SmtpCfg:
    host = "smtp.example"
    port = 25
    starttls = False
    sender = "quota-report@example.org"
    password = None


def test_build_message_text():
    msg = build_message("from@example.org", ["to@example.org"], "Betreff",
                        "Text-Body", None)
    assert msg["Subject"] == "Betreff"
    assert msg.get_content_type() == "text/plain"
    assert "Text-Body" in msg.get_payload(decode=True).decode()


def test_build_message_html():
    msg = build_message("from@example.org", ["to@example.org"], "B",
                        None, "<p>html</p>")
    assert msg.get_content_type() == "text/html"


def test_build_message_both_multipart_alternative():
    msg = build_message("from@example.org", ["to@example.org"], "B",
                        "Text", "<p>html</p>")
    assert msg.is_multipart()
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert len(parts) == 2
    # Reihenfolge: text zuerst, html zuletzt (RFC 2046)
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"


def test_build_message_multiple_recipients():
    msg = build_message("from@example.org", ["a@example.org", "b@example.org"],
                        "B", "Text", None)
    assert "a@example.org" in msg["To"]
    assert "b@example.org" in msg["To"]


def test_build_message_no_bodies_fail():
    with pytest.raises(ValueError):
        build_message("f@x", ["t@x"], "B", None, None)


def test_build_message_utf8_subject_and_body():
    msg = build_message("f@x", ["t@x"], "Quota-Report äöü", "Inhalt mit Ümlauten", None)
    # BodyUTF-8-dekodierbar; Betreff-Kodierung übernimmt die stdlib (RFC 2047)
    body = msg.get_payload(decode=True).decode("utf-8")
    assert "Inhalt mit Ümlauten" in body
    assert msg["Subject"]


# --- Versand (Fake-SMTP) --------------------------------------------------------

class _FakeSmtpOk:
    def __init__(self):
        self.sent = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, pw):
        self.login_args = (user, pw)

    def send_message(self, msg):
        self.sent.append(msg)
        return {}  # keine abgelehnten Empfänger


class _FakeSmtpRefused:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_message(self, msg):
        return {"b@example.org": (550, b"unknown user")}


def test_send_message_ok():
    client = _FakeSmtpOk()
    msg = build_message("f@x", ["t@x"], "B", "Text", None)
    recipients = send_message(_SmtpCfg(), msg, smtp_client_factory=lambda c: client)
    assert client.entered
    assert len(client.sent) == 1
    assert recipients == ["t@x"]


def test_send_message_refused_recipient_raises():
    msg = build_message("f@x", ["a@x", "b@example.org"], "B", "Text", None)
    with pytest.raises(MailError, match="abgelehnt"):
        send_message(_SmtpCfg(), msg, smtp_client_factory=lambda c: _FakeSmtpRefused())


def test_send_message_starttls_and_auth_used():
    class _Cfg(_SmtpCfg):
        starttls = True
        password = "secret"

    client = _FakeSmtpOk()
    msg = build_message("f@x", ["t@x"], "B", "Text", None)
    send_message(_Cfg(), msg, smtp_client_factory=lambda c: client)
    assert client.tls is True
    assert client.login_args == ("quota-report@example.org", "secret")


def test_send_message_no_recipients_fails():
    msg = build_message("f@x", ["t@x"], "B", "Text", None)
    del msg["To"]
    with pytest.raises(MailError, match="Empfänger"):
        send_message(_SmtpCfg(), msg, smtp_client_factory=lambda c: _FakeSmtpOk())


# --- Scheduler -------------------------------------------------------------------

def test_parse_schedule_aliases():
    assert parse_schedule("weekly").cron == "0 7 * * 1"
    assert parse_schedule("daily").cron == "0 7 * * *"
    assert parse_schedule("monthly").cron == "0 7 1 * *"
    assert parse_schedule("DAILY").cron == "0 7 * * *"


def test_parse_schedule_cron_syntax():
    s = parse_schedule("30 8 * * 5")
    assert s.cron == "30 8 * * 5"


@pytest.mark.parametrize("bad", ["", "  ", "bogus", "0 7 * *", "*/x * * * *"])
def test_parse_schedule_invalid(bad):
    with pytest.raises(ScheduleError):
        parse_schedule(bad)


def test_next_run_weekly_from_friday():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    s = parse_schedule("weekly")
    # Freitag 06.11.2026 12:00 → nächster Montag 09.11.2026 07:00
    now = datetime(2026, 11, 6, 12, 0, tzinfo=tz)
    nxt = next_run(s, now=now, _tz=tz)
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 11, 9, 7, 0)
    assert nxt.isoweekday() == 1


def test_next_run_daily_same_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    s = parse_schedule("daily")
    now = datetime(2026, 11, 6, 6, 0, tzinfo=tz)
    nxt = next_run(s, now=now, _tz=tz)
    assert (nxt.day, nxt.hour, nxt.minute) == (6, 7, 0)


def test_next_run_daily_already_passed():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    s = parse_schedule("daily")
    now = datetime(2026, 11, 6, 8, 0, tzinfo=tz)
    nxt = next_run(s, now=now, _tz=tz)
    assert (nxt.day, nxt.hour) == (7, 7)


def test_next_run_no_drift():
    # Je Lauf neu berechnet: zweite Berechnung von der ersten aus ergibt
    # nie denselben Zeitpunkt (immer strikt danach)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    s = parse_schedule("daily")
    now = datetime(2026, 11, 6, 7, 30, tzinfo=tz)
    first = next_run(s, now=now, _tz=tz)
    second = next_run(s, now=first, _tz=tz)
    assert second > first


# --- Heartbeat --------------------------------------------------------------------

def test_heartbeat_roundtrip(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    p = tmp_path / "heartbeat"
    now = datetime(2026, 11, 6, 7, 0, tzinfo=tz)
    write_heartbeat(str(p), now=now)
    assert p.exists()
    age = heartbeat_age_hours(str(p), now=now + timedelta(hours=2))
    assert age == pytest.approx(2.0)


# --- CLI: Exit-Codes ----------------------------------------------------------------

class _Args:
    config = None
    selftest = False
    dry_run = False
    once = False


def test_cli_without_config_exit_1(monkeypatch):
    from mail_quota_report import cli
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    rc = cli.main(["--selftest"])  # Config fehlt → fail-closed
    assert rc == 1


def test_cli_selftest_ok(config_env, monkeypatch, capsys):
    from mail_quota_report import cli
    monkeypatch.setenv("CONFIG_FILE", str(config_env))
    rc = cli.main(["--selftest"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert "universalnet" in out


def test_cli_dry_run_prints_report(config_env, monkeypatch, capsys):
    from mail_quota_report import cli

    monkeypatch.setenv("CONFIG_FILE", str(config_env))

    # Alle Außenkontakte faken: LDAP + SSH + kein Mailversand im dry-run
    from mail_quota_report import ldap_source, doveadm

    class _FakeConn:
        def search_s(self, base, scope, filterstr, attrlist=None):
            return [
                ("uid=anna,ou=persons,cd=example.org,dc=x",
                 {"mail": [b"anna@example.org"], "sn": [b"Beispiel"],
                  "givenName": [b"Anna"], "uid": [b"anna"]}),
            ]
        def unbind_s(self):
            pass

    monkeypatch.setattr(ldap_source.LdapSource, "_connect",
                        lambda self: _FakeConn())

    class _FakeCompleted:
        def __init__(self):
            self.returncode = 0
            self.stdout = "anna@example.org\t0\t8000000\t10000000\n"
            self.stderr = ""

    # cli.py wählt den Collector über make_collector — TCP-Collector patchen
    from mail_quota_report import doveadm as doveadm_mod

    class _FakeTcpCollector:
        def __init__(self, cfg):
            self.cfg = cfg

        def collect(self, mailboxes):
            res = doveadm_mod.CollectResult()
            res.records["anna@example.org"] = doveadm_mod.QuotaRecord(
                "anna@example.org", 8_000_000, 10_000_000)
            return res

    monkeypatch.setattr(doveadm_mod, "DoveadmTcpCollector", _FakeTcpCollector)

    rc = cli.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "anna@example.org" in out
    assert "WARNUNG" in out
    assert "8,00 GB" in out


def test_cli_version():
    from mail_quota_report import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0


def test_cli_schedule_env_fail_closed(config_env, monkeypatch):
    from mail_quota_report import cli
    monkeypatch.setenv("CONFIG_FILE", str(config_env))
    monkeypatch.delenv("SCHEDULE", raising=False)
    rc = cli.main([])  # Scheduler-Modus ohne SCHEDULE → Exit 1
    assert rc == 1


# --- Scheduler-Loop (Fix: Spam-Lauf + Healthcheck-Kill) ------------------------

from datetime import datetime as _dt, timedelta


class _FakeClock:
    """Fake-Uhr für _scheduler_loop: now() liefert die simulierte Zeit,
    sleep(s) schaltet sie weiter (kein echtes Warten)."""

    def __init__(self, start):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t = self.t + timedelta(seconds=seconds)


class _StopLoop(BaseException):
    """Bricht die Endlosschleife nach N Läufen ab (nur Tests).

    Bewusst BaseException: die Loop fängt Exception ab (Lauf-Fehler-
    Toleranz) — ein Test-Abbruch darf nicht als Lauf-Fehler geschluckt
    werden, sonst läuft die Schleife endlos."""


def _make_loop(runs, exit_after):
    """run()-Fake für _scheduler_loop: zählt Läufe, bricht nach dem
    exit_after-ten Lauf per _StopLoop ab."""
    from mail_quota_report import cli

    def fake_run():
        runs.append(1)
        if len(runs) >= exit_after:
            raise _StopLoop
        return cli.EXIT_OK

    return fake_run


class _AnyCfg:
    """Dummy-Config — die Loop reicht cfg nur an run_report durch."""


def test_scheduler_loop_no_run_before_due_time(monkeypatch, tmp_path):
    """Feldbefund 1 aus dem Pilot-Deployment: Start 23:56, SCHEDULE 0 7 * * * —
    nach dem Stundenschlaf (00:56) darf KEIN Lauf kommen; erster Lauf
    erst zur fälligen Zeit 07:00 am Folgetag."""
    from zoneinfo import ZoneInfo
    from mail_quota_report import cli
    from mail_quota_report.scheduler import parse_schedule

    tz = ZoneInfo("Europe/Vienna")
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "hb"))
    clock = _FakeClock(_dt(2026, 11, 6, 23, 56, tzinfo=tz))
    schedule = parse_schedule("0 7 * * *")  # täglich 07:00
    runs = []
    fake_run = _make_loop(runs, exit_after=1)

    with pytest.raises(_StopLoop):
        cli._scheduler_loop(_AnyCfg(), schedule,
                            sleep=clock.sleep, now=clock.now, run=fake_run)

    # Genau EIN Lauf um 07:00 am 07.11. — nicht um 00:56 (der alte Code
    # lief nach dem ersten Stundenschlaf blind in run_report: Spam-Lauf
    # exakt 3600 s nach Start).
    assert len(runs) == 1
    assert (clock.t.day, clock.t.hour, clock.t.minute) == (7, 7, 0)


def test_scheduler_loop_multiple_hour_sleeps_without_run(monkeypatch, tmp_path):
    """Ziel weit entfernt (weekly): mehrere Stundenschlafen à 3600 s,
    zwischen den Schläfen KEIN Lauf, erst zum fälligen Montag 07:00."""
    from zoneinfo import ZoneInfo
    from mail_quota_report import cli
    from mail_quota_report.scheduler import parse_schedule

    tz = ZoneInfo("Europe/Vienna")
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "hb"))
    # Freitag 06.11. 12:00 → nächster Lauf Montag 09.11. 07:00 (= 67 h)
    clock = _FakeClock(_dt(2026, 11, 6, 12, 0, tzinfo=tz))
    schedule = parse_schedule("weekly")
    runs = []
    fake_run = _make_loop(runs, exit_after=1)

    with pytest.raises(_StopLoop):
        cli._scheduler_loop(_AnyCfg(), schedule,
                            sleep=clock.sleep, now=clock.now, run=fake_run)

    assert len(runs) == 1
    assert (clock.t.isoweekday(), clock.t.hour, clock.t.minute) == (1, 7, 0)
    # Alle Schlaf-Intervalle bis zum Lauf sind Stundenschritte à 3600 s
    # (der 60-s-Schlaf nach dem Lauf erreicht der _StopLoop nie — er
    # wirft bereits in run()).
    assert clock.sleeps
    assert all(s == 3600 for s in clock.sleeps)
    assert 0 < clock.sleeps[-1] <= 3600


def test_scheduler_loop_exactly_one_run_when_sleep_overshoots(monkeypatch, tmp_path):
    """Schlaf springt ÜBER die fällige Zeit (Suspend/NTP-Sprung): genau
    EIN nachgeholter Lauf — kein Doppellauf, kein Warten auf morgen."""
    from zoneinfo import ZoneInfo
    from mail_quota_report import cli
    from mail_quota_report.scheduler import parse_schedule

    tz = ZoneInfo("Europe/Vienna")
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "hb"))
    clock = _FakeClock(_dt(2026, 11, 6, 6, 59, 30, tzinfo=tz))

    def jumping_sleep(seconds):
        """Schlaf + 2 h Suspend-Sprung: 07:00 wird überSprungen."""
        clock.sleeps.append(seconds)
        clock.t = clock.t + timedelta(seconds=seconds, hours=2)

    schedule = parse_schedule("daily")
    runs = []
    fake_run = _make_loop(runs, exit_after=1)

    with pytest.raises(_StopLoop):
        cli._scheduler_loop(_AnyCfg(), schedule,
                            sleep=jumping_sleep, now=clock.now, run=fake_run)

    # Genau EIN Lauf, nachgeholt NACH der fälligen Zeit (08:59:30),
    # nicht erst am Folgetag und nicht doppelt.
    assert len(runs) == 1
    assert clock.t >= _dt(2026, 11, 6, 7, 0, tzinfo=tz)
    assert clock.t.day == 6


def test_scheduler_heartbeat_written_at_start(monkeypatch, tmp_path):
    """Feldbefund 2 aus dem Pilot-Deployment: Heartbeat existiert direkt nach
    Scheduler-Start, VOR jedem Lauf — sonst killt der Healthcheck frische
    Container (Interval 180 s, Retries 2), bevor der erste Lauf fällig
    ist (bis zu 24 h bei SCHEDULE=0 7 * * *)."""
    from zoneinfo import ZoneInfo
    from mail_quota_report import cli
    from mail_quota_report.scheduler import parse_schedule

    tz = ZoneInfo("Europe/Vienna")
    hb = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hb))
    clock = _FakeClock(_dt(2026, 11, 6, 23, 56, tzinfo=tz))
    schedule = parse_schedule("0 7 * * *")
    runs = []

    def fake_run():
        # Beim ersten (und einzigen) Lauf muss der Start-Heartbeat
        # bereits existieren — der Lauf liegt 7 h nach dem Start.
        assert hb.exists(), "Heartbeat fehlt beim ersten Lauf"
        runs.append(1)
        raise _StopLoop

    assert not hb.exists()  # Vorbedingung: noch nichts geschrieben
    with pytest.raises(_StopLoop):
        cli._scheduler_loop(_AnyCfg(), schedule,
                            sleep=clock.sleep, now=clock.now, run=fake_run)
    assert hb.exists()
    assert len(runs) == 1
