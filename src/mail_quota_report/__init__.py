"""mail-quota-report — Dovecot mailbox quota reporting by email.

Docker container (Python 3, Debian distribution packages, no pip) that
collects mailbox quotas via doveadm (TCP collector against the doveadm
server, SSH + docker exec fallback), enriches them with directory data
(OpenLDAP / Active Directory) and emails the report through a local
relay.
"""

__version__ = "0.2"
