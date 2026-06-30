"""dag.py の決定的導出を検証する。"""

from __future__ import annotations

from pathlib import Path

import dag
import pytest


def _graph() -> dag.Graph:
    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="基盤", kind="foundation", blocked_by=()),
            dag.Task(id="T-002", title="葉A", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-003", title="葉B", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-004", title="統合", kind="integration", blocked_by=("T-002", "T-003")),
        ]
    )


def _done(graph: dag.Graph, *done_ids: str) -> dag.Graph:
    tasks = [
        dag.Task(t.id, t.title, t.kind, t.blocked_by, "done" if t.id in done_ids else t.status, t.test)
        for t in graph.tasks
    ]
    return dag.Graph.from_tasks(tasks)


def test_fan_out() -> None:
    assert _graph().fan_out() == {"T-001": 2, "T-002": 1, "T-003": 1, "T-004": 0}


def test_dependents_closure() -> None:
    g = _graph()  # T-001 →(T-002,T-003)→ T-004
    assert g.dependents_closure(["T-001"]) == {"T-002", "T-003", "T-004"}  # 推移的・自身は含めない
    assert g.dependents_closure(["T-002"]) == {"T-004"}
    assert g.dependents_closure(["T-004"]) == set()  # 葉は被依存なし
    assert g.dependents_closure(["T-999"]) == set()  # 未知 seed は無視
    assert g.dependents_closure(["T-002", "T-003"]) == {"T-004"}  # 重複は1回
    # 相互依存する seed（T-002 は T-001 の下流）も結果から除外する＝seed と波及は排他。
    assert g.dependents_closure(["T-001", "T-002"]) == {"T-003", "T-004"}


def test_frontier_only_roots_when_nothing_done() -> None:
    assert [t.id for t in _graph().frontier()] == ["T-001"]


def test_frontier_opens_after_foundation_done() -> None:
    assert [t.id for t in _done(_graph(), "T-001").frontier()] == ["T-002", "T-003"]


def test_layers() -> None:
    assert _graph().layers() == [["T-001"], ["T-002", "T-003"], ["T-004"]]


def test_critical_path_is_deterministic() -> None:
    assert _graph().critical_path() == ["T-001", "T-002", "T-004"]


def test_order_frontier_prefers_foundation() -> None:
    assert [t.id for t in _graph().order_frontier()] == ["T-001"]


def test_order_frontier_prefers_critical_then_id() -> None:
    # 基盤 done 後、両葉とも fan-out=1。クリティカルパス上の T-002 が先。
    assert [t.id for t in _done(_graph(), "T-001").order_frontier()] == ["T-002", "T-003"]


def test_counts() -> None:
    counts = _done(_graph(), "T-001").counts()
    assert counts["done"] == 1
    assert counts["todo"] == 3


def test_cycle_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks(
            [
                dag.Task(id="A", title="a", kind="parallel", blocked_by=("B",)),
                dag.Task(id="B", title="b", kind="parallel", blocked_by=("A",)),
            ]
        )


def test_unknown_dependency_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks([dag.Task(id="A", title="a", kind="parallel", blocked_by=("X",))])


def test_duplicate_id_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks(
            [
                dag.Task(id="A", title="a", kind="parallel"),
                dag.Task(id="A", title="a2", kind="parallel"),
            ]
        )


def test_load_from_yaml(tmp_path: object) -> None:
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n"
        "  - id: T-001\n    title: 基盤\n    kind: foundation\n    blockedBy: []\n    status: done\n"
        "  - id: T-002\n    title: 葉\n    kind: parallel\n    blockedBy: [T-001]\n    status: todo\n",
        encoding="utf-8",
    )
    graph = dag.load(str(p))
    assert [t.id for t in graph.frontier()] == ["T-002"]


def test_load_rejects_non_mapping_task(tmp_path: object) -> None:
    # task 要素がスカラ（マッピングでない）場合は AttributeError ではなく DagError を投げる。
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text("tasks:\n  - T-001\n", encoding="utf-8")
    with pytest.raises(dag.DagError):
        dag.load(str(p))


def test_load_reads_req_and_phase(tmp_path: object) -> None:
    # req/phase は任意メタ。未指定は既定（""／build）。
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n"
        "  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n"
        "    status: todo\n    req: R-1\n    phase: verify\n"
        "  - id: T-002\n    title: y\n    kind: parallel\n"
        "    blockedBy: [T-001]\n    status: todo\n",
        encoding="utf-8",
    )
    g = dag.load(str(p))
    assert (g.get("T-001").req, g.get("T-001").phase) == ("R-1", "verify")
    assert (g.get("T-002").req, g.get("T-002").phase) == ("", "build")


def test_load_req_phase_empty_value_defaults(tmp_path: object) -> None:
    # キーだけ書いて値が空（YAML null）でも 'None' 文字列にせず既定（""／build）にする。
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n"
        "    status: todo\n    req:\n    phase:\n",
        encoding="utf-8",
    )
    t = dag.load(str(p)).get("T-001")
    assert (t.req, t.phase) == ("", "build")


def test_mermaid_structure() -> None:
    out = dag.mermaid(_done(_graph(), "T-001"))
    lines = out.splitlines()
    assert lines[0] == "```mermaid"
    assert lines[1] == "graph TD"
    assert out.endswith("```")
    # ノードは `-`→`_` でサニタイズ、ラベルは原文の id: title。
    assert '  T_001["T-001: 基盤"]' in lines
    assert '  T_004["T-004: 統合"]' in lines
    # blockedBy ごとの辺（依存→被依存）。
    assert "  T_001 --> T_002" in lines
    assert "  T_002 --> T_004" in lines
    assert "  T_003 --> T_004" in lines
    # status の色分け（done と todo を別クラスに割当）。
    assert "  classDef done fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;" in lines
    assert "  class T_001 done;" in lines
    # クリティカルパス強調（T-001→T-002→T-004 等が critical クラス）。
    assert any(line.startswith("  class ") and line.endswith(" critical;") for line in lines)


def test_mermaid_is_deterministic() -> None:
    g = _done(_graph(), "T-001")
    assert dag.mermaid(g) == dag.mermaid(g)


def test_mermaid_empty_graph() -> None:
    out = dag.mermaid(dag.Graph.from_tasks([]))
    assert "graph TD" in out
    assert 'empty["（タスクなし）"]' in out
    assert out.strip().endswith("```")


# ---- 整合性トレース（要件→設計→タスク） ----------------------------------------


def test_parse_requirement_ids_from_headings() -> None:
    text = (
        "# 要件\n\n## 一覧\n\n### R-1: ログイン\n本文\n### R-2: 検索\n"
        "<!-- R-3 はコメントなので拾わない -->\n本文に R-9 と書いても拾わない\n"
    )
    # 見出しのみ・出現順・重複排除。コメント/本文の R-x は拾わない。
    assert dag.parse_requirement_ids(text) == ["R-1", "R-2"]


def test_parse_requirement_ids_dedupes() -> None:
    assert dag.parse_requirement_ids("### R-1 → 設計\n### R-1 補足\n### R-2 → 設計\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_multiple_per_heading() -> None:
    # 1見出しが複数要件を束ねる場合（共通設計節）は全IDを拾う。
    assert dag.parse_requirement_ids("### R-1, R-2 → 共通設計\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_any_heading_level() -> None:
    # 見出しの深さ(# の数)には結合しない（H1〜H6 のいずれでも拾う）。
    assert dag.parse_requirement_ids("# R-1\n###### R-2\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_ignores_code_fences() -> None:
    # コードフェンス内の例示見出しは実IDと誤認しない。
    text = "### R-1: 本物\n\n```\n### R-99 → 設計（ドキュメント例）\n```\n"
    assert dag.parse_requirement_ids(text) == ["R-1"]


def test_parse_requirement_ids_matches_before_cjk() -> None:
    # 区切り無しで CJK が続いても拾う（末尾 \b に依存しない）。R-12 を R-1 と取り違えない。
    assert dag.parse_requirement_ids("### R-1ログイン\n### R-12 検索\n") == ["R-1", "R-12"]


def test_task_req_ids_splits_field() -> None:
    # カンマ・空白いずれの区切りも受け、重複は排除する。
    assert dag.task_req_ids(dag.Task("T-1", "x", "parallel", req="R-1, R-3")) == ["R-1", "R-3"]
    assert dag.task_req_ids(dag.Task("T-2", "y", "parallel", req="R-1 R-3")) == ["R-1", "R-3"]
    assert dag.task_req_ids(dag.Task("T-3", "z", "parallel", req="R-1, R-1")) == ["R-1"]
    assert dag.task_req_ids(dag.Task("T-4", "w", "parallel", req="")) == []


def test_from_tasks_rejects_malformed_req() -> None:
    # req トークンは load 時に R-<番号> 形式を検証（typo を見逃さない）。
    for bad in ("R1", "Req-1", "R-", "R-1x"):
        with pytest.raises(dag.DagError, match="req"):
            dag.Graph.from_tasks([dag.Task("T-001", "x", "foundation", req=bad)])


def _trace_graph() -> dag.Graph:
    return dag.Graph.from_tasks(
        [
            dag.Task("T-001", "基盤", "foundation", req="R-1"),
            dag.Task("T-002", "葉", "parallel", blocked_by=("T-001",), req="R-2"),
        ]
    )


def test_trace_all_connected() -> None:
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], ["R-1", "R-2"])
    assert report.ok
    assert report.req_to_tasks == {"R-1": ["T-001"], "R-2": ["T-002"]}
    assert report.uncovered_requirements == ()


def test_trace_detects_uncovered_requirement() -> None:
    # R-3 はどのタスクも担っていない（要件→タスクが途切れ）。
    report = dag.trace(_trace_graph(), ["R-1", "R-2", "R-3"], ["R-1", "R-2", "R-3"])
    assert not report.ok
    assert report.uncovered_requirements == ("R-3",)


def test_trace_detects_requirement_missing_design() -> None:
    # R-2 が設計に無い（要件→設計が途切れ）。
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], ["R-1"])
    assert not report.ok
    assert report.requirements_missing_design == ("R-2",)


def test_trace_detects_unknown_refs() -> None:
    g = dag.Graph.from_tasks([dag.Task("T-001", "x", "foundation", req="R-9")])
    report = dag.trace(g, ["R-1"], ["R-1", "R-7"])
    assert not report.ok
    assert report.unknown_in_tasks == (("T-001", "R-9"),)
    assert report.unknown_in_design == ("R-7",)


def test_trace_warns_build_task_without_req_but_stays_ok() -> None:
    # req 未設定の build タスクは WARN（ok は崩さない）。verify 工程は対象外。
    g = dag.Graph.from_tasks(
        [
            dag.Task("T-001", "x", "foundation", req="R-1"),
            dag.Task("T-002", "build無req", "parallel", blocked_by=("T-001",)),
            dag.Task("T-003", "verify無req", "parallel", blocked_by=("T-001",), phase="verify"),
        ]
    )
    report = dag.trace(g, ["R-1"], ["R-1"])
    assert report.ok  # WARN だけなら ok
    assert report.tasks_without_req == ("T-002",)


def test_trace_skips_design_dimension_when_absent() -> None:
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], None)
    assert report.design_checked is False
    assert report.requirements_missing_design == ()
    assert report.ok


def test_trace_verify_task_does_not_cover_requirement() -> None:
    # verify 工程のタスクだけが req を持つ要件は「実装計画に落ちていない」＝未カバー。
    g = dag.Graph.from_tasks(
        [
            dag.Task("T-001", "build", "foundation", req="R-1"),
            dag.Task("T-002", "verifyのみ", "parallel", blocked_by=("T-001",), req="R-2", phase="verify"),
        ]
    )
    report = dag.trace(g, ["R-1", "R-2"], ["R-1", "R-2"])
    assert not report.ok
    assert report.uncovered_requirements == ("R-2",)  # verify タスクはカバレッジに数えない
    assert report.req_to_tasks == {"R-1": ["T-001"], "R-2": []}


def test_trace_unknown_ref_flagged_regardless_of_phase() -> None:
    # 宙吊り参照（要件に無い R）は工程に関わらず ERROR。
    g = dag.Graph.from_tasks([dag.Task("T-001", "verify", "foundation", req="R-9", phase="verify")])
    report = dag.trace(g, ["R-1"], ["R-1"])
    assert report.unknown_in_tasks == (("T-001", "R-9"),)


def test_trace_cli_returns_nonzero_on_gap(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n### R-2: b\n", encoding="utf-8")  # R-2 は未カバー
    design = tmp_path / "design.md"
    design.write_text("### R-1 → 設計\n### R-2 → 設計\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(design)])
    assert rc == 1


def test_trace_cli_ok_when_connected(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    design = tmp_path / "design.md"
    design.write_text("### R-1 → 設計\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(design)])
    assert rc == 0


def _trace_tasks_file(tmp_path: Path) -> Path:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    return tasks


def test_trace_cli_exit2_when_requirements_missing(tmp_path: Path) -> None:
    # 要件ドキュメント不在は「検査不能」＝終了2（欠落の 1 と区別）。
    tasks = _trace_tasks_file(tmp_path)
    rc = dag.main([str(tasks), "--trace", "--requirements", str(tmp_path / "missing.md")])
    assert rc == 2


def test_trace_cli_exit2_when_no_requirement_ids(tmp_path: Path) -> None:
    # 要件ファイルは在るが要件ID を1件も抽出できない＝検査不能＝終了2。
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("# 要件\n本文だけで R 見出しが無い\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs)])
    assert rc == 2


def test_trace_cli_design_absent_skips_with_exit0(tmp_path: Path) -> None:
    # 設計不在は既定ではスキップ（要件カバレッジが OK なら終了0）。
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(tmp_path / "none.md")])
    assert rc == 0


def test_trace_cli_require_design_exit2_when_design_missing(tmp_path: Path) -> None:
    # --require-design 指定時は設計不在を許さず終了2（設計承認済みフェーズのゲート用）。
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main(
        [str(tasks), "--trace", "--requirements", str(reqs), "--design", str(tmp_path / "none.md"), "--require-design"]
    )
    assert rc == 2


def test_trace_cli_exit2_when_tasks_yaml_missing(tmp_path: Path) -> None:
    # tasks.yaml が読めない＝トレース不成立＝終了2（render 等の汎用 1 と区別）。
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main([str(tmp_path / "none.yaml"), "--trace", "--requirements", str(reqs)])
    assert rc == 2
