"""The production gate checks the image itself before it starts (BUG-117).

The first 4.14.2 image put Debian's ``bwrap`` on the image's ``PATH``; codex
took it instead of its own and every codex call failed (INC-4). The gate only
saw it once the gateway served: its CLI check ran ``--version``, which a
wrong ``bwrap`` does not touch, and the corrected image then carried the same
tag as the broken one. ``docs/runbooks/prod-gate.md`` now, before the switch:

- writes down the ID of the image the build produced (``{{.Id}}``) and runs
  every check on that ID, not on a tag another build can move;
- checks that no ``bwrap`` is on the image's ``PATH`` and that the gateway's
  own (``agy_cage.GATEWAY_BWRAP``) starts;
- after the switch, checks that the container runs that very ID;
- says how the operator removes a broken image by its ID.

``deploy/ovh/verify.sh`` only calls the running gateway over HTTP (no Docker),
and ``verify-agy-sandbox.py`` runs inside the started container (it reports a
``bwrap`` on the ``PATH`` too, after the switch): neither runs before start,
so the gate's commands are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

from plugboard import agy_cage

ROOT = Path(__file__).resolve().parents[1]
GATE = (ROOT / "docs/runbooks/prod-gate.md").read_text(encoding="utf-8")


def _step(number: int) -> str:
    match = re.search(rf"(?ms)^### {number}\. .*?(?=^### |^## )", GATE)
    assert match, number
    return match.group(0)


def _commands(text: str) -> str:
    return "\n".join(re.findall(r"(?ms)^```bash\n(.*?)^```", text))


def test_the_build_step_writes_down_the_image_id():
    commands = _commands(_step(4))

    assert re.search(
        r"new_id=\"\$\(sudo docker image inspect --format '\{\{\.Id\}\}' "
        r"plugboard:private-full-v<new-version>\)\"",
        commands,
    ), commands
    assert "<new-image-id>" in _step(4)


def test_the_image_is_checked_by_its_id_before_the_switch():
    commands = _commands(_step(5))

    # Every check of step 5 runs on the ID, none on the tag.
    runs = re.findall(r"(?m)^sudo docker run .*$", commands)
    assert len(runs) >= 3, runs
    for run in runs:
        assert '"$new_id"' in run, run
        assert "--network none" in run, run
    assert "plugboard:private-full-v" not in commands


def test_no_bwrap_on_the_image_path_before_start():
    commands = _commands(_step(5))

    assert re.search(
        r"--entrypoint sh \"\$new_id\" -c 'command -v bwrap'; echo \"exit \$\?\"",
        commands,
    ), commands
    assert re.search(
        rf"--entrypoint {re.escape(agy_cage.GATEWAY_BWRAP)} \"\$new_id\" --version",
        commands,
    ), commands
    text = _step(5)
    assert "prints nothing, then `exit 1`" in text
    assert "INC-4" in text and "BUG-117" in text


def test_the_switch_runs_the_checked_image():
    step = _step(6)
    commands = _commands(step)
    up = commands.index("gw up -d --no-build gateway")

    # The tag still names the checked image right before the switch...
    before = commands[:up]
    assert re.search(
        r"\[ \"\$\(sudo docker image inspect --format '\{\{\.Id\}\}' "
        r"plugboard:private-full-v<new-version>\)\" = \"\$new_id\" \]",
        before,
    ), before
    # ...and the container runs it after.
    after = commands[up:]
    assert re.search(
        r"\[ \"\$\(sudo docker inspect --format '\{\{\.Image\}\}' "
        r"\"\$\(gw ps -q gateway\)\"\)\" = \"\$new_id\" \]",
        after,
    ), after


def test_the_operator_removes_a_broken_image_by_its_id():
    section = re.search(
        r"(?ms)^## Remove an image that failed the gate.*?(?=^## )", GATE
    )
    assert section, "section missing"
    text = section.group(0)
    assert "sudo docker image rm <broken-image-id>" in text
    assert "command -v bwrap" in text
    # Never the running image nor the kept rollback image.
    assert "plugboard:rollback-<running-version>" in text


def test_a_failed_image_check_stops_the_gate():
    table = GATE[GATE.index("## If a step fails") :]
    assert re.search(r"(?m)^\| 5 \| .*`bwrap`.*\|", table), table
    assert re.search(r"(?m)^\| 6 \| .*image ID.*\|", table), table
