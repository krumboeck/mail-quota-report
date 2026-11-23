"""Tests Phase-3-Fix: client-seitige doveadm-Minimal-Config im Image.

Der Koordinator hat gegen das gebaute Image bewiesen: ohne
mail_plugins=quota antwortet doveadm "Unknown command 'quota'" (Exit 75)
— der erste Deploy-Lauf wäre gescheitert. Diese Tests sichern die
Fix-Bestandteile im Repo (Datei existiert, Pflichtzeilen enthalten,
Dockerfile-COPY vorhanden, Jenkinsfile prüft "Unknown command").
Kein Logik-Test nötig — der echte doveadm-Beweis läuft in der Jenkins-
smoke-Stage gegen das gebaute Image.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "conf" / "dovecot.conf"


def test_dovecot_conf_exists():
    assert CONF.is_file(), "conf/dovecot.conf fehlt im Repo"


def test_dovecot_conf_required_lines():
    """Exakt die drei vom Koordinator verifizierten Zeilen (2.4.0-Pflicht)."""
    text = CONF.read_text(encoding="utf-8")
    assert "dovecot_config_version = 2.4.0" in text
    assert "dovecot_storage_version = 2.4.0" in text, (
        "dovecot_storage_version MUSS gesetzt sein (doveconf-Fatal/Exit 89 sonst)")
    assert "mail_plugins = quota" in text, (
        "ohne mail_plugins=quota: doveadm 'Unknown command quota' (Exit 75)")


def test_dovecot_conf_is_not_server_config():
    """Klarstellung im Kommentar: Client-Minimal-Config, NICHT Server-Config."""
    text = CONF.read_text(encoding="utf-8")
    assert "NICHT die Server-Config" in text


def test_dockerfile_copies_dovecot_conf():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY conf/dovecot.conf /etc/dovecot/dovecot.conf" in dockerfile, (
        "Build-Zeit-COPY fehlt — ohne die Datei im Image crasht doveadm")


def test_jenkinsfile_checks_unknown_command():
    """smoke-Stage muss 'Unknown command' als FAIL-Bedingung prüfen."""
    jenkinsfile = (REPO / "Jenkinsfile").read_text(encoding="utf-8")
    assert "Unknown command" in jenkinsfile, (
        "doveadm-Verfügbarkeits-Beweis fehlt in der smoke-Stage")
    assert "127.0.0.1:1" in jenkinsfile, (
        "Beweis-Aufruf gegen garantiert toten Endpoint fehlt")


def test_consistency_check_expects_dovecot_conf():
    """Konsistenz-Check kennt die Datei als erwartete Image-Datei."""
    script = (REPO / "scripts" / "check_image_consistency.py").read_text(
        encoding="utf-8")
    assert "conf/dovecot.conf" in script
    assert "/etc/dovecot/dovecot.conf" in script
