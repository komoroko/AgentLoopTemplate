"""Verify events.py — the structured escalation log (NDJSON truth + generated state.md view)."""

from __future__ import annotations

from pathlib import Path

import events
import pytest


def _log(tmp_path: Path) -> str:
    return str(tmp_path / "events.ndjson")


def test_append_assigns_monotonic_ids_and_roundtrips(tmp_path: Path) -> None:
    path = _log(tmp_path)
    first = events.append_event("blocked", task="T-003", step="test", detail="red", path=path)
    second = events.append_event("task_done", task="T-002", commit="abc123", path=path)
    assert (first.id, second.id) == (1, 2)
    loaded = events.load_events(path)
    assert [(e.id, e.event, e.task) for e in loaded] == [(1, "blocked", "T-003"), (2, "task_done", "T-002")]
    assert loaded[1].commit == "abc123"


def test_append_rejects_unknown_event_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown event"):
        events.append_event("typo_kind", path=_log(tmp_path))


def test_load_skips_corrupt_lines(tmp_path: Path) -> None:
    # A corrupt line (partial write, hand edit) must be skipped, never take the orchestrator down —
    # and the next append must still produce a valid, monotonically increasing id.
    path = _log(tmp_path)
    events.append_event("blocked", task="T-001", path=path)
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write('{"event": "blocked"}\n')  # missing id
    events.append_event("resolve", ref=1, path=path)
    loaded = events.load_events(path)
    assert [e.id for e in loaded] == [1, 2]


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert events.load_events(_log(tmp_path)) == []


def test_open_escalations_closed_by_resolve_ref(tmp_path: Path) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-003", path=path)  # id 1
    events.append_event("merge_conflict", task="T-004", path=path)  # id 2
    events.append_event("step_fail", task="T-003", step="test", path=path)  # not an escalation
    events.append_event("resolve", ref=1, detail="fixed", path=path)
    opened = events.open_escalations(events.load_events(path))
    assert [(e.id, e.event) for e in opened] == [(2, "merge_conflict")]


def test_render_view_lists_open_and_counts(tmp_path: Path) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-003", step="check", detail="line one\nline two", path=path)
    events.append_event("task_done", task="T-002", path=path)
    view = events.render_view(events.load_events(path))
    assert "blocked=1" in view and "task_done=1" in view
    assert "| 1 |" in view and "T-003" in view and "check" in view
    assert "line one" in view and "line two" not in view  # detail is first-line-only in the table


def test_render_view_escapes_pipes_and_truncates(tmp_path: Path) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-001", detail="a | b " + "x" * 200, path=path)
    view = events.render_view(events.load_events(path))
    assert "a \\| b" in view  # a raw pipe would break the markdown table
    assert "…" in view


def test_render_view_no_events(tmp_path: Path) -> None:
    view = events.render_view(events.load_events(_log(tmp_path)))
    assert "- (none)" in view


def test_render_summary_aggregates(tmp_path: Path) -> None:
    path = _log(tmp_path)
    for _ in range(2):
        events.append_event("blocked", task="T-003", path=path)
    events.append_event("blocked", task="T-005", path=path)
    events.append_event("step_fail", task="T-003", step="test", path=path)
    events.append_event("step_fail", task="T-003", step="test", path=path)
    events.append_event("step_fail", task="T-005", step="check", path=path)
    events.append_event("task_done", task="T-002", path=path)
    events.append_event("resolve", ref=1, path=path)
    summary = events.render_summary(events.load_events(path))
    assert "escalations: 3 total, 2 open" in summary
    assert "T-003×2, T-005×1" in summary
    assert "test×2, check×1" in summary
    assert "tasks done: 1" in summary


# --- state.md generated view -------------------------------------------------

_STATE = (
    "---\nproject: demo\n---\n# board\n\n## Escalation log\n\n"
    f"{events.VIEW_BEGIN}\n_(no events yet)_\n{events.VIEW_END}\n"
)


def test_refresh_state_view_replaces_block(tmp_path: Path) -> None:
    path = _log(tmp_path)
    state = tmp_path / "state.md"
    state.write_text(_STATE, encoding="utf-8")
    events.append_event("blocked", task="T-003", detail="red", path=path)
    assert events.refresh_state_view(path, str(state)) is True
    text = state.read_text(encoding="utf-8")
    assert "_(no events yet)_" not in text
    assert "T-003" in text
    assert events.VIEW_BEGIN in text and events.VIEW_END in text  # markers survive re-runs
    assert events.refresh_state_view(path, str(state)) is True  # idempotent re-render


def test_refresh_state_view_noop_without_markers(tmp_path: Path) -> None:
    state = tmp_path / "state.md"
    state.write_text("---\nproject: demo\n---\n# board\n", encoding="utf-8")
    before = state.read_text(encoding="utf-8")
    assert events.refresh_state_view(_log(tmp_path), str(state)) is False
    assert state.read_text(encoding="utf-8") == before


def test_refresh_state_view_missing_state_is_noop(tmp_path: Path) -> None:
    assert events.refresh_state_view(_log(tmp_path), str(tmp_path / "absent.md")) is False


# --- rotation (context hygiene; open escalations must survive) ---------------


def test_rotate_carries_open_escalations_and_preserves_ids(tmp_path: Path) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-003", detail="x" * 100, path=path)  # id 1, stays open
    events.append_event("blocked", task="T-004", path=path)  # id 2
    events.append_event("resolve", ref=2, path=path)
    for _ in range(20):
        events.append_event("task_done", task="T-001", path=path)
    assert events.rotate_if_large(path, max_bytes=512) is True
    assert Path(f"{path}.1").exists()  # the full history moved aside
    live = events.load_events(path)
    assert [(e.id, e.event) for e in live] == [(1, "blocked")]  # only the open escalation carried, id preserved
    nxt = events.append_event("task_done", task="T-005", path=path)
    assert nxt.id == 2  # ids continue from the carried max, still resolvable


def test_rotate_noop_when_small_or_absent(tmp_path: Path) -> None:
    path = _log(tmp_path)
    assert events.rotate_if_large(path, max_bytes=512) is False  # absent: nothing to do
    events.append_event("blocked", path=path)
    assert events.rotate_if_large(path, max_bytes=512) is False  # under budget: kept
    assert len(events.load_events(path)) == 1


def test_rotate_best_effort_on_replace_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _log(tmp_path)
    Path(path).write_text('{"id": 1, "ts": "", "event": "blocked"}\n' * 100, encoding="utf-8")

    def boom(self: Path, target: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", boom)
    assert events.rotate_if_large(path, max_bytes=512) is False  # swallowed, never aborts the run


# --- CLI ----------------------------------------------------------------------


def test_cli_add_and_resolve_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _log(tmp_path)
    state = tmp_path / "state.md"
    state.write_text(_STATE, encoding="utf-8")
    isolate = ["--path", path, "--state", str(state)]
    assert events.main(["--add", "blocked", "--task", "T-003", "--detail", "red", *isolate]) == 0
    assert "T-003" in state.read_text(encoding="utf-8")  # --add refreshed the view
    assert events.main(["--resolve", "1", "--note", "fixed by abc123", *isolate]) == 0
    out = capsys.readouterr().out
    assert "added #1 blocked (T-003)" in out
    assert "resolved #1 blocked (T-003)" in out
    loaded = events.load_events(path)
    assert loaded[1].event == "resolve" and loaded[1].ref == 1 and loaded[1].detail == "fixed by abc123"
    assert "resolve=1" in state.read_text(encoding="utf-8")  # --resolve refreshed it too


def test_cli_add_rejects_unknown_kind(tmp_path: Path) -> None:
    assert events.main(["--add", "typo", "--path", _log(tmp_path)]) == 2


def test_cli_resolve_rejects_unknown_or_non_escalation_id(tmp_path: Path) -> None:
    path = _log(tmp_path)
    events.append_event("task_done", task="T-001", path=path)  # id 1, not an escalation
    assert events.main(["--resolve", "1", "--path", path]) == 2
    assert events.main(["--resolve", "99", "--path", path]) == 2


def test_cli_resolve_rejects_already_resolved(tmp_path: Path) -> None:
    path = _log(tmp_path)
    isolate = ["--path", path, "--state", str(tmp_path / "state.md")]
    events.append_event("blocked", task="T-003", path=path)
    assert events.main(["--resolve", "1", *isolate]) == 0
    assert events.main(["--resolve", "1", *isolate]) == 2  # double-close is a usage error


def test_cli_render_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-003", path=path)
    assert events.main(["--path", path]) == 0
    assert "Open escalations" in capsys.readouterr().out


def test_cli_summary_appends_aggregates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _log(tmp_path)
    events.append_event("blocked", task="T-003", path=path)
    assert events.main(["--summary", "--path", path]) == 0
    out = capsys.readouterr().out
    assert "Open escalations" in out and "Aggregates" in out
