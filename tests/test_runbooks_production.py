"""The production gate and the rollback runbook describe the instance in service (PLU-66).

Up to 4.14.4 both runbooks gave the commands of the former private host
(``deploy/ovh/``): a checkout in ``/opt/plugboard`` driving
``deploy/ovh/compose.yml``, a port published on ``127.0.0.1:8765``, a network
with a fixed subnet. The instance in service runs otherwise
(``docs/runbooks/production-instance.md``): Compose project
``plugboard-private``, one Compose file per version under ``/etc/plugboard``,
no published port, an existing network. What only applies to the former host
now sits in a separate section, after the gate's own steps.

The checks of BUG-117 (no ``bwrap`` on the image's ``PATH``, every check and
the switch on the image ID) stay: ``tests/test_prod_gate_image.py`` pins them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOKS = ROOT / "docs" / "runbooks"
GATE = (RUNBOOKS / "prod-gate.md").read_text(encoding="utf-8")
ROLLBACK = (RUNBOOKS / "rollback-release.md").read_text(encoding="utf-8")

LEGACY = "## Former private host (deploy/ovh)"

# What only the former host had: never in the commands for the instance in
# service.
FORMER_HOST = (
    "127.0.0.1:8765",
    "cd /opt/plugboard",
    "-f deploy/ovh/compose.yml",
    "172.30.115.",
    "PLUGBOARD_IMAGE=",
)


def _before_legacy(text: str) -> str:
    assert LEGACY in text, f"missing section {LEGACY!r}"
    return text[: text.index(LEGACY)]


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(heading)}.*?(?=^#{{2,3}} )", text)
    assert match, heading
    return match.group(0)


def _commands(text: str) -> str:
    return "\n".join(re.findall(r"(?ms)^```bash\n(.*?)^```", text))


def test_both_runbooks_name_the_instance_in_service():
    for text in (GATE, ROLLBACK):
        assert "plugboard-private" in text
        assert "/etc/plugboard/compose.v<new-version>.json" in text
        assert "production-instance.md" in text
    # The gate leaves <running-version>; a rollback goes back to <previous-version>.
    assert "/etc/plugboard/compose.v<running-version>.json" in GATE
    assert "/etc/plugboard/compose.v<previous-version>.json" in ROLLBACK


def test_the_gate_no_longer_drives_the_former_host():
    gate = _before_legacy(GATE)
    for former in FORMER_HOST:
        assert former not in gate, former
    commands = _commands(gate)
    assert re.search(
        r"(?m)^gw\(\) \{ sudo docker compose -p plugboard-private "
        r"-f /etc/plugboard/compose\.v<new-version>\.json \"\$@\"; \}$",
        commands,
    ), commands
    assert re.search(
        r"(?m)^gw_prev\(\) \{ sudo docker compose -p plugboard-private "
        r"-f /etc/plugboard/compose\.v<running-version>\.json \"\$@\"; \}$",
        commands,
    ), commands


def test_the_rollback_of_the_instance_no_longer_drives_the_former_host():
    section = _section(ROLLBACK, "### 4b. Production instance (`plugboard-private`)")
    for former in FORMER_HOST:
        assert former not in section, former
    assert (
        "sudo docker compose -p plugboard-private "
        "-f /etc/plugboard/compose.v<previous-version>.json" in section
    )


def test_no_port_is_published_the_gateway_is_reached_from_inside():
    gate = _before_legacy(GATE)
    assert "No port is published" in gate
    commands = _commands(gate)
    # Health and smoke run inside the gateway container; the smoke key goes
    # through standard input, never through argv nor sudo's environment.
    assert "sudo docker exec" in commands
    assert "http://127.0.0.1:${PORT:-8765}" in commands
    assert "IFS= read -r PLUGBOARD_SMOKE_KEY" in commands
    assert "< ~/plugboard-tools/smoke_consumers.py" not in commands
    section = _section(ROLLBACK, "### 4b. Production instance (`plugboard-private`)")
    assert "No port is published" in section


def test_the_rollback_goes_back_to_the_previous_image_by_its_id():
    gate = _before_legacy(GATE)
    back = _commands(
        _section(gate, "### 8. Rehearse the rollback, then go forward again")
    )
    assert "gw_prev up -d --no-build gateway" in back
    assert '"$old_id"' in back
    assert re.search(
        r"\[ \"\$\(sudo docker inspect --format '\{\{\.Image\}\}' "
        r"\"\$\(gw_prev ps -q gateway\)\"\)\" = \"\$old_id\" \]",
        back,
    ), back

    section = _commands(
        _section(ROLLBACK, "### 4b. Production instance (`plugboard-private`)")
    )
    assert "<previous-image-id>" in section
    assert re.search(r"\{\{\.Image\}\}.*= \"\$old_id\"", section), section


def test_the_backup_is_described_as_it_is_done():
    gate = _before_legacy(GATE)
    step = _section(gate, "### 3. Check the data, then back it up")
    # Before a switch: the encrypted script run by hand, or the plain
    # archive of 4.14.0; neither restore has been rehearsed.
    assert "deploy/ovh/backup.sh" in step
    assert "/var/backups/plugboard" in step
    assert "not encrypted" in step
    assert "PLU-63" in step
    assert "plugboard-backup.timer" in gate

    section = _section(
        ROLLBACK, "### 5. If the old version cannot read the current data"
    )
    assert "PLU-63" in section


def test_what_the_repository_cannot_say_is_left_to_the_operator():
    for text in (_before_legacy(GATE), ROLLBACK):
        assert "to be confirmed by the operator" in text


def test_the_former_host_is_a_separate_section_at_the_end():
    for text in (GATE, ROLLBACK):
        legacy = text[text.index(LEGACY) :]
        assert "end of life" in legacy
        assert "deploy/ovh/README.md" in legacy


def test_bug_117_checks_stay_in_the_gate():
    gate = _before_legacy(GATE)
    assert "command -v bwrap" in gate
    assert "/usr/libexec/plugboard/bwrap" in gate
    assert "new_id=\"$(sudo docker image inspect --format '{{.Id}}'" in gate
    assert "## Remove an image that failed the gate" in gate
    assert "gw exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py" in gate
    assert "gw exec -T gateway python - < deploy/ovh/verify-codex-sandbox.py" in gate
