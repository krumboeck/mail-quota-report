"""Trivialer grüner Test — hält das pytest-Build-Gate lauffähig, bis
Phase 1 echte Unit-Tests nachschiebt (Test-Strategie: pytest als Gate vor
jedem Commit/Build, siehe README „Build & Jenkins“)."""


def test_package_importable_with_version():
    import mail_quota_report

    assert mail_quota_report.__version__ == "0.2"
    assert mail_quota_report.__doc__


def test_stub_modules_importable_and_documented():
    import importlib

    for name in (
        "config",
        "doveadm",
        "ldap_source",
        "filter",
        "report",
        "template",
        "mailer",
        "cli",
        "scheduler",
    ):
        mod = importlib.import_module(f"mail_quota_report.{name}")
        assert mod.__doc__, (
            f"Modul mail_quota_report.{name} braucht einen Docstring "
            "(Phase-0-Konvention: Stubs nur mit Docstring + TODO-Verweisen)"
        )
