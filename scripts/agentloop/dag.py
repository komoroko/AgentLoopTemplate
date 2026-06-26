"""タスクグラフ(DAG)の確定的な導出ユーティリティ。

`.agentloop/tasks.yaml` を読み、実行可能フロンティア・実行レイヤ・クリティカルパス・
fan-out を **blockedBy から決定的に導出** する純粋関数群を提供する。
scripts/build_loop.py（消化順の決定）と /status（`--render`）が共用する。

導出値（fan-out 等）はファイルに保存しない。常にグラフから計算するため drift しない。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# status の取り得る値。done のみが依存解決済みとみなされる。
STATUS_VALUES = frozenset({"todo", "in_progress", "blocked", "needs-revision", "done"})
KIND_VALUES = frozenset({"foundation", "parallel", "integration"})


class DagError(ValueError):
    """tasks.yaml の不整合（循環・未知の依存・重複ID・不正値）を表す。"""


@dataclass(frozen=True)
class Task:
    """tasks.yaml の1タスク。導出値（fan-out 等）は持たない。"""

    id: str
    title: str
    kind: str
    blocked_by: tuple[str, ...] = ()
    status: str = "todo"
    test: str = ""

    @property
    def is_done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class Graph:
    """検証済みのタスクDAG。`load`/`from_tasks` 経由でのみ生成する。"""

    tasks: tuple[Task, ...]
    _by_id: dict[str, Task] = field(default_factory=dict)

    @classmethod
    def from_tasks(cls, tasks: list[Task]) -> Graph:
        by_id: dict[str, Task] = {}
        for t in tasks:
            if t.id in by_id:
                raise DagError(f"タスクIDが重複しています: {t.id}")
            if t.kind not in KIND_VALUES:
                raise DagError(f"{t.id}: 不正な kind '{t.kind}'（{sorted(KIND_VALUES)} のいずれか）")
            if t.status not in STATUS_VALUES:
                raise DagError(f"{t.id}: 不正な status '{t.status}'（{sorted(STATUS_VALUES)} のいずれか）")
            by_id[t.id] = t
        for t in tasks:
            for dep in t.blocked_by:
                if dep not in by_id:
                    raise DagError(f"{t.id}: 未知の依存 '{dep}' を参照しています")
                if dep == t.id:
                    raise DagError(f"{t.id}: 自分自身に依存しています")
        graph = cls(tasks=tuple(tasks), _by_id=by_id)
        graph._ensure_acyclic()
        return graph

    def get(self, task_id: str) -> Task:
        return self._by_id[task_id]

    def _ensure_acyclic(self) -> None:
        # Kahn 法で全ノードを取り出せなければ循環している。
        if len(self._topo_order()) != len(self.tasks):
            raise DagError("依存グラフに循環があります（DAG ではありません）")

    def _topo_order(self) -> list[str]:
        """決定的なトポロジカル順（同位は id 昇順）。循環時は取り出せた分のみ返す。"""
        indegree = {t.id: len(t.blocked_by) for t in self.tasks}
        dependents = self._dependents_map()
        ready = sorted(tid for tid, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            newly: list[str] = []
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    newly.append(child)
            # 決定性のため毎回ソートして取り込む。
            ready = sorted(ready + newly)
        return order

    def _dependents_map(self) -> dict[str, list[str]]:
        """各タスク -> それに直接依存する（被依存）タスクIDのリスト。"""
        dependents: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.blocked_by:
                dependents[dep].append(t.id)
        return dependents

    # ---- 導出 -------------------------------------------------------------

    def fan_out(self) -> dict[str, int]:
        """各タスクの被依存数（直接そのタスクを待っているタスクの数）。"""
        return {tid: len(children) for tid, children in self._dependents_map().items()}

    def frontier(self) -> list[Task]:
        """今すぐ着手できる todo（status==todo かつ blockedBy が全て done）。id 昇順。"""
        result = [t for t in self.tasks if t.status == "todo" and all(self.get(dep).is_done for dep in t.blocked_by)]
        return sorted(result, key=lambda t: t.id)

    def layers(self) -> list[list[str]]:
        """構造的な実行レイヤ。レイヤ深さ = 依存の最長段数。各レイヤ内は id 昇順。"""
        depth: dict[str, int] = {}
        for tid in self._topo_order():
            deps = self.get(tid).blocked_by
            depth[tid] = 1 + max((depth[d] for d in deps), default=-1)
        max_depth = max(depth.values(), default=-1)
        return [sorted(tid for tid, d in depth.items() if d == level) for level in range(max_depth + 1)]

    def critical_path(self) -> list[str]:
        """最長チェーン（ノード数最大の依存経路）。同長は id 昇順で決定的に1本選ぶ。"""
        length: dict[str, int] = {}
        pred: dict[str, str | None] = {}
        for tid in self._topo_order():
            best_len = 0
            best_pred: str | None = None
            for dep in sorted(self.get(tid).blocked_by):
                if length[dep] > best_len:
                    best_len, best_pred = length[dep], dep
            length[tid] = best_len + 1
            pred[tid] = best_pred
        if not length:
            return []
        end = min(length, key=lambda t: (-length[t], t))
        path: list[str] = []
        node: str | None = end
        while node is not None:
            path.append(node)
            node = pred[node]
        return list(reversed(path))

    def order_frontier(self) -> list[Task]:
        """最適消化順に並べたフロンティア。

        優先度: ①基盤・高 fan-out → ②クリティカルパス上 → ③その他。同点は id 昇順で決定的。
        """
        fan = self.fan_out()
        on_critical = set(self.critical_path())
        return sorted(
            self.frontier(),
            key=lambda t: (
                0 if t.kind == "foundation" else 1,
                -fan[t.id],
                0 if t.id in on_critical else 1,
                t.id,
            ),
        )

    def counts(self) -> dict[str, int]:
        """status 別の件数。"""
        result = {s: 0 for s in STATUS_VALUES}
        for t in self.tasks:
            result[t.status] += 1
        return result


def _task_from_raw(raw: dict[str, object]) -> Task:
    if "id" not in raw:
        raise DagError(f"id の無いタスクがあります: {raw!r}")
    blocked = raw.get("blockedBy", []) or []
    if not isinstance(blocked, list):
        raise DagError(f"{raw['id']}: blockedBy はリストである必要があります")
    return Task(
        id=str(raw["id"]),
        title=str(raw.get("title", "")),
        kind=str(raw.get("kind", "parallel")),
        blocked_by=tuple(str(d) for d in blocked),
        status=str(raw.get("status", "todo")),
        test=str(raw.get("test", "")),
    )


def load(path: str | Path = ".agentloop/tasks.yaml") -> Graph:
    """tasks.yaml を読み込み、検証済みの Graph を返す。"""
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise DagError("tasks.yaml の 'tasks' はリストである必要があります")
    return Graph.from_tasks([_task_from_raw(r) for r in raw_tasks])


def render(graph: Graph) -> str:
    """/status 用の確定レンダリング（実行レイヤ・クリティカルパス・フロンティア・件数）。"""
    lines: list[str] = []
    counts = graph.counts()
    lines.append("## 実行プラン（tasks.yaml から確定導出）")
    lines.append("")
    lines.append(
        "件数: " + " / ".join(f"{s}={counts[s]}" for s in ("todo", "in_progress", "blocked", "needs-revision", "done"))
    )
    lines.append("")
    lines.append("### 実行レイヤ（同一レイヤ内は並列可能）")
    layers = graph.layers()
    if layers:
        for i, layer in enumerate(layers):
            lines.append(f"- L{i}: {', '.join(layer)}")
    else:
        lines.append("- （タスクなし）")
    lines.append("")
    critical = graph.critical_path()
    lines.append("### クリティカルパス（最長チェーン）")
    lines.append("- " + (" → ".join(critical) if critical else "（タスクなし）"))
    lines.append("")
    lines.append("### 現在の実行可能フロンティア（最適消化順）")
    ordered = graph.order_frontier()
    if ordered:
        fan = graph.fan_out()
        for t in ordered:
            lines.append(f"- {t.id} [{t.kind}, fan-out={fan[t.id]}] {t.title}")
    else:
        lines.append("- （着手可能な todo なし）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tasks.yaml から DAG を確定導出する")
    parser.add_argument("path", nargs="?", default=".agentloop/tasks.yaml", help="tasks.yaml のパス")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="/status 用のサマリを出力")
    group.add_argument("--frontier", action="store_true", help="最適消化順のフロンティアID を改行区切りで出力")
    group.add_argument("--validate", action="store_true", help="DAG の整合のみ検証（出力なし・異常時は非0）")
    args = parser.parse_args(argv)

    try:
        graph = load(args.path)
    except (OSError, DagError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.frontier:
        print("\n".join(t.id for t in graph.order_frontier()))
    elif args.validate:
        pass  # load 成功＝検証OK
    else:  # --render（既定）
        print(render(graph))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
