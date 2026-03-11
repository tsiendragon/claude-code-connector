"""
tests/unit/test_fixtures.py — Data-driven parser tests using fixture folders.

Each fixture under tests/fixtures/<NNN>-<name>/ contains:
  frames/01.txt, 02.txt, ...   Terminal captures (ANSI-stripped, in poll order)
  expected.json                Expected output from parser functions

HOW TO ADD A NEW TEST CASE
───────────────────────────
1. Create: tests/fixtures/<NNN>-<description>/
2. Add frames:
     Single capture  → frames/01.txt
     Heartbeat test  → frames/01.txt, 02.txt, 03.txt, ...
                       (each .txt = one tmux pane capture, 300ms apart)
   Get content with: ccc tail <name> --lines 40
3. Add expected.json:
   {
     "description": "What this fixture is testing",
     "detectReady": { "isReady": true, "confidence": "prompt" },
     "detectReadySingleFrame": { ... },   // optional: shows single-frame is wrong
     "extractLastResponse": "the response text",
     "detectPermission": null
   }
4. Run: make test
   The new case is picked up automatically — no code changes needed.

HEARTBEAT NOTE
──────────────
detectReady is tested via simulateHeartbeat (all frames, 300ms apart).
If expected.json also has "detectReadySingleFrame", that field is compared
against a single-frame call with no prevLines — proving that a single snapshot
gives a different (incorrect) result vs. heartbeat polling.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "parse-fixture.mjs"
DIST_PARSER = Path(__file__).parent.parent.parent / "dist" / "parser.js"


def _fixture_cases():
    if not FIXTURES_DIR.exists():
        return []
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())


def _run_parser(frame_paths: list[Path]) -> dict:
    """Call parse-fixture.mjs and return parsed JSON output."""
    if not DIST_PARSER.exists():
        pytest.skip(
            "dist/parser.js not found — run `npm run build` first, then `make test`"
        )
    result = subprocess.run(
        ["node", str(SCRIPT)] + [str(p) for p in frame_paths],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"parse-fixture.mjs failed:\n{result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "case_dir",
    _fixture_cases(),
    ids=lambda p: p.name,
)
def test_fixture(case_dir: Path):
    frames_dir = case_dir / "frames"
    assert frames_dir.exists(), f"Missing frames/ directory in {case_dir}"

    frame_paths = sorted(frames_dir.glob("*.txt"))
    assert frame_paths, f"No .txt frame files in {frames_dir}"

    expected = json.loads((case_dir / "expected.json").read_text())
    actual = _run_parser(frame_paths)

    # ── detectReady (heartbeat over all frames) ──────────────────────────────
    exp_ready = expected["detectReady"]
    act_ready = actual["detectReady"]
    assert act_ready["isReady"] == exp_ready["isReady"], (
        f"detectReady.isReady mismatch\n"
        f"  frames: {[p.name for p in frame_paths]}\n"
        f"  expected: {exp_ready['isReady']}\n"
        f"  actual:   {act_ready['isReady']}"
    )
    assert act_ready["confidence"] == exp_ready["confidence"], (
        f"detectReady.confidence mismatch\n"
        f"  expected: {exp_ready['confidence']!r}\n"
        f"  actual:   {act_ready['confidence']!r}"
    )

    # ── detectReady (single frame, no heartbeat) — optional ──────────────────
    if "detectReadySingleFrame" in expected:
        exp_single = expected["detectReadySingleFrame"]
        act_single = actual["detectReadySingleFrame"]
        assert act_single["isReady"] == exp_single["isReady"], (
            f"detectReadySingleFrame.isReady mismatch\n"
            f"  expected: {exp_single['isReady']}\n"
            f"  actual:   {act_single['isReady']}\n"
            f"  (This proves single-frame status is unreliable — heartbeat required)"
        )
        assert act_single["confidence"] == exp_single["confidence"]

    # ── extractLastResponse ───────────────────────────────────────────────────
    exp_resp = expected["extractLastResponse"]
    act_resp = actual["extractLastResponse"]
    assert act_resp == exp_resp, (
        f"extractLastResponse mismatch\n"
        f"  expected: {exp_resp!r}\n"
        f"  actual:   {act_resp!r}"
    )

    # ── detectPermission ──────────────────────────────────────────────────────
    exp_perm = expected["detectPermission"]
    act_perm = actual["detectPermission"]

    if exp_perm is None:
        assert act_perm is None, (
            f"detectPermission: expected null, got {json.dumps(act_perm)}"
        )
    else:
        assert act_perm is not None, "detectPermission: expected non-null, got null"
        assert act_perm["type"] == exp_perm["type"], (
            f"detectPermission.type: expected {exp_perm['type']!r}, got {act_perm['type']!r}"
        )
        if "tool" in exp_perm:
            assert act_perm["tool"] == exp_perm["tool"], (
                f"detectPermission.tool: expected {exp_perm['tool']!r}, got {act_perm.get('tool')!r}"
            )
        for i, exp_opt in enumerate(exp_perm["options"]):
            act_opt = act_perm["options"][i]
            assert act_opt["key"] == exp_opt["key"], (
                f"option[{i}].key: expected {exp_opt['key']!r}, got {act_opt['key']!r}"
            )
            assert act_opt["label"] == exp_opt["label"], (
                f"option[{i}].label: expected {exp_opt['label']!r}, got {act_opt['label']!r}"
            )
            assert act_opt["selected"] == exp_opt["selected"], (
                f"option[{i}].selected: expected {exp_opt['selected']}, got {act_opt['selected']}"
            )
