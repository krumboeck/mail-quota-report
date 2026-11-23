"""cli — Einstieg, Betriebsmodi, Exit-Codes (Phase 1).

Aufgaben:
- argparse: --selftest | --dry-run | --once | --config <pfad> | --version
- Exit-Codes: 0 = Erfolg; 1 = Konfiguration/Fail-closed (fehlende
  Config, fehlende Secrets, ungültiger SCHEDULE, Selftest rot);
  2 = Lauf-Fehler (LDAP/SSH/SMTP nicht erreichbar, Versand abgelehnt)
- --selftest: Config + Secrets + (optional, selftest.network_probes)
  Verbindungsproben ohne Versand; Exit 0/1
- --dry-run: kompletter Lauf OHNE Mailversand — Report nach stdout
- --once: ein Lauf mit Versand (außerhalb des Schedules)
- OHNE Modus-Argument: Scheduler-Loop (SCHEDULE-ENV) — ohne Config
  Exit 1 (fail-closed), damit die Jenkins-smoke-Stage grün wird
- Logging stdout, ein-Zeilen-Summary je Lauf, keine Personendaten
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import __version__
from .config import Config, ConfigError, load_config
from .doveadm import CollectorError, make_collector
from .filter import Threshold, passes_threshold
from .ldap_source import LdapSource
from .mailer import MailError, build_message, send_message
from .report import (
    ReportData,
    build_rows,
    sort_rows,
    summarize,
)
from .scheduler import (
    Schedule,
    ScheduleError,
    get_tz,
    next_run,
    parse_schedule,
    write_heartbeat,
)
from .template import render_message, render_subject

log = logging.getLogger("mail_quota_report")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUN = 2

_SUMMARY_FMT = ("Lauf-Ende: ok={ok} mailboxen={n} warn={warn} kritisch={crit} "
                "ohne_ldap={no_ldap} ohne_quota={no_quota} gesendet={sent} "
                "dauer={dauer:.1f}s")


def setup_logging() -> None:
    import os

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "plain").lower()
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        class _JsonFormatter(logging.Formatter):
            def format(self, record):
                import json
                return json.dumps({
                    "ts": datetime.now().isoformat(),
                    "level": record.levelname,
                    "msg": record.getMessage(),
                })
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(level)
    log.addHandler(handler)


def os_environ_get(key: str, default: str) -> str:
    import os
    return os.environ.get(key, default)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-quota-report",
        description="Dovecot mailbox quota reporting by email (see README)",
    )
    parser.add_argument("--config", help="Pfad zur YAML-Config "
                        "(Default: ENV CONFIG_FILE/CONFIG_PATH, sonst "
                        "/etc/mail-quota-report/config.yaml)")
    parser.add_argument("--version", action="version",
                        version=f"mail-quota-report {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--selftest", action="store_true",
                      help="Config/Secrets/Verbindungsproben, Exit 0/1 — kein Versand")
    mode.add_argument("--dry-run", action="store_true",
                      help="Report nach stdout, KEIN Mailversand")
    mode.add_argument("--once", action="store_true",
                      help="ein Lauf mit Versand (außerhalb des Schedules)")
    return parser


# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------

def run_report(cfg: Config, send: bool = True, stdout: bool = False) -> int:
    """Ein kompletter Lauf: LDAP → doveadm → Filter → Report → Versand.

    Rückgabe: Exit-Code (0 ok, 2 Lauf-Fehler). send=False + stdout=True
    = --dry-run. Kein Datenbestand über den Lauf hinaus.
    """
    import time
    started = time.monotonic()
    source = LdapSource(cfg.ldap)
    conn = source._connect()
    try:
        entries = source.enumerate_entries(conn=conn)
        entries = source.filter_by_groups(entries, conn)
    finally:
        try:
            conn.unbind_s()
        except Exception:  # noqa: BLE001 — Unbind-Fehler ist unkritisch
            pass
    mailboxes = [e.mail_primary or e.username for e in entries]
    mailboxes = [m for m in mailboxes if m]

    collector = make_collector(cfg.collect)
    result = collector.collect(mailboxes)
    matched = source.match_mailboxes(entries, mailboxes)

    rows = build_rows(
        mailboxes, result.records, matched,
        warn_percent=cfg.report.warn_percent,
        critical_percent=cfg.report.critical_percent,
        group_by_tenant=cfg.report.group_by_tenant,
        hide_zero_limit=cfg.report.hide_zero_limit,
        # Mandant aus dem LDAP-Eintrag (tenant_from_dn wird beim Einlesen
        # je Eintrag gesetzt, ldap_source.py) — ohne tenant_of bliebe
        # row.tenant leer und die Gruppierung hätte keinen Effekt
        # (Feldbefund aus dem Pilot-Deployment).
        tenant_of=lambda e: getattr(e, "tenant", ""),
    )

    # Berichtsmodus: all oder below_threshold (Filter auf freien Speicher)
    if cfg.report.mode == "below_threshold" and cfg.report.threshold:
        th: Threshold = cfg.report.threshold
        rows = [r for r in rows if passes_threshold(r.used_kib, r.limit_kib, th)]
        threshold_desc = th.describe
    else:
        threshold_desc = ""

    rows = sort_rows(rows, cfg.report.sort)
    counts = summarize(rows)

    data = ReportData(
        deployment=cfg.deployment,
        date=datetime.now().strftime("%Y-%m-%d"),
        rows=rows,
        n_total=counts["n"],
        n_no_ldap=counts["no_ldap"],
        n_no_quota=counts["no_quota"],
        n_warn=counts["warn"],
        n_crit=counts["crit"],
        warn_percent=cfg.report.warn_percent,
        critical_percent=cfg.report.critical_percent,
        sort=cfg.report.sort,
        group_by_tenant=cfg.report.group_by_tenant,
        hide_zero_limit=cfg.report.hide_zero_limit,
        mode=cfg.report.mode,
        threshold_desc=threshold_desc,
        errors=[f"{mb}: {err}" for mb, err in sorted(result.errors.items())],
    )

    subject = render_subject(cfg.report.subject, data)
    rendered = render_message(data, cfg.message)

    sent = False
    if stdout:
        print(rendered.text_body or rendered.html_body or "")
    if send:
        try:
            msg = build_message(cfg.smtp.sender, list(cfg.report.recipients),
                                subject, rendered.text_body, rendered.html_body)
            send_message(cfg.smtp, msg)
            sent = True
        finally:
            # Heartbeat nach jedem echten Lauf-VERSUCH (Expirator-Muster):
            # schlägt der Lauf fehl (z. B. Relay down), lebt der Scheduler
            # trotzdem — der Healthcheck misst Scheduler-Lebendigkeit, nicht
            # Versand-Erfolg (Fehlversuche stehen im Log; ein Healthcheck-Kill
            # bei Relay-Ausfall wäre ein Restart-Loop). --dry-run und
            # --selftest schreiben bewusst NICHT: ein manueller Lauf darf
            # einen toten Scheduler nicht grün maskieren.
            write_heartbeat()
    duration = time.monotonic() - started
    log.info(_SUMMARY_FMT.format(
        ok=True, n=counts["n"], warn=counts["warn"], crit=counts["crit"],
        no_ldap=counts["no_ldap"], no_quota=counts["no_quota"],
        sent=sent, dauer=duration,
    ))
    return EXIT_OK


def run_selftest(cfg: Config) -> bool:
    """Selftest: Config + Secrets sind bereits geladen (fail-closed davor).

    Optionale Netz-Proben nur wenn selftest.network_probes: true —
    LDAP-Bind und SSH-Erreichbarkeit testen, KEIN doveadm, kein Versand.
    """
    ok = True
    print("selftest: Config geladen und validiert: OK")
    print(f"selftest: deployment={cfg.deployment} mode={cfg.report.mode} "
          f"format={cfg.message.format}")
    if cfg.report.mode == "below_threshold":
        print(f"selftest: threshold={cfg.report.threshold.describe}")
    if not cfg.selftest_network_probes:
        print("selftest: Netz-Proben deaktiviert (selftest.network_probes) — OK")
        return True

    source = LdapSource(cfg.ldap)
    try:
        conn = source._connect()
        conn.search_s(cfg.ldap.tenant_bases[0], 0, "(objectClass=*)", ["dn"])
        print(f"selftest: LDAP-Bind und Suche OK ({cfg.ldap.uri})")
    except Exception as e:  # noqa: BLE001 — Selftest sammelt, bricht nicht
        print(f"selftest: LDAP-Probe FEHLGESCHLAGEN: {e}")
        ok = False
    try:
        import socket
        host, port = cfg.smtp.host, cfg.smtp.port
        with socket.create_connection((host, port), timeout=10):
            print(f"selftest: Relay erreichbar: {host}:{port}")
    except OSError as e:
        print(f"selftest: Relay-Probe FEHLGESCHLAGEN: {host}:{port}: {e}")
        ok = False
    return ok


def _scheduler_loop(cfg: Config, schedule: Schedule, *,
                    sleep=None, now=None, run=None) -> None:
    """Scheduler-Hauptschleife: parken bis zur fälligen Zeit, dann EIN Lauf.

    Struktur: nächste fällige Zeit berechnen; solange sie in
    der Zukunft liegt, in Stundenschritten schlafen und NACH JEDEM Schlaf
    die Restzeit gegen das GLEICHE Ziel neu messen (Zeit kann springen —
    NTP, Suspend). Erst wenn die fällige Zeit erreicht/überschritten ist,
    läuft run_report() — genau einer, auch wenn ein Schlaf die Zeit
    übersprungen hat (nachgeholter Lauf). Kein Lauf, wenn nichts fällig
    ist. Nach dem Lauf wird die nächste fällige Zeit neu berechnet
    (kein Drift).

    sleep/now/run sind injizierbar (Tests mit Fake-Uhr); Produktions-
    Defaults: time.sleep, jetzt in der Schedule-Zone, run_report-Versand.

    Heartbeat beim Scheduler-START: genau das misst der Healthcheck —
    „Scheduler lebt“ (Docstring scripts/healthcheck.py). Ohne diesen
    Schreibvorgang hätte ein frischer Container mit SCHEDULE=0 7 * * *
    bis zum ersten Lauf (bis zu 24 h) KEIN Heartbeat → HEALTHCHECK
    (Interval 180 s, Retries 2) killt ihn nach ~35 min (Exit 137,
    Restart-Loop — Feldbefund aus dem Pilot-Deployment).
    --dry-run/--selftest schreiben weiterhin NICHT: ein manueller Lauf
    darf einen toten Scheduler nicht grün maskieren (unverändert).
    """
    import time as _time

    if sleep is None:
        sleep = _time.sleep
    if now is None:
        def now():  # noqa: F811 — lokale Default-Implementierung
            return datetime.now(tz=get_tz())
    if run is None:
        def run():  # noqa: F811 — lokale Default-Implementierung
            return run_report(cfg, send=True, stdout=False)

    log.info("Scheduler aktiv: %s", schedule.describe())
    write_heartbeat()
    while True:
        # Nächste fällige Zeit aus EINER Zeitquelle (now()) berechnen —
        # nicht intern aus der echten Wanduhr: der Testbetrieb (Fake-Uhr)
        # und Produktion müssen dasselbe Ziel messen.
        nxt = next_run(schedule, now=now())
        delay = (nxt - now()).total_seconds()
        log.info("Nächster Lauf: %s (in %.0f s)", nxt.isoformat(), max(delay, 0))
        # In Stundenschritten parken, NACH JEDEM Schlaf die Restzeit neu
        # gegen das GLEICHE Ziel messen. Der alte Code lief nach dem
        # geparkten Schlaf ohne erneute Fälligkeitsprüfung direkt in
        # run_report() — jeder Stundenschlaf war ein Report-Versand
        # (Spam-Lauf exakt 3600 s nach Start — Feldbefund aus dem
        # Pilot-Deployment).
        while delay > 0:
            sleep(min(delay, 3600))
            delay = (nxt - now()).total_seconds()
        try:
            rc = run()
            if rc != EXIT_OK:
                log.error("Lauf fehlgeschlagen (Exit %s) — Scheduler bleibt aktiv", rc)
        except Exception as e:  # noqa: BLE001 — Scheduler überlebt Lauf-Fehler
            log.error("Lauf-Fehler: %s: %s — Scheduler bleibt aktiv",
                      type(e).__name__, e)
        # kurze Pause, damit ein sofort erneut fälliger Lauf nicht engt
        sleep(60)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Ohne Config ist jeder Modus fail-closed: Exit 1 (Jenkins-smoke-Stage)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"FAIL-CLOSED: {e}", file=sys.stderr)
        return EXIT_CONFIG

    if args.selftest:
        ok = run_selftest(cfg)
        return EXIT_OK if ok else EXIT_CONFIG

    if args.dry_run:
        try:
            return run_report(cfg, send=False, stdout=True)
        except Exception as e:  # noqa: BLE001 — Lauf-Fehler → Exit 2 mit Diagnose
            print(f"Lauf-Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            return EXIT_RUN

    if args.once:
        try:
            return run_report(cfg, send=True, stdout=False)
        except Exception as e:  # noqa: BLE001 — Lauf-Fehler → Exit 2 mit Diagnose
            log.error("Lauf-Fehler: %s: %s", type(e).__name__, e)
            return EXIT_RUN

    # Scheduler-Loop: SCHEDULE-ENV fail-closed prüfen, dann bis zur nächsten
    # Laufzeit schlafen (Phase 2 verfeinert; Grundgerüst jetzt, damit der
    # Container ohne Modus-Argument sinnvoll läuft).
    try:
        schedule = parse_schedule(os_environ_get("SCHEDULE", ""))
    except ScheduleError as e:
        print(f"FAIL-CLOSED: {e}", file=sys.stderr)
        return EXIT_CONFIG

    _scheduler_loop(cfg, schedule)
    return EXIT_OK  # pragma: no cover — Schleife endet in main nie


if __name__ == "__main__":
    sys.exit(main())
