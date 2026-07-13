"""Tests for common.py — the shared primitives every agentloop tool builds on."""

from __future__ import annotations

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
