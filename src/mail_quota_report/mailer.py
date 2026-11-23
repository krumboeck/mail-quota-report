"""mailer — SMTP-Versand über das lokale Relay (Phase 1).

Aufgaben:
- Versand über das Relay des Standorts (kein Cloud-Versand): smtplib
  (stdlib), STARTTLS konfigurierbar (in internen Overlay-Netzen mit
  DNS-Alias oft off, weil das Relay-Cert den Alias nicht deckt; wo
  unterstützt, an)
- message.format: text | html | both → korrekte MIME-Struktur:
  * text: text/plain, UTF-8
  * html: text/html, UTF-8
  * both: multipart/alternative mit text-Teil VOR html-Teil (RFC 2046:
    letzte Alternative ist die beste — Clients zeigen bevorzugt HTML)
- Empfängerliste (ein oder mehrere), Absender konfigurierbar; optional
  AUTH mit Passwort aus Secret-Datei (leer = kein AUTH)
- Fehler beim Versand: MailError mit Klartext-Diagnose → Exit-Code != 0
  im CLI (Exit 2, Lauf-Fehler)
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate

log = logging.getLogger(__name__)


class MailError(Exception):
    """Versand fehlgeschlagen — Klartext-Diagnose fürs Log (Exit 2 im CLI)."""


def build_message(sender: str, recipients: list[str], subject: str,
                  text_body: str | None, html_body: str | None,
                  date: str | None = None) -> EmailMessage:
    """MIME-Nachricht bauen: text/html/both mit korrekter Struktur.

    - both → multipart/alternative, text-Teil zuerst, html-Teil zuletzt
    - UTF-8 überall; Date-Header gesetzt
    - Fehlende Bodies je Format sind ein Fehler (fail-closed im Aufbau):
      text ohne text_body, html ohne html_body, both ohne beide → ValueError
    """
    fmt = "both" if (text_body and html_body) else ("html" if html_body else "text")
    if text_body is None and html_body is None:
        raise ValueError("Nachricht ohne Inhalt: weder text- noch html_body")
    if fmt == "text" and html_body is not None:
        raise ValueError("Format text, aber html_body übergeben")
    if fmt == "html" and text_body is not None:
        raise ValueError("Format html, aber text_body übergeben")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if date:
        msg["X-Mail-Quota-Report-Run"] = date

    if fmt == "both":
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
    elif fmt == "html":
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(text_body)
    return msg


def send_message(smtp_cfg, msg: EmailMessage,
                  smtp_client_factory=None) -> list[str]:
    """Nachricht über das Relay senden; gibt die angenommene Empfängerliste zurück.

    smtp_client_factory ist injizierbar für Unit-Tests (Fake-SMTP mit
    canned Antworten — kein Netz in Tests, canned Daten statt Produktiv).
    Produktiv: smtplib.SMTP mit optional STARTTLS und AUTH.
    """
    if not smtp_cfg.sender:
        raise MailError("smtp.sender fehlt")
    if not msg["To"]:
        raise MailError("Keine Empfänger konfiguriert")

    factory = smtp_client_factory or _default_smtp_factory
    try:
        with factory(smtp_cfg) as smtp:
            if smtp_cfg.starttls:
                smtp.starttls()
            if smtp_cfg.password:
                smtp.login(smtp_cfg.sender, smtp_cfg.password)
            refused = smtp.send_message(msg)
    except smtplib.SMTPException as e:
        raise MailError(f"SMTP-Versand fehlgeschlagen: {e}") from e
    except OSError as e:
        raise MailError(f"Relay nicht erreichbar: {smtp_cfg.host}:{smtp_cfg.port}: "
                        f"{e}") from e
    if refused:
        # refused: dict recipient -> (code, msg) — Teilversand ist ein Fehler
        details = "; ".join(f"{rcpt}: {code} {text.decode() if isinstance(text, bytes) else text}"
                            for rcpt, (code, text) in refused.items())
        raise MailError(f"Relay hat Empfänger abgelehnt: {details}")
    return list(msg["To"].split(", "))


def _default_smtp_factory(smtp_cfg):
    client = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=30)
    client.ehlo()
    return client
