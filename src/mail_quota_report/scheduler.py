"""scheduler — Laufzeitplan aus SCHEDULE-ENV, Heartbeat (Phase 1/2).

Aufgaben:
- SCHEDULE-ENV, drei Formen:
  * Aliase: `weekly` (Mo 07:00), `daily` (07:00), `monthly` (1. 07:00)
  * Cron-Syntax (5 Felder: Minute Stunde Tag Monat Wochentag; je Feld
    `*`, `*/n`, `n`, `n-m`, Komma-Listen; Tag+Wochentag gepinnt = ODER)
  * Intervall-Form (Phase 2): `12h`, `30m`, `2d`, kombiniert in beliebiger
    Reihenfolge (`45m12h`, `12h45m`) — jede Einheit höchstens einmal
- Intervalle: nächste Laufzeit = ANKER + k*Intervall (Anker lokal
  2000-01-01 00:00) — exakte Abstandsfolge, kein driftender Loop,
  restart-sicher; DST verschiebt die Wandzeit (absolute Arithmetik) —
  für feste Uhrzeiten Cron-Syntax verwenden
- Nächste Laufzeit JE Lauf neu aus dem Zeitplan berechnet (Expirator-
  Muster); lokale Zeit, TZ aus ENV (Default Europe/Vienna, im Image)
- Kein Host-Cron — läuft identisch in Swarm und compose
- Heartbeat-Datei beim Scheduler-START und nach jedem ECHTEN Lauf
  (HEARTBEAT_FILE, Default /tmp/last-run; --dry-run/--selftest schreiben
  bewusst NICHT — ein manueller Lauf darf einen toten Scheduler nicht
  grün maskieren); HEALTHCHECK_MAX_AGE_H skaliert mit dem Schedule
  (weekly → 170 h)
- Einmal-Läufe jederzeit per --once möglich
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

try:  # zoneinfo ist stdlib (3.9+); Sandbox und Image haben es
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — nur exotische Umgebungen
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TZ = "Europe/Vienna"
DEFAULT_HEARTBEAT_FILE = "/tmp/last-run"

#: Cron-Aliase (Expirator-Muster)
_ALIASES = {
    "weekly": "0 7 * * 1",     # Montag 07:00
    "daily": "0 7 * * *",      # täglich 07:00
    "monthly": "0 7 1 * *",    # 1. des Monats 07:00
}

#: Anker für Intervall-Schedules: lokale Zeit 2000-01-01 00:00.
#: Fest und restart-unabhängig — die Laufzeitfolge ist ANKER + k*Intervall.
_INTERVAL_ANCHOR = datetime(2000, 1, 1, 0, 0, 0)

#: Ein Cron-Feld: *, */n, n, n-m, oder Komma-Liste daraus
_FIELD_RE = re.compile(r"^(\*|\*/\d+|\d+(?:-\d+)?)(,(\*|\*/\d+|\d+(?:-\d+)?))*$")

#: Intervall-Form: Tokenfolge <n><m|h|d> in beliebiger Reihenfolge
_INTERVAL_RE = re.compile(r"^(?:\d+[mhd])+$")
_INTERVAL_TOKEN_RE = re.compile(r"(\d+)([mhd])")


class ScheduleError(Exception):
    """Ungültiger SCHEDULE-Wert (fail-closed → Exit 1 im CLI)."""


@dataclass(frozen=True)
class Schedule:
    """Geparster Zeitplan: Cron-5-Felder ODER Intervall (timedelta)."""

    raw: str
    cron: str | None = None
    interval: timedelta | None = None

    def describe(self) -> str:
        if self.cron:
            return f"cron {self.cron}"
        if self.interval is not None:
            secs = int(self.interval.total_seconds())
            if secs % 86400 == 0:
                return f"alle {secs // 86400} Tag(e)"
            if secs % 3600 == 0:
                return f"alle {secs // 3600} Stunde(n)"
            if secs >= 3600:
                return (f"alle {secs // 3600}h{secs % 3600 // 60}m")
            return f"alle {secs // 60} Minute(n)"
        return f"unbekannt ({self.raw})"


def _validate_cron(cron: str) -> str:
    """5 Felder prüfen (Syntax je Feld); liefert die normalisierte Form."""
    fields = cron.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"SCHEDULE={cron!r}: genau 5 Felder erwartet "
            "(Minute Stunde Tag Monat Wochentag)")
    for field in fields:
        if not _FIELD_RE.match(field):
            raise ScheduleError(
                f"SCHEDULE={cron!r}: ungültiges Feld {field!r} "
                "(erlaubt: *, */n, n, n-m, Komma-Listen)")
    return cron


def _parse_interval(value: str) -> timedelta:
    """Intervall-Form `12h`, `30m`, `45m12h` → timedelta; jede Einheit max. einmal.

    Raises ScheduleError bei unbekannten Einheiten, Doppel-Token, 0-Werten.
    """
    seen: dict[str, int] = {}
    total = 0
    pos = 0
    for m in _INTERVAL_TOKEN_RE.finditer(value):
        if m.start() != pos:
            raise ScheduleError(f"SCHEDULE={value!r}: ungültige Intervall-Syntax "
                                "(erwartet z. B. '12h', '30m', '45m12h')")
        n, unit = int(m.group(1)), m.group(2)
        if n == 0:
            raise ScheduleError(f"SCHEDULE={value!r}: 0 ist als Intervallwert "
                                "ungültig")
        if unit in seen:
            raise ScheduleError(f"SCHEDULE={value!r}: Einheit {unit!r} mehrfach")
        seen[unit] = n
        total += n * {"m": 60, "h": 3600, "d": 86400}[unit]
        pos = m.end()
    if pos != len(value) or total == 0:
        raise ScheduleError(f"SCHEDULE={value!r}: ungültige Intervall-Syntax")
    return timedelta(seconds=total)


def parse_schedule(raw: str | None) -> Schedule:
    """SCHEDULE-Wert parsen: Aliase, Cron-Syntax oder Intervall-Form.

    Raises ScheduleError mit Klartext bei ungültigen Werten (der CLI
    macht daraus Exit 1, fail-closed — keine stillen Defaults).
    """
    value = (raw or "").strip().lower()
    if not value:
        raise ScheduleError("SCHEDULE fehlt (ENV) — fail-closed, kein Default")
    if value in _ALIASES:
        return Schedule(raw=value, cron=_ALIASES[value])
    if _INTERVAL_RE.fullmatch(value):
        return Schedule(raw=value, interval=_parse_interval(value))
    return Schedule(raw=value, cron=_validate_cron(value))


def get_tz():
    """Zeitzone aus ENV TZ (Default Europe/Vienna; Fallback UTC bei Müll)."""
    name = os.environ.get("TZ") or DEFAULT_TZ
    if ZoneInfo is None:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — unbekannte TZ: UTC-Fallback
        return ZoneInfo("UTC")


def _expand(field: str, lo: int, hi: int, is_dow: bool = False) -> frozenset[int] | None:
    """Cron-Feld → Menge erlauber Werte; None = uneingeschränkt (*).

    Unterstützt *, */n, n, n-m, Komma-Listen. Wochentag: 0 → 7 (So),
    klassische cron-Kompatibilität.
    """
    if field == "*":
        return None
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            return None  # Liste mit * = uneingeschränkt
        if part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ScheduleError(f"Ungültiger Cron-Schritt: {part!r}")
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                raise ScheduleError(f"Ungültiger Bereich: {part!r}")
            values.update(range(start, end + 1))
        else:
            values.add(int(part))
    if is_dow and 0 in values:
        values.add(7)  # cron: 0 = Sonntag = ISO 7
    for v in values:
        if not lo <= v <= hi:
            raise ScheduleError(f"Wert {v} außerhalb {lo}..{hi} in Feld {field!r}")
    return frozenset(values)


def next_run(schedule: Schedule, now: datetime | None = None,
             _tz=None) -> datetime:
    """Nächste Laufzeit ab jetzt (je Lauf NEU berechnet — kein Drift).

    Berechnet minutengenau nach den Cron-Feldern; Tag- und Wochentag-
    Filter mit klassischer ODER-Semantik, wenn beide gepinnt sind.
    Sprungweite: Monat/Stunde/Tag werden übersprungen, nicht minütlich
    durchgeläuft.
    """
    tz = _tz or get_tz()
    if now is None:
        now = datetime.now(tz=tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    if schedule.interval is not None:
        return _next_run_interval(schedule, now)
    if schedule.cron is None:
        raise ScheduleError(f"SCHEDULE={schedule.raw!r}: weder Cron noch Intervall")

    minutes, hours, doms, months, dows = (
        _expand(f, lo, hi, is_dow=(i == 4))
        for i, (f, lo, hi) in enumerate([
            (schedule.cron.split()[0], 0, 59),
            (schedule.cron.split()[1], 0, 23),
            (schedule.cron.split()[2], 1, 31),
            (schedule.cron.split()[3], 1, 12),
            (schedule.cron.split()[4], 1, 7),
        ])
    )

    candidate = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = candidate + timedelta(days=366 * 2)
    while candidate < limit:
        if months is not None and candidate.month not in months:
            # zum 1. des nächsten Monats springen
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1,
                                             day=1, hour=0, minute=0)
            else:
                candidate = candidate.replace(month=candidate.month + 1,
                                             day=1, hour=0, minute=0)
            continue
        if doms is not None and dows is not None:
            day_ok = candidate.day in doms or candidate.isoweekday() in dows
        else:
            day_ok = ((doms is None or candidate.day in doms)
                      and (dows is None or candidate.isoweekday() in dows))
        if not day_ok:
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if hours is not None and candidate.hour not in hours:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        if minutes is None or candidate.minute in minutes:
            return candidate
        candidate += timedelta(minutes=1)
    raise ScheduleError("Keine nächste Laufzeit innerhalb von zwei Jahren gefunden")


def _next_run_interval(schedule: Schedule, now: datetime) -> datetime:
    """Nächste Intervall-Laufzeit: ANKER + k*Intervall > now (strikt).

    Der Anker (2000-01-01 00:00 lokale Zeit) ist fest: Container-Restarts
    verschieben die Folge NICHT — die nächste Laufzeit ist immer der
    nächste Rasterpunkt nach jetzt. Kein Drift, keine Anhäufung von
    versäumten Läufen.
    """
    interval = schedule.interval
    anchor = _INTERVAL_ANCHOR.replace(tzinfo=now.tzinfo)
    elapsed = (now - anchor).total_seconds()
    step = interval.total_seconds()
    k = int(elapsed // step) + 1
    return anchor + timedelta(seconds=k * step)


def heartbeat_path() -> str:
    """Heartbeat-Datei aus ENV (Default /tmp/last-run)."""
    return os.environ.get("HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE


def write_heartbeat(path: str | None = None, now: datetime | None = None) -> None:
    """Heartbeat-Datei schreiben (Altersprüfung macht der Healthcheck)."""
    p = path or heartbeat_path()
    ts = now or datetime.now(tz=get_tz())
    with open(p, "w", encoding="utf-8") as f:
        f.write(ts.isoformat())


def heartbeat_age_hours(path: str | None = None,
                        now: datetime | None = None) -> float:
    """Alter der Heartbeat-Datei in Stunden (für Tests/Healthcheck)."""
    p = path or heartbeat_path()
    with open(p, encoding="utf-8") as f:
        ts = datetime.fromisoformat(f.read().strip())
    ref = now or datetime.now(tz=ts.tzinfo)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=ts.tzinfo)
    return (ref - ts).total_seconds() / 3600.0
