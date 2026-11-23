"""doveadm — Quota-Collectoren: doveadm-TCP (Default) und SSH+docker exec (Fallback).

Architektur-Entscheidung 2026-11-06 (README): Sammelweg host-agnostisch —
doveadm-TCP-Collector per Dovecots eigenem doveadm-Server; Verbindung über
Swarm/compose-DNS-Namen (z. B. mail-shared_imap:4242). SSH+docker exec
bleibt als konfigurierbarer Fallback (ssh_exec) für Umgebungen ohne
doveadm-Listener.

Aufgaben:
- Gemeinsames Collector-Interface: collect(mailboxes) -> CollectResult mit
  identischer QuotaRecord-Struktur für beide Wege.
- DoveadmTcpCollector: ruft das offizielle doveadm-Client-Binary je Mailbox
  auf. Verifizierte Syntax (doveadm-quota(1), Debian trixie dovecot-core
  2.4.1, manpages.debian.org/trixie/dovecot-core/doveadm-quota.1.en.html;
  identisch doc.dovecot.org/2.4.5/core/man/):
      doveadm [-o doveadm_ssl=<no|ssl|starttls>] quota get \
             -S <hostname>:<port> -u <mailbox>
  "-S socket_path: The option's argument is either an absolute path to a
  local UNIX domain socket, or a hostname and port (hostname:port), in
  order to connect a remote host via a TCP socket."
  - doveadm_password wird über eine temporäre Client-Config (-c, 0600,
    include der Image-Config) an den Client übergeben — NIE als argv
    (Prozessliste) und NIE als ENV DOVEADM_PASSWORD: doveadm 2.4 wertet
    das ENV nicht aus (Feldbefund aus dem Pilot-Deployment, live
    bewiesen); der dokumentierte Weg ist doveadm_password in der
    Client-Config.
- DoveadmSshCollector (= bisheriger Weg, Phase 1): EINE SSH-Session, Mailboxen
  als Stapel über stdin an den Forced-Command-Wrapper (tab-delimited-Protokoll);
  Fallback je Mailbox über ControlMaster. Unverändert, nur ans Interface
  angepasst.
- Parsing der doveadm-Ausgabe: table- UND flow-Formatter, Werte in kB
  (STORAGE-Zeile: benutzt/limit in kB; "quota get ... storage values are
  reported in kilobytes" — doveadm-quota(1)).
- Fehler je Mailbox ohne Laufabbruch: Einzel-Fehler landen in
  errors[mailbox] → Report-Status KEIN_QUOTA + Fehler-Sektion.
- Verbindungsfehler (Endpoint/SSH nicht erreichbar): klarer Fehler für
  ALLE Mailboxen (Exit 2 im CLI) — doveadm Exit 76 = "Protocol error
  during remote host connection" (doveadm(1), EXIT STATUS).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: doveadm quota get -u liefert (2.4, table-Formatter, doc.dovecot.org
#: doveadm-quota(1) Beispiel) u. a.:
#:   Quota name Type Value Limit %
#:   User quota STORAGE 90099 102400 87
#: Ohne Limit: User quota STORAGE 9820254 - 98
#: 2.3-Formate (doppeltes STORAGE mit (k)-Suffix je Spalte) bleiben
#: unterstützt (Quota-Name-Spalte fehlt dort, Type steht Leading).
#: Prefix = Quota-Name (kann Leerzeichen enthalten, z. B. "User quota").
_TABLE_RE = re.compile(
    r"^(?P<qname>\S+(?:\s\S+)*?\s+)?STORAGE(?:\s+STORAGE)?\s+"
    r"(?P<value>\d+)(?:\s*\(k\))?\s+"
    r"(?:(?P<limit>\d+|-)(?:\s*\(k\))?)?(?:\s+(?P<pct>\d+))?\s*$",
    re.IGNORECASE,
)

#: flow-Formatter (-f flow): STORAGE=9820254/10000000/98/% …
_FLOW_RE = re.compile(
    r"STORAGE=(?P<value>\d+)/(?P<limit>\d+|\?)",
    re.IGNORECASE,
)


@dataclass
class QuotaRecord:
    """Quota-Werte einer Mailbox (kB-Einheiten, wie dovecot sie zählt)."""

    mailbox: str
    used_kib: int
    limit_kib: int
    source_host: str = ""

    @property
    def valid(self) -> bool:
        return self.limit_kib >= 0 and self.used_kib >= 0


@dataclass
class CollectResult:
    """Ergebnis eines Collect-Laufs: Records + Fehler je Mailbox."""

    records: dict[str, QuotaRecord] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class CollectorError(Exception):
    """Verbindungsfehler des Collectors (Endpoint/SSH nicht erreichbar).

    Betrifft den GESAMTEN Lauf → Exit 2 im CLI (im Unterschied zu
    Per-Mailbox-Fehlern, die gesammelt werden und den Lauf nicht reißen).
    """


class DoveadmCollector:
    """Gemeinsames Interface: collect(mailboxes) -> CollectResult.

    Beide Implementierungen liefern identische QuotaRecord-Struktur;
    Per-Mailbox-Fehler ohne Abbruch; Verbindungsfehler als CollectorError.
    """

    def collect(self, mailboxes: list[str]) -> CollectResult:
        raise NotImplementedError


class DoveadmTcpCollector(DoveadmCollector):
    """doveadm-TCP-Collector (Default, host-agnostisch).

    Ruft das offizielle doveadm-Client-Binary aus dem Image (Debian-Paket
    dovecot-core) je Mailbox auf. Syntax verifiziert (s. Modul-Docstring):
        doveadm quota get -S <hostname>:<port> -u <mailbox>
    doveadm_password ÜBER EINE TEMPORÄRE CLIENT-CONFIG (-c), NIE als argv
    (Prozessliste) und NIE als ENV DOVEADM_PASSWORD — letzteres wird von
    doveadm 2.4 NICHT ausgewertet (Feldbefund aus dem Pilot-Deployment:
    "doveadm_password not set" trotz ENV; live am Image bewiesen).
    Dokumentierter Weg ist doveadm_password in der Client-Config
    (doc.dovecot.org doveadm.html,
    Replikations-Setup: "Both the client and the server also need to have
    a shared secret: doveadm_password = secret"). Die Temp-Config wird
    pro collect() mit 0600 erzeugt (Include der Image-Config, damit
    mail_plugins=quota registriert bleibt) und danach gelöscht.
    ssl-Modus per -o doveadm_ssl=<mode>.

    runner ist injizierbar für Unit-Tests (canned stdout/returncode).
    """

    #: doveadm(1) EXIT STATUS: 76 = Protocol error during remote host
    #: connection — Verbindung zum Endpoint endgültig fehlgeschlagen.
    EXIT_PROTOCOL_ERROR = 76

    def __init__(self, tcp_cfg, runner=None):
        self.cfg = tcp_cfg
        self.runner = runner or subprocess.run

    def _write_client_config(self) -> str | None:
        """Temp-Client-Config mit doveadm_password (0600), Pfad zurück.

        Include der Standard-Config (registriert quota-Subcommand); das
        Secret steht nur in der 0600-Datei, nie in argv/ENV/Log.
        """
        if not self.cfg.password:
            return None
        fd, path = tempfile.mkstemp(prefix="mqr-doveadm-", suffix=".conf")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("# mail-quota-report: temporäre doveadm-Client-Config\n")
                # 2.4 verlangt dovecot_config_version als ERSTE Setting —
                # Werte identisch zur Image-Config (conf/dovecot.conf).
                f.write("dovecot_config_version = 2.4.0\n")
                f.write("dovecot_storage_version = 2.4.0\n")
                f.write("!include_try /etc/dovecot/dovecot.conf\n")
                f.write(f"doveadm_password = {self.cfg.password}\n")
            os.chmod(path, 0o600)
            return path
        except OSError:
            os.unlink(path)
            raise

    def _build_cmd(self, mailbox: str, client_conf: str | None = None) -> list[str]:
        """Kommandozeile je Mailbox (verifizierte Syntax, s. Docstring)."""
        cmd = ["doveadm"]
        if client_conf:
            cmd += ["-c", client_conf]
        if self.cfg.ssl and self.cfg.ssl != "no":
            cmd += ["-o", f"doveadm_ssl={self.cfg.ssl}"]
        cmd += ["quota", "get", "-S", self.cfg.endpoint, "-u", mailbox]
        return cmd

    def collect(self, mailboxes: list[str]) -> CollectResult:
        result = CollectResult()
        if not mailboxes:
            return result
        env = {k: v for k, v in os.environ.items()
               # CONFIG_FILE/CONFIG_PATH sind die Report-YAML — doveadm
               # erbt sonst den ENV und parst die YAML als Dovecot-Config
               # ("Expecting '{'" — Feldbefund aus dem Pilot-Deployment).
               # doveadm soll die Image-Config /etc/dovecot/dovecot.conf
               # lesen.
               if k not in ("CONFIG_FILE", "CONFIG_PATH")}
        client_conf = self._write_client_config()
        connection_failed = False
        try:
            for mb in mailboxes:
                cmd = self._build_cmd(mb, client_conf)
                try:
                    proc = self.runner(
                        cmd, capture_output=True, text=True,
                        timeout=self.cfg.timeout, check=False, env=env,
                    )
                except subprocess.TimeoutExpired:
                    # Timeout je Mailbox: als Per-Mailbox-Fehler werten —
                    # bei wiederholtem Timeout als Verbindungsfehler.
                    result.errors[mb] = "doveadm-TCP: Timeout"
                    continue
                except OSError as e:
                    raise CollectorError(
                        f"doveadm-Binary nicht ausführbar: {e}") from e
                if proc.returncode == self.EXIT_PROTOCOL_ERROR:
                    connection_failed = True
                    result.errors[mb] = (
                        f"doveadm-TCP: Verbindung zu {self.cfg.endpoint} "
                        f"fehlgeschlagen (Exit 76, Protocol error — "
                        f"doveadm(1) EXIT STATUS)")
                    continue
                if proc.returncode != 0:
                    first_err = (proc.stderr or proc.stdout or "").strip().splitlines()
                    result.errors[mb] = (first_err[0][:200] if first_err
                                         else f"doveadm rc={proc.returncode}")
                    continue
                rec = parse_quota_output(proc.stdout, mb,
                                         source_host=self.cfg.endpoint)
                if rec is None:
                    result.errors[mb] = "keine STORAGE-Zeile in doveadm-Ausgabe"
                else:
                    result.records[mb] = rec
        finally:
            if client_conf:
                try:
                    os.unlink(client_conf)
                except OSError:
                    log.warning("Temp-Client-Config %s nicht löschbar",
                                client_conf)
        if connection_failed:
            raise CollectorError(
                f"doveadm-TCP: Endpoint {self.cfg.endpoint} nicht erreichbar "
                f"(Exit 76 bei mind. einer Mailbox) — Server-seitig prüfen: "
                f"service doveadm {{ inet_listener doveadm {{ port = ... }} }} "
                f"+ doveadm_password gesetzt?")
        return result


class DoveadmSshCollector(DoveadmCollector):
    """SSH+docker exec-Fallback (Phase-1-Weg, unverändert).

    Wrapper über EINE SSH-Session (tab-delimited-Batch), sonst ControlMaster-
    Fallback je Mailbox. Nur ans Interface angepasst.
    """

    def __init__(self, ssh_cfg, runner=None, host_selector=None):
        self.cfg = ssh_cfg
        self.runner = runner
        self.host_selector = host_selector

    def collect(self, mailboxes: list[str]) -> CollectResult:
        kwargs = {}
        if self.runner is not None:
            kwargs["runner"] = self.runner
        if self.host_selector is not None:
            kwargs["host_selector"] = self.host_selector
        return collect_ssh(mailboxes, self.cfg, **kwargs)


def make_collector(collect_cfg):
    """Collector aus der Config wählen (collect.type: doveadm_tcp|ssh_exec)."""
    if collect_cfg.type == "doveadm_tcp":
        if collect_cfg.tcp is None:
            raise CollectorError("collect.type=doveadm_tcp, aber keine "
                                 "tcp-Konfiguration geladen")
        return DoveadmTcpCollector(collect_cfg.tcp)
    if collect_cfg.type == "ssh_exec":
        if collect_cfg.ssh is None:
            raise CollectorError("collect.type=ssh_exec, aber keine "
                                 "ssh-Konfiguration geladen")
        return DoveadmSshCollector(collect_cfg.ssh)
    raise CollectorError(f"Unbekannter collect.type: {collect_cfg.type!r}")


def parse_quota_output(text: str, mailbox: str, source_host: str = "") -> QuotaRecord | None:
    """doveadm-Ausgabe (table- oder flow-Formatter) in ein QuotaRecord.

    Rückgabe None, wenn keine STORAGE-Zeile erkannt wird (Aufrufer macht
    daraus „kein Quota-Datensatz"). Unbekannte Zeilen werden ignoriert
    (doveadm kann zusätzliche Quota-Roots melden).
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Quota name"):
            continue
        m = _TABLE_RE.match(line)
        if m:
            limit_s = m.group("limit")
            limit = -1 if limit_s in (None, "-") else int(limit_s)
            return QuotaRecord(
                mailbox=mailbox,
                used_kib=int(m.group("value")),
                limit_kib=limit,
                source_host=source_host,
            )
        m = _FLOW_RE.search(line)
        if m:
            limit_s = m.group("limit")
            return QuotaRecord(
                mailbox=mailbox,
                used_kib=int(m.group("value")),
                limit_kib=int(limit_s) if limit_s != "?" else -1,
                source_host=source_host,
            )
    return None


def _ssh_base_cmd(ssh_cfg) -> list[str]:
    """SSH-Basisaufruf aus der Config (Key, Port, Timeouts, Härtung).

    StrictHostKeyChecking: accept-new — erster Connect akzeptiert den
    Host-Key (TOFU), danach ist ein geänderter Key ein harter Fehler
    (fail-closed gegen MITM). known_hosts_file optional konfigurierbar.
    """
    cmd = [
        "ssh",
        "-i", ssh_cfg.key_file,
        "-p", str(ssh_cfg.port),
        "-o", f"ConnectTimeout={ssh_cfg.connect_timeout}",
        "-o", f"BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "PasswordAuthentication=no",
        "-o", "PermitLocalCommand=no",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
    ]
    if ssh_cfg.known_hosts_file:
        cmd += ["-o", f"UserKnownHostsFile={ssh_cfg.known_hosts_file}"]
    return cmd


def collect_ssh(mailboxes: list[str], ssh_cfg,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 host_selector: Callable[[tuple[str, ...]], str] | None = None,
                 ) -> CollectResult:
    """Quoten aller Mailboxen über EINE SSH-Session erheben.

    Ablauf: Mailboxen über stdin an den Forced-Command-Wrapper;
    der Wrapper antwortet je Mailbox mit einem tab-delimited-Datensatz:
        <mailbox>\t<rc>\t<used>\t<limit>
    rc=0 mit Werten, rc!=0 → Fehlermeldung folgt nach dem Datensatz.
    Fällt der Wrapper weg (nicht installiert), greift der Fallback: je
    Mailbox ein doveadm-Aufruf über dieselbe Session (ControlMaster),
    Ausgabe geparst per parse_quota_output.

    runner ist injizierbar für Unit-Tests (canned stdout — nie
    Produktivdaten). host_selector wählt den Zielhost aus der Liste
    (Task-Float); Default: erster Host.
    """
    result = CollectResult()
    if not mailboxes:
        return result

    host = host_selector(ssh_cfg.hosts) if host_selector else ssh_cfg.hosts[0]
    target = f"{ssh_cfg.user}@{host}"
    wrapper_cmd = "mqr-doveadm-wrapper"
    stdin_data = "\n".join(mailboxes) + "\n"

    proc = runner(
        _ssh_base_cmd(ssh_cfg) + [target, wrapper_cmd],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=ssh_cfg.timeout,
        check=False,
    )

    if proc.returncode == 0 and "\t" in proc.stdout:
        # Wrapper-Weg: tab-delimited je Mailbox:
        #   <mailbox>\t<rc>\t<used>\t<limit>          (rc=0: Werte)
        #   <mailbox>\t<rc>\t<fehlermeldung>           (rc!=0: Fehler)
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            mb, rc = parts[0], parts[1]
            if mb not in mailboxes:
                continue
            if rc == "0" and len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
                result.records[mb] = QuotaRecord(
                    mailbox=mb, used_kib=int(parts[2]), limit_kib=int(parts[3]),
                    source_host=host,
                )
            else:
                result.errors[mb] = "\t".join(parts[2:]).strip() or f"doveadm rc={rc}"
        return result

    # Fallback: klassischer per-mailbox-Weg über dieselbe Session.
    log.warning(
        "mqr-doveadm-wrapper nicht verfügbar (rc=%s) — Fallback: doveadm je "
        "Mailbox über eine SSH-Session (ControlMaster)",
        proc.returncode,
    )
    cmd_prefix = _ssh_base_cmd(ssh_cfg)
    # ControlMaster: eine offene Session für alle Einzelaufrufe
    ctl = "/tmp/mqr-ssh-%d" % id(mailboxes)
    master_cmd = cmd_prefix + [
        "-o", "ControlMaster=yes",
        "-o", "ControlPath=" + ctl,
        "-o", "ControlPersist=yes",
        target,
    ]
    try:
        # Master öffnen (bleibt im Hintergrund; Check auf Erreichbarkeit)
        runner(master_cmd + ["true"],
               input="", capture_output=True, text=True,
               timeout=ssh_cfg.connect_timeout, check=False)
        for mb in mailboxes:
            remote = (f"docker exec {shlex.quote(ssh_cfg.container_filter)} "
                      f"doveadm quota get -u {shlex.quote(mb)}")
            try:
                p = runner(cmd_prefix + ["-o", "ControlPath=" + ctl, target, remote],
                           input="", capture_output=True, text=True,
                           timeout=ssh_cfg.timeout, check=False)
            except subprocess.TimeoutExpired:
                result.errors[mb] = "SSH-Timeout"
                continue
            if p.returncode != 0:
                first_err = (p.stderr or p.stdout or "unbekannter Fehler").strip().splitlines()
                result.errors[mb] = first_err[0][:200] if first_err else "SSH-Fehler"
                continue
            rec = parse_quota_output(p.stdout, mb, source_host=host)
            if rec is None:
                result.errors[mb] = "keine STORAGE-Zeile in doveadm-Ausgabe"
            else:
                result.records[mb] = rec
    finally:
        # Master schließen (sonst bleibt der SSH-Prozess hängen)
        try:
            runner(cmd_prefix + ["-o", "ControlPath=" + ctl, "-O", "exit", target],
                   input="", capture_output=True, text=True, timeout=5, check=False)
        except Exception:  # noqa: BLE001 — Cleanup darf nicht reißen
            log.debug("ControlMaster-Exit fehlgeschlagen (ignoriert)")
    return result
