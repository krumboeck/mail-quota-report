# mail-quota-report — Docker-Image
# Basis debian:trixie-slim, Distro-Pakete, kein pip.
# Deployment-Details (Digest-Pinning, Ressourcen, no-new-privileges):
# README "Deployment notes" (Hinweise zu Ressourcen/Härtung).

FROM debian:trixie-slim

# Distro-Pakete: Security-Updates über Distro-Rebuilds (postfix-Konvention).
# python3-yaml/python3-ldap direkt aus apt; openssh-client für den
# doveadm-Collector (SSH + docker exec); ca-certificates für TLS
# (StartTLS/LDAPS). KEIN pip, keine pip-Abhängigkeitsfläche.
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=0.2
# OCI-Labels (MAJOR.MINOR per Build-ARG, Release-Tags vX.Y.Z — siehe CHANGELOG)
LABEL org.opencontainers.image.title="mail-quota-report" \
      org.opencontainers.image.description="Dovecot mailbox quota reporting by email (doveadm TCP/SSH collector, LDAP/AD enrichment, report via local relay)" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/krumboeck/mail-quota-report"

# non-root: fester Systembenutzer uid 1000, keine Shell (Design-Abschnitt 5.1/8.2)
RUN useradd --uid 1000 --system --no-create-home --shell /usr/sbin/nologin mqr

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-yaml \
        python3-jinja2 \
        python3-ldap \
        openssh-client \
        ca-certificates \
        dovecot-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
# dovecot-core: liefert das doveadm-Client-Binary (Debian-Paket dovecot-core,
# trixie: Dovecot 2.4.x) für den doveadm-TCP-Collector — siehe README
# „Dovecot server setup". openssh-client bleibt für den
# ssh_exec-Fallback.

WORKDIR /app
# Client-seitige Minimal-Config: registriert das quota-Subcommand im
# doveadm-Client (ohne mail_plugins=quota: "Unknown command 'quota'",
# Exit 75 — Koordinator-Befund 2026-11-06, gegen das gebaute Image
# bewiesen). Build-Zeit-COPY nach /etc/dovecot/dovecot.conf; Laufzeit
# read-only (doveadm liest nur, non-root braucht keinen Schreibzugriff).
# NICHT die Server-Config — Zweck und Quelle: conf/dovecot.conf im Repo.
COPY conf/dovecot.conf /etc/dovecot/dovecot.conf
COPY src/ /app/src/
COPY templates/ /app/templates/
COPY scripts/healthcheck.py /app/scripts/healthcheck.py
COPY config-example.yml /app/config-example.yml
ENV PYTHONPATH=/app/src \
    TZ=Europe/Vienna

# Healthcheck: Heartbeat-Alter gegen HEALTHCHECK_MAX_AGE_H (Expirator-
# Muster; kein curl/wget im Image — python3-stdlib genügt).
# Default 26 h (daily + Puffer); je Schedule skalieren: weekly → 170,
# monthly → 840 (docker service update --env-add oder Stack-File).
HEALTHCHECK --interval=30m --timeout=10s --start-period=5m --retries=2 \
    CMD ["python3", "/app/scripts/healthcheck.py"]

# Start per CMD (kein ENTRYPOINT mit Logik — Hauskonvention: exec-ENTRYPOINT
# ist mit app.inside()/Smoke-Tests unvereinbar, siehe Jenkinsfile).
# Fail-closed (Exit 1 ohne Config/Secrets) ist in cli.py implementiert
# (Phase 1); Betriebsmodi: --selftest / --dry-run / --once / Scheduler.
USER mqr
CMD ["python3", "-m", "mail_quota_report.cli"]
