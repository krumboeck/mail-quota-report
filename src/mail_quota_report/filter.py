"""filter — Berichtsmodi, Schwellwerte, Gruppen-Filter (Phase 1).

Aufgaben (README Features, Berichts-/Filter-Entscheidungen
06.11.2026):
- report.mode: all — alle Benutzer
- report.mode: below_threshold — nur Benutzer, deren FREIER Speicher
  (limit − benutzt) den Schwellwert unterschreitet:
  * type percent: frei < value % des Limits
  * type fixed:   frei < value (kB/MB/GB, case-insensitiv; kB-Einheiten,
    siehe config.KIB_PER_* — deckt sich mit der Report-Anzeige)
- Status-Klassifizierung für die Hervorhebung: ≥ warn_percent WARNUNG,
  ≥ critical_percent KRITISCH (Default 80/95, konfigurierbar)
- LDAP-Gruppen-Include/Exclude: include leer = alle; exclude gewinnt

Größenkonvention: dovecot STORAGE/mailQuota zählen in kB-Einheiten;
MB = 1.000 kB, GB = 1.000.000 kB (konsistent mit format_kib im report).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import KIB_PER_GB, KIB_PER_MB, ConfigError

#: Einheiten (case-insensitiv) → Faktor in kB-Einheiten.
_UNITS: dict[str, int] = {
    "": 1, "kb": 1, "k": 1, "kib": 1,
    "mb": KIB_PER_MB, "m": KIB_PER_MB, "mib": KIB_PER_MB,
    "gb": KIB_PER_GB, "g": KIB_PER_GB, "gib": KIB_PER_GB,
}

_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-z]*)\s*$", re.IGNORECASE)

#: Sonderstatus (Zusätzlich zu OK/WARNUNG/KRITISCH)
STATUS_OK = "OK"
STATUS_WARN = "WARNUNG"
STATUS_CRIT = "KRITISCH"
STATUS_NO_LDAP = "KEIN_LDAP"
STATUS_NO_QUOTA = "KEIN_QUOTA"
STATUS_LIMIT_0 = "LIMIT_0"


class FilterError(Exception):
    """Ungültiger Schwellwert/Größenangabe (Validierung fängt das vorher)."""


def parse_size(value: str | int | float) -> int:
    """Größe in kB-Einheiten parsen: "5GB", "512MB", "100 kB", 512000.

    Akzeptiert Zahlen mit Einheit (kB/KB/kb/KiB, MB/MiB/m, GB/GiB/g),
    case-insensitiv, optional mit Leerzeichen; reine Zahlen = kB-Einheiten.
    Raises ValueError bei negativen Werten, Müll-Einheiten, Leereingabe.
    """
    if isinstance(value, bool):  # bool ist int-Subklasse — nie gültig hier
        raise ValueError(f"Ungültige Größenangabe: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Negative Größe ungültig: {value!r}")
        if value != int(value):
            raise ValueError(f"Größe ohne Einheit muss ganzzahlig sein: {value!r}")
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"Ungültige Größenangabe: {value!r}")
    m = _SIZE_RE.match(value)
    if not m:
        raise ValueError(f"Ungültige Größenangabe: {value!r} "
                         "(erwartet z. B. '5GB', '512MB', '100kB')")
    number = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit not in _UNITS:
        raise ValueError(f"Unbekannte Einheit {m.group(2)!r} in {value!r} "
                         "(erlaubt: kB/KB, MB, GB)")
    if number < 0:
        raise ValueError(f"Negative Größe ungültig: {value!r}")
    result = number * _UNITS[unit]
    if result != int(result):
        raise ValueError(f"Größe ergibt keine ganze kB-Zahl: {value!r}")
    return int(result)


@dataclass(frozen=True)
class Threshold:
    """Validierter Schwellwert für below_threshold (Filter auf freien Speicher)."""

    type: str      # percent | fixed
    value: float   # percent: 0..100 | fixed: kB-Einheiten

    @property
    def describe(self) -> str:
        if self.type == "percent":
            return f"frei < {self.value:g} % des Limits"
        from .report import format_kib
        return f"frei < {format_kib(self.value)}"


def parse_threshold(data: dict) -> Threshold:
    """{type: percent, value: 80} oder {type: fixed, value: "5GB" | 512000}.

    Raises ValueError mit Klartext (config.py hängt das in die Fehlerliste).
    """
    if not isinstance(data, dict):
        raise ValueError("Schwellwert als Mapping {type, value} erwartet")
    ttype = data.get("type")
    value = data.get("value")
    if ttype == "percent":
        try:
            pct = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"percent: Zahl erwartet, got {value!r}") from None
        if not 0 <= pct <= 100:
            raise ValueError(f"percent: Wert in 0..100 erwartet, got {pct!r}")
        return Threshold(type="percent", value=pct)
    if ttype == "fixed":
        try:
            kib = parse_size(value)
        except ValueError as e:
            raise ValueError(f"fixed: {e}") from None
        return Threshold(type="fixed", value=float(kib))
    raise ValueError(f"type: 'percent' oder 'fixed' erwartet, got {ttype!r}")


def free_kib(used_kib: int, limit_kib: int) -> int:
    """Freier Speicher in kB-Einheiten (negativ bei Überschreitung)."""
    return limit_kib - used_kib


def free_percent(used_kib: int, limit_kib: int) -> float | None:
    """Freier Speicher in Prozent des Limits; None bei Limit 0."""
    if limit_kib <= 0:
        return None
    return (limit_kib - used_kib) * 100.0 / limit_kib


def used_percent(used_kib: int, limit_kib: int) -> float | None:
    """Auslastung in Prozent; None bei Limit 0 (kein Prozentwert)."""
    if limit_kib <= 0:
        return None
    return used_kib * 100.0 / limit_kib


def classify(percent: float | None, warn_percent: float, critical_percent: float) -> str:
    """Status: KRITISCH ab critical_percent, WARNUNG ab warn_percent (>=)."""
    if percent is None:
        return STATUS_LIMIT_0
    if percent >= critical_percent:
        return STATUS_CRIT
    if percent >= warn_percent:
        return STATUS_WARN
    return STATUS_OK


def passes_threshold(used_kib: int, limit_kib: int, threshold: Threshold) -> bool:
    """below_threshold-Entscheidung: freier Speicher < Schwellwert?

    Kein Quota-Datensatz oder Limit 0 → freier Speicher nicht berechenbar
    → False (kein Trivial-True; die Fälle laufen im Report über die
    Fehler-/Sonderfall-Sektionen, nicht über den Tabellenfilter).
    """
    if limit_kib <= 0:
        return False
    if threshold.type == "percent":
        fp = free_percent(used_kib, limit_kib)
        return fp is not None and fp < threshold.value
    return free_kib(used_kib, limit_kib) < threshold.value


def normalize_dn(dn: str) -> str:
    """DN normalisieren für Set-Vergleiche: lowercase, Leerzeichen raus."""
    return ",".join(part.strip().lower() for part in dn.split(","))


def apply_group_filter(entries: list, include_dns: set[str],
                       exclude_dns: set[str]) -> list:
    """LDAP-Gruppen-Filter auf Einträge (.dn-Attribut).

    - include leer = alle Einträge
    - exclude gewinnt über include
    - DNs case-insensitive/whitespace-normalisiert verglichen
    """
    include = {normalize_dn(d) for d in include_dns}
    exclude = {normalize_dn(d) for d in exclude_dns}
    result = []
    for entry in entries:
        dn = normalize_dn(getattr(entry, "dn", ""))
        if dn in exclude:
            continue
        if include and dn not in include:
            continue
        result.append(entry)
    return result
