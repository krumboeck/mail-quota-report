// Jenkinsfile für mail-quota-report (jenkins.universalnet.at)
// Muster: pim/postfix + netbox-license-expirator (Node 'docker', Parameter
// IMAGE_TAG, Push nur des einen Tags, latest nie implizit).
// Stage-Ordnung: test (pytest-Build-Gate) -> build -> smoke (fail-closed)
// -> sbom (CycloneDX via syft gegen das gebaute Image) -> push.

node ('docker') {
    def app

    stage('Clone repository') {
        checkout scm
    }

    stage('test') {
        /* pytest-Build-Gate (Test-Strategie: Unit-Tests vor jedem Build).
         * Läuft in der Zielumgebung des Images — debian:trixie-slim mit
         * Distro-pytest (python3-pytest), KEIN pip. src/-Layout wird über
         * tests/conftest.py in sys.path gehängt; :ro-Mount, daher Cache-
         * Plugin aus (-p no:cacheprovider).
         * Danach: Image-Konsistenz-Check (Phase-2-Lehre): alle Dritt-
         * Imports gegen Dockerfile-apt-Set UND diese Stage messen —
         * verhindert die jinja2-Defekt-Klasse (ModuleNotFoundError im
         * gebauten Image trotz grüner Sandbox-Tests).
         */
        sh '''
            docker run --rm -v "$PWD":/workspace:ro -w /workspace \\
                debian:trixie-slim bash -c '
                    apt-get update -qq >/dev/null &&
                    apt-get install -y -qq --no-install-recommends python3-pytest python3-yaml python3-jinja2 python3-ldap >/dev/null &&
                    python3 -m pytest -p no:cacheprovider tests/ -v &&
                    python3 scripts/check_image_consistency.py
                '
        '''
    }

    stage('build') {
        app = docker.build("systemtechnik/docker/pim/mail-quota-report:snapshot")
    }

    stage('smoke') {
        /* Smoke-Tests fail-closed (netbox-license-expirator-Muster):
         * 1) --selftest MIT ci/smoke/config.yaml muss grün sein —
         *    Fake-Config, Netz-Proben aus (selftest.network_probes off),
         *    kein Versand, kein LDAP/SSH-Kontakt.
         * 2) negativer Test: Container-Start OHNE Config muss verweigern
         *    (Exit 1, fail-closed) — Security-Eigenschaft, bewiesen.
         * app.inside() ist mit einem Scheduler-ENTRYPOINT unvereinbar
         * (Expirator-Lesson) — deshalb docker run mit --entrypoint.
         */
        sh '''
            set +e
            docker run --rm --entrypoint python3 \\
                -v "$PWD/ci/smoke":/ci/smoke:ro \\
                -e CONFIG_FILE=/ci/smoke/config.yaml \\
                systemtechnik/docker/pim/mail-quota-report:snapshot \\
                -m mail_quota_report.cli --selftest
            RC=$?
            set -e
            if [ "$RC" -ne 0 ]; then
                echo "SELFTEST FEHLGESCHLAGEN (Exit $RC)"
                exit 1
            fi
            echo "Selftest OK"

            set +e
            docker run --rm systemtechnik/docker/pim/mail-quota-report:snapshot
            RC=$?
            set -e
            if [ "$RC" -eq 0 ]; then
                echo "NEGATIVER TEST FEHLGESCHLAGEN: Start ohne Config lieferte Exit 0, erwartet != 0 (fail-closed)"
                exit 1
            fi
            echo "Negativer Test OK: Start ohne Config -> Exit $RC (fail-closed)"

            # doveadm-Verfügbarkeits-Beweis (Phase-3-Fix): das quota-
            # Subcommand muss im Client registriert sein. Ohne die
            # Minimal-Config im Image antwortet doveadm "Unknown command
            # 'quota'" (Exit 75) — der erste echte Deploy-Lauf wäre
            # daran gescheitert (Koordinator-Befund 2026-11-06).
            # Beweis: Aufruf gegen einen garantiert toten Endpoint
            # (127.0.0.1:1) muss mit CONNECTION-Fehler antworten
            # (Connection refused), NICHT mit "Unknown command" —
            # dann ist das Subcommand registriert und scheitert nur
            # noch an der Verbindung (korrektes Verhalten).
            set +e
            DOVEADM_OUT=$(docker run --rm \
                systemtechnik/docker/pim/mail-quota-report:snapshot \
                doveadm quota get -S 127.0.0.1:1 -u ci@example.com 2>&1)
            DOVEADM_RC=$?
            set -e
            echo "doveadm-Check: Exit $DOVEADM_RC, Ausgabe: $DOVEADM_OUT"
            if echo "$DOVEADM_OUT" | grep -q "Unknown command"; then
                echo "DOVEADM-CHECK FEHLGESCHLAGEN: quota-Subcommand nicht registriert (conf/dovecot.conf fehlt im Image oder mail_plugins=quota fehlt)"
                exit 1
            fi
            if [ "$DOVEADM_RC" -eq 0 ]; then
                echo "DOVEADM-CHECK FEHLGESCHLAGEN: Aufruf gegen 127.0.0.1:1 lieferte Exit 0 — unerwartet (Endpoint muss connection-refused liefern)"
                exit 1
            fi
            echo "doveadm-Check OK: quota-Subcommand registriert, scheitert erwartungsgemäß an der Verbindung"
        '''
    }

    stage('sbom') {
        /* SBOM (CycloneDX via syft) je Build — CRA-Vorbereitung (F1).
         * Scan gegen das GEBAUTE Image: der Dpkg-Scan deckt die Distro-
         * Pakete vollständig ab (kein pip im Image). Fehlt syft auf dem
         * Node UND im Docker-Fallback, schlägt die Stage bewusst an
         * (SBOM ist freigegebenes Feature, kein Best-Effort-Anhang).
         */
        sh '''
            mkdir -p sbom
            if command -v syft >/dev/null 2>&1; then
                syft scan systemtechnik/docker/pim/mail-quota-report:snapshot \\
                    -o cyclonedx-json > sbom/mail-quota-report-snapshot.cdx.json
            else
                docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \\
                    anchore/syft:latest \\
                    systemtechnik/docker/pim/mail-quota-report:snapshot \\
                    -o cyclonedx-json > sbom/mail-quota-report-snapshot.cdx.json
            fi
            echo "SBOM erzeugt: sbom/mail-quota-report-snapshot.cdx.json"
        '''
        archiveArtifacts artifacts: 'sbom/*.cdx.json', fingerprint: true
    }

    stage('push') {
        /* Push nur des einen IMAGE_TAG-Parameters (postfix-Muster).
         * latest nie implizit — bewusstes Verschieben siehe README Tagging.
         * BUILDX_NO_DEFAULT_ATTESTATIONS=1: kein Buildx-Attestations-Fuss
         * (Expirator-Konvention).
         */
        withEnv(['BUILDX_NO_DEFAULT_ATTESTATIONS=1']) {
            docker.withRegistry('https://container.universalnet.at', 'jenkins.git.universalnet.at') {
                app.push(env.IMAGE_TAG ?: 'snapshot')
            }
        }
    }
}
