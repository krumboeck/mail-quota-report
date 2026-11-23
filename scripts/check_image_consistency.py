"""Sandbox-QA: Image-Konsistenz-Check (Phase-2-Lehre vom Koordinator).

Der Phase-1-Defekt: python3-jinja2 fehlte im Dockerfile-apt-Set — das
Image wäre an ModuleNotFoundError gecrashed, obwohl die Sandbox-Tests
grün waren. Ursache: Imports und apt-Set wurden nie gegeneinander
gemessen. Dieser Check tut genau das (AST-basiert):

1. Alle Top-Level-Imports aus src/mail_quota_report/*.py und
   scripts/healthcheck.py extrahieren (inkl. lazy Imports).
2. Gegen das Dockerfile-apt-Set mappen (stdlib erkannt und frei).
3. Fehlende Pakete melden → Exit 1.

Kein Docker in der Sandbox nötig — das ist die statische Variante von
"gegen das Image messen". Der vollständige Image-Build/run läuft in der
Jenkins-Pipeline (Node mit Docker).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Dritt-Paket-Import → Debian-Distro-Paket (alles andere = stdlib)
IMPORT_TO_PACKAGE = {
    "yaml": "python3-yaml",
    "jinja2": "python3-jinja2",
    "ldap": "python3-ldap",
}

#: BINARY-Abhängigkeiten → Debian-Paket (Phase-3: doveadm-Client-Binary
#: für den TCP-Collector; dieselbe Fehlerklasse wie Python-Imports —
#: fehlt das Paket, crasht das Image/der Collector zur Laufzeit).
BINARY_TO_PACKAGE = {
    "doveadm": "dovecot-core",   # doveadm(1)-Client, Debian trixie 2.4.x
}

#: Wo Binaries im Code referenziert werden (Subprozess-Aufrufe):
BINARY_SOURCES = {
    "doveadm": ["src/mail_quota_report/doveadm.py"],
}

#: Erwartete Dateien im Repo, die per Dockerfile-COPY ins Image gehören
#: (Phase-3-Fix: conf/dovecot.conf — Client-Minimal-Config für die
#: quota-Subcommand-Registrierung; fehlt sie, crasht doveadm mit
#: "Unknown command 'quota'" — derselbe Fehlerzustand wie die
#: jinja2-Defekt-Klasse, deshalb im selben Check).
EXPECTED_IMAGE_FILES = {
    "conf/dovecot.conf": "/etc/dovecot/dovecot.conf",
}

#: stdlib-Module (Python 3.13) — bewusst gepflegt, kein sys.modules-Trick
STDLIB = {
    "os", "sys", "re", "time", "logging", "subprocess", "shlex",
    "argparse", "smtplib", "email", "dataclasses", "datetime", "pathlib",
    "typing", "zoneinfo", "json", "textwrap", "tempfile", "ast", "math",
    "collections", "functools", "itertools", "contextlib", "enum",
    "importlib", "unittest", "io", "base64", "socket",
}


def extract_imports(py_file: Path) -> set[str]:
    """Alle Top-Level-Module aus einer Datei (AST, inkl. Funktions-Imports)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


def dockerfile_apt_packages() -> set[str]:
    """Paketliste aus dem Dockerfile-apt-RUN-Block lesen."""
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"apt-get install -y --no-install-recommends (.*?)&&",
                  text, re.DOTALL)
    if not m:
        raise SystemExit("Dockerfile: apt-get-Install-Block nicht gefunden")
    return {p.strip().rstrip("\\") for p in m.group(1).split()
            if p.strip() and not p.strip().startswith("\\")}


def jenkinsfile_test_packages() -> set[str]:
    """Paketliste aus der Jenkinsfile-test-Stage lesen (Zeilen mit python3-*)."""
    text = (REPO / "Jenkinsfile").read_text(encoding="utf-8")
    return set(re.findall(r"(python3-[a-z0-9-]+)", text))


def main() -> int:
    py_files = list((REPO / "src" / "mail_quota_report").glob("*.py"))
    py_files += [REPO / "scripts" / "healthcheck.py"]

    third_party: dict[str, set[str]] = {}  # import → {dateien}
    internal = "mail_quota_report"
    for f in py_files:
        for mod in extract_imports(f):
            if mod == internal or mod.startswith("_"):
                continue  # paketintern / testfixtures
            if mod in STDLIB:
                continue
            third_party.setdefault(mod, set()).add(f.relative_to(REPO))

    apt = dockerfile_apt_packages()
    jenkins = jenkinsfile_test_packages()

    errors = []
    print("Gefundene Dritt-Imports:")
    for mod, files in sorted(third_party.items()):
        pkg = IMPORT_TO_PACKAGE.get(mod)
        status = []
        if pkg is None:
            errors.append(
                f"Import {mod!r} ({', '.join(sorted(str(f) for f in files))}): "
                "kein Debian-Paket-Mapping — IMPORT_TO_PACKAGE erweitern "
                "oder Import entfernen")
            status.append("UNBEKANNT")
        else:
            status.append(f"Dockerfile: {'OK' if pkg in apt else 'FEHLT'}")
            status.append(f"Jenkinsfile-test: {'OK' if pkg in jenkins else 'FEHLT'}")
            if pkg not in apt:
                errors.append(f"Dockerfile: {pkg} fehlt (Import {mod})")
            if pkg not in jenkins:
                errors.append(f"Jenkinsfile-test-Stage: {pkg} fehlt (Import {mod})")
        print(f"  {mod:12s} -> {pkg or '???':16s} [{' | '.join(status)}]")

    # --- Binary-Abhängigkeiten (Phase 3): Subprozess-Binaries im Code
    # referenziert → Paket muss im Dockerfile-apt-Set liegen.
    print("Binary-Abhängigkeiten (Subprozess-Aufrufe):")
    for binary, pkg in sorted(BINARY_TO_PACKAGE.items()):
        sources = BINARY_SOURCES.get(binary, [])
        referenced = any(
            _file_references_binary(REPO / s, binary) for s in sources)
        if not referenced:
            continue  # Binary nicht (mehr) im Code verwendet
        ok = pkg in apt
        print(f"  {binary:12s} -> {pkg:16s} "
              f"[Dockerfile: {'OK' if ok else 'FEHLT'}] "
              f"(referenziert in {', '.join(sources)})")
        if not ok:
            errors.append(f"Dockerfile: {pkg} fehlt (Binary {binary} wird "
                          f"per subprocess aufgerufen: {', '.join(sources)})")

    # --- Erwartete Image-Dateien (Phase-3-Fix): conf/dovecot.conf muss im
    # Repo existieren UND per Dockerfile-COPY ins Image gelangen.
    print("Erwartete Image-Dateien (Dockerfile-COPY):")
    dockerfile_text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    for rel_path, image_path in sorted(EXPECTED_IMAGE_FILES.items()):
        repo_ok = (REPO / rel_path).is_file()
        copy_ok = f"COPY {rel_path} {image_path}" in dockerfile_text
        print(f"  {rel_path:22s} -> {image_path:26s} "
              f"[Repo: {'OK' if repo_ok else 'FEHLT'} | "
              f"COPY: {'OK' if copy_ok else 'FEHLT'}]")
        if not repo_ok:
            errors.append(f"Datei fehlt im Repo: {rel_path}")
        if not copy_ok:
            errors.append(f"Dockerfile: COPY {rel_path} {image_path} fehlt")

    print(f"\nDockerfile-apt-Set: {sorted(apt)}")
    print(f"Jenkinsfile-test:   {sorted(jenkins)}")
    print()
    if errors:
        print("FEHLER (Image-Konsistenz):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("Image-Konsistenz OK: alle Dritt-Imports im Dockerfile-apt-Set "
          "und in der Jenkinsfile-test-Stage abgedeckt; alle Binary-"
          "Abhängigkeiten im Dockerfile-apt-Set.")
    return 0


def _file_references_binary(path: Path, binary: str) -> bool:
    """Prüfen, ob eine Datei das Binary als subprocess-Aufruf referenziert."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Aufrufmuster: "doveadm" als eigenständiges Token in cmd-Listen
    import re as _re
    return bool(_re.search(rf'["\']{binary}["\']|^\s*{binary}\b', text, _re.M))


if __name__ == "__main__":
    sys.exit(main())
