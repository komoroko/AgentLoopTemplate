"""Tests for common.py — the shared primitives every agentloop tool builds on."""

from __future__ import annotations

import sys
from pathlib import Path

import common
import pytest
import yaml

_STATE = """---
project: "demo"
gates:
  requirements: pending
  design: approved   # 2026-07-01 alice
---

# body

a stray --- in the body must not confuse the parser
"""


# --- run: subprocess with the rc-124 timeout convention -----------------------


def test_run_kills_hung_process_with_rc_124() -> None:
    rc, out = common.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=".", timeout=0.2)
    assert rc == 124  # the coreutils timeout convention; a hung process must not stall the loop
    assert "timed out after 0s (process killed)" in out


def test_run_no_timeout_by_default() -> None:
    rc, out = common.run([sys.executable, "-c", "print('ok')"])
    assert rc == 0
    assert "ok" in out


# --- parse_frontmatter: one parser, one split rule ---------------------------


def test_parse_frontmatter_returns_the_mapping() -> None:
    front = common.parse_frontmatter(_STATE)
    assert front is not None and front["project"] == "demo"


def test_parse_frontmatter_ignores_body_fences() -> None:
    """maxsplit=2: a `---` inside the body never changes what the front matter is."""
    front = common.parse_frontmatter(_STATE)
    assert front is not None and set(front) == {"project", "gates"}


@pytest.mark.parametrize("text", ["no front matter", "---\nunterminated: yes\n", "---\n- a list\n---\nbody"])
def test_parse_frontmatter_structural_absence_is_none(text: str) -> None:
    assert common.parse_frontmatter(text) is None


def test_parse_frontmatter_malformed_yaml_raises() -> None:
    """Malformed YAML is an error, not a state — the caller picks its posture (docstring)."""
    with pytest.raises(yaml.YAMLError):
        common.parse_frontmatter("---\n{invalid: [\n---\nbody")


# --- read_frontmatter: the fail-open file reader ------------------------------


def test_read_frontmatter_fails_open_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "state.md"
    p.write_text("no front matter", encoding="utf-8")
    assert common.read_frontmatter(str(p)) == {}


def test_read_frontmatter_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        common.read_frontmatter(str(tmp_path / "absent.md"))


# --- gates_of: coercion with the absent-vs-present split ----------------------


def test_gates_of_coerces_to_str() -> None:
    gates = common.gates_of({"gates": {"requirements": "pending", "design": True}})
    assert gates == {"requirements": "pending", "design": "True"}


@pytest.mark.parametrize("front", [None, {}, {"gates": "not a mapping"}, {"gates": ["a", "b"]}])
def test_gates_of_absent_is_none(front: dict[str, object] | None) -> None:
    assert common.gates_of(front) is None


# --- gate-chain invariant ------------------------------------------------------


def test_gate_chain_violations_reports_each_approved_over_pending() -> None:
    gates = {"requirements": "pending", "design": "approved", "tasks": "pending", "build": "approved"}
    assert common.gate_chain_violations(gates) == [("design", "requirements"), ("build", "requirements")]


def test_gate_chain_ok_for_a_proper_prefix() -> None:
    assert common.gate_chain_violations({"requirements": "approved", "design": "approved"}) == []
    assert common.gate_chain_violations({}) == []


def test_pending_upstream_names_the_first_blocker() -> None:
    assert common.pending_upstream({}, "tasks") == "requirements"
    assert common.pending_upstream({"requirements": "approved"}, "design") is None
    assert common.pending_upstream({"requirements": "approved"}, "tasks") == "design"


# --- rewrite_gate_line: surgical front-matter line rewrite ----------------------


def test_rewrite_gate_line_keep_trailer_preserves_the_comment() -> None:
    out, n = common.rewrite_gate_line(_STATE, "design", "approved", "pending", keep_trailer=True)
    assert n == 1
    assert "design: pending   # 2026-07-01 alice" in out
    assert out.replace("design: pending ", "design: approved ") == _STATE  # everything else intact


def test_rewrite_gate_line_replace_trailer_stamps_the_new_value() -> None:
    out, n = common.rewrite_gate_line(_STATE, "requirements", "pending", "approved   # 2026-07-12", keep_trailer=False)
    assert n == 1
    assert "requirements: approved   # 2026-07-12" in out


def test_rewrite_gate_line_only_touches_the_front_matter() -> None:
    text = "---\ngates:\n  design: approved\n---\nbody says\n  design: approved\n"
    out, n = common.rewrite_gate_line(text, "design", "approved", "pending", keep_trailer=True)
    assert n == 1
    assert out.endswith("body says\n  design: approved\n")  # the body line survived


def test_rewrite_gate_line_no_match_is_zero() -> None:
    assert common.rewrite_gate_line("no front matter", "design", "approved", "pending", keep_trailer=True) == (
        "no front matter",
        0,
    )


# --- read_yaml: tolerant mapping reads ----------------------------------------


def test_read_yaml_reads_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    assert common.read_yaml(str(p)) == {"a": 1}


@pytest.mark.parametrize("content", ["- just\n- a list\n", "{broken: [\n"])
def test_read_yaml_tolerates_bad_content(tmp_path: Path, content: str) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(content, encoding="utf-8")
    assert common.read_yaml(str(p)) is None


def test_read_yaml_tolerates_missing_file(tmp_path: Path) -> None:
    assert common.read_yaml(str(tmp_path / "absent.yaml")) is None
