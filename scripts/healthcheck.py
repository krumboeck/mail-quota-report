#!/usr/bin/env python3
"""Healthcheck für mail-quota-report (Expirator-Muster).

Prüft das Alter der Heartbeat-Datei, die der Scheduler beim START und
nach jedem ECHTEN Lauf schreibt (ohne den Start-Heartbeat killte der
Healthcheck frische Container, bevor der erste Lauf fällig war;
--dry-run/--selftest schreiben bewusst nicht — ein manueller Lauf
darf einen toten Scheduler nicht grün maskieren). Schlagen Läufe fehl
(LDAP/SSH/SMTP unerreichbar), wird die Datei dennoch geschrieben — der
Healthcheck meldet erst, wenn seit dem letzten Lauf mehr als
HEALTHCHECK_MAX_AGE_H vergangen ist.

Exit 0 = gesund, Exit 1 = Heartbeat zu alt oder fehlend.
Default-Grenze 26 h: deckt daily (24 h + Puffer); weekly-Betrieb
setzt HEALTHCHECK_MAX_AGE_H=170, monthly=840.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/last-run")
# 26 h: daily-Lauf + Puffer; je Schedule skalieren (weekly → 170 h)
MAX_AGE_H = int(os.environ.get("HEALTHCHECK_MAX_AGE_H", "26"))


def main() -> int:
    try:
        stat = os.stat(HEARTBEAT_FILE)
        age_h = (time.time() - stat.st_mtime) / 3600.0
    except OSError:
        print(f"UNHEALTHY: Heartbeat {HEARTBEAT_FILE} fehlt")
        return 1
    if age_h > MAX_AGE_H:
        age = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        print(f"UNHEALTHY: letzter Lauf {age} "
              f"({age_h:.0f}h her, Grenze {MAX_AGE_H}h)")
        return 1
    print(f"HEALTHY: letzter Lauf vor {age_h:.0f}h (Grenze {MAX_AGE_H}h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
