# Security-Policy — mail-quota-report

## Unterstützte Versionen

| Version | Supported |
|---|---|
| 0.x (MAJOR.MINOR, siehe README) | ja — Best-Effort |

Security-Fixes fließen über neue MAJOR.MINOR-Tags; Deployments pinnen
Tag@Digest und nehmen Rebuilds der Basis (Distro-Security-Updates) mit.

## Schwachstellen melden (Responsible Disclosure)

- **Nicht öffentlich melden.** Keine GitHub-/GitLab-Issues, keine
  Pull-Requests mit Exploit-Details, keine Posts in öffentlichen Kanälen,
  solange kein Fix koordiniert veröffentlicht ist.
- **Kontakt:** Der Security-Kontakt liegt bewusst NICHT als Klartext-
  Adresse in diesem Repo (Spam-/Harvesting-Schutz). Zuständige Kontakt-
  daten (Adresse, ggf. Public-Key für verschlüsselte Meldung) sind im
  Passbolt-Eintrag „mail-quota-report Security-Kontakt“ hinterlegt;
  Zugriff über den Betreiber (Krumböck Bernd) bzw. die zuständige
  Verwahrstelle.
- **Bitte angeben:** Beschreibung der Schwachstelle, betroffene Version
  bzw. Commit, Reproduktionsschritte, Impact-Einschätzung. Keine Tests
  gegen Produktivsysteme Dritter.

## Handling-Zusagen

- Eingangsbestätigung: angestrebt innerhalb von 5 Werktagen (Best-Effort).
- Keine Weitergabe der Meldung an Dritte ohne Zustimmung der meldenden
  Person.
- Koordinierte Veröffentlichung nach Fix; Credit nach Absprache.
- Keine rechtlichen Schritte gegen gutgläubige Meldungen, die dieser
  Policy folgen.

## Support-Realität (ehrlich)

Dies ist ein nicht-monetarisiertes Open-Source-Projekt, das im Rahmen
einer IT-Dienstleistung betrieben wird. Es gibt **keine SLA** und keinen
bezahlten Support. Analyse und Behebung erfolgen nach Möglichkeiten
(Best-Effort) durch den Betreiber.

## Sicherheits-Design (Kurzfassung)

- Fail-closed: Start ohne Config oder ohne lesbare Secret-Dateien endet
  mit Exit 1 — keine Default-Credentials.
- Secrets ausschließlich als Dateien (`*_FILE`-Muster, Docker Secrets /
  Datei-Mounts) — nie in ENV, nie im Image-Layer.
- Minimierte Angriffsfläche: kein EXPOSE, reiner Client (SSH/LDAP/SMTP),
  non-root uid 1000, `no-new-privileges` im Deployment.
- Dedizierter SSH-Key mit Forced-Command-Wrapper (nur der feste
  doveadm-Aufruf, kein Shell-Zugang); dedizierter Lese-Bind fürs
  Verzeichnis (nicht der Dovecot-Bind).
- SBOM (CycloneDX) je Build; Distro-Rebuild-Pfad für Security-Updates.

Details zur fail-closed-Konfiguration und zum Secrets-Handling
(`*_FILE`-Muster): Modul-Docstrings in `src/mail_quota_report/`
(config.py, doveadm.py) und README "Configuration".
