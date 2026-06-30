"""タスクグラフ(DAG)の確定的な導出ユーティリティ。

`.agentloop/tasks.yaml` を読み、実行可能フロンティア・実行レイヤ・クリティカルパス・
fan-out を **blockedBy から決定的に導出** する純粋関数群を提供する。
scripts/agentloop/build_loop.py（消化順の決定）と /status（`--render`）が共用する。

導出値（fan-out 等）はファイルに保存しない。常にグラフから計算するため drift しない。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# status の取り得る値。done のみが依存解決済みとみなされる。
STATUS_VALUES = frozenset({"todo", "in_progress", "blocked", "needs-revision", "done"})
# STATUS_VALUES の表示順（件数表示・Mermaid 色分けで共用）。
STATUS_ORDER = ("todo", "in_progress", "blocked", "needs-revision", "done")
KIND_VALUES = frozenset({"foundation", "parallel", "integration"})

# 要件ID は `R-<番号>`（R-1, R-2, …）。要件/設計ドキュメントとタスクの req で共通の語彙。
# タスクの req は1トークン全体がこの形であることを load 時に検証する（R1 / Req-1 等の誤記を弾く）。
_REQ_ID_EXACT_RE = re.compile(r"^R-\d+$")


def _split_req(req: str) -> list[str]:
    """req フィールド（"R-1" / "R-1,R-3" / "R-1 R-3"）をトークン列に分解する（カンマ・空白両対応）。"""
    return [tok for tok in re.split(r"[,\s]+", req.strip()) if tok]


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
    # 表示・ラベル専用のメタ（DAG 導出には使わない）。req=対応要件（例 "R-1" / "R-1,R-3"）、
    # phase=ライフサイクル工程（requirements|design|build|verify。既定 build）。
    req: str = ""
    phase: str = "build"

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
            for tok in _split_req(t.req):
                if not _REQ_ID_EXACT_RE.match(tok):
                    raise DagError(f"{t.id}: 不正な req トークン '{tok}'（R-<番号> 形式である必要があります）")
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

    def dependents_closure(self, seed_ids: list[str]) -> set[str]:
        """seed タスク群の **推移的被依存**（直接・間接にそれらに依存する全タスク）を返す。

        差し戻し（/revise）でのタスク影響分析に使う。上流変更で直接影響するタスクを seed に与えると、
        それに連なる下流タスクが漏れなく再レビュー対象として上がる。**seed 自身は結果から除外する**
        （相互依存で seed が別 seed の下流に来ても除外。seed=直接影響、戻り値=その先の波及、という排他集合）。
        未知の seed ID は無視する（呼び出し側で検証済みを想定）。
        """
        dependents = self._dependents_map()
        result: set[str] = set()
        stack = [tid for tid in seed_ids if tid in self._by_id]
        while stack:
            current = stack.pop()
            for child in dependents.get(current, []):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result - set(seed_ids)

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
    if not isinstance(raw, dict):
        raise DagError(f"タスクはマッピング（id/title/... を持つ要素）である必要があります: {raw!r}")
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
        req=str(raw.get("req") or ""),
        phase=str(raw.get("phase") or "build"),
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
    lines.append("件数: " + " / ".join(f"{s}={counts[s]}" for s in STATUS_ORDER))
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


# status -> Mermaid classDef（fill=状態色、critical=太枠）。クラス名は status の `-` を `_` に。
_STATUS_CLASSDEFS = (
    "classDef todo fill:#eeeeee,stroke:#999999,color:#333333;",
    "classDef in_progress fill:#cfe8ff,stroke:#3b82f6,color:#06325e;",
    "classDef blocked fill:#ffd6d6,stroke:#ee2233,color:#7a0010;",
    "classDef needs_revision fill:#ffe9c7,stroke:#f59e0b,color:#7a4a00;",
    "classDef done fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;",
    "classDef critical stroke-width:3px;",
)


def _node_key(task_id: str) -> str:
    """Mermaid ノードID用にサニタイズする（`-` は識別子に使えないため `_` へ）。"""
    return task_id.replace("-", "_")


def mermaid(graph: Graph) -> str:
    """依存グラフを Mermaid（graph TD）で確定出力する。status で色分けし、クリティカルパスを太枠で強調。

    GitHub / VS Code / Markdown でそのまま描画される Mermaid テキストを ```mermaid フェンス付きで返す
    （画像化はオフライン性を崩すため、クライアント側描画に委ねる）。
    """
    tasks = sorted(graph.tasks, key=lambda t: t.id)
    lines: list[str] = ["```mermaid", "graph TD"]
    if not tasks:
        lines.append('  empty["（タスクなし）"]')
        lines.append("```")
        return "\n".join(lines)
    for t in tasks:
        label = f"{t.id}: {t.title}".replace('"', "'")
        lines.append(f'  {_node_key(t.id)}["{label}"]')
    for t in tasks:
        for dep in t.blocked_by:
            lines.append(f"  {_node_key(dep)} --> {_node_key(t.id)}")
    lines.extend(f"  {cd}" for cd in _STATUS_CLASSDEFS)
    for status in STATUS_ORDER:
        ids = [_node_key(t.id) for t in tasks if t.status == status]
        if ids:
            lines.append(f"  class {','.join(ids)} {status.replace('-', '_')};")
    critical = graph.critical_path()
    if critical:
        lines.append(f"  class {','.join(_node_key(i) for i in critical)} critical;")
    lines.append("```")
    return "\n".join(lines)


# ---- 整合性トレース（要件→設計→タスク） --------------------------------------
# 要件ID の糸（R-1, R-2, …）が requirements→design→tasks を切れ目なく貫いているかを
# 確定的に検査する。fan-out 等と同じく「LLM 裁量に委ねない機械チェック」。/tasks ゲートと
# CI で回し、人のレビュー前に「全要件が設計とタスクに連結しているか」を可視化する。

# 要件/設計ドキュメントの **見出し行** から要件ID を拾う。見出し行に限定するので本文・
# コメント中の R-x 言及は拾わない（誤検出を避ける）。見出しの深さ(# の数)には結合しない
# （H1〜H6 のいずれでも拾う）。1見出しに複数IDを書いた場合（例 `### R-1, R-2 → 共通設計`）は
# 全IDを拾う。要件文書とタスク req とで同じ抽出規則を共有する。
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}\s+(.*)$", re.MULTILINE)
# 見出しテキスト中の要件ID。前は語中混入を避け（FOOR-1 を弾く）、後は末尾を数字で切らない
# （R-12 を R-1 と取り違えない）。直後が CJK 等でも拾えるよう末尾 \b は使わない。
_REQ_ID_RE = re.compile(r"(?<![0-9A-Za-z_])R-\d+(?!\d)")
# ``` または ~~~ で囲まれたコードフェンス（例示の見出しを実IDと誤認しないため抽出前に除去する）。
_CODE_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)[^\n]*$.*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)


def parse_requirement_ids(text: str) -> list[str]:
    """要件/設計ドキュメントの見出しから要件ID を **出現順・重複排除** で抽出する。

    コードフェンス内の例示見出しは実IDと誤認しないよう除去してから走査する。
    """
    body = _CODE_FENCE_RE.sub("", text)
    ids = (rid for heading in _HEADING_RE.findall(body) for rid in _REQ_ID_RE.findall(heading))
    return list(dict.fromkeys(ids))  # 出現順を保ったまま重複排除


def task_req_ids(task: Task) -> list[str]:
    """タスクの req フィールドを要件ID のリストに分解する（出現順・重複排除）。

    カンマ・空白いずれの区切りも受ける。各トークンの形式（R-<番号>）は load 時に
    `Graph.from_tasks` が検証済みなので、ここに来る時点で不正トークンは無い。
    """
    return list(dict.fromkeys(_split_req(task.req)))


@dataclass(frozen=True)
class TraceReport:
    """要件→設計→タスクの整合（トレーサビリティ）検査結果。

    要件IDの集合・カバレッジ・未カバーは `req_to_tasks` 一つから導出する（状態を二重化
    しない）。要件順は `req_to_tasks` の挿入順（=要件ドキュメント出現順）が保持する。
    """

    req_to_tasks: dict[str, list[str]]  # 要件ID -> それを実装する build タスクID（要件順 / 値=id昇順）
    design_checked: bool  # 設計次元を検査したか（False=設計ドキュメント未検査）
    requirements_missing_design: tuple[str, ...]  # ERROR: 設計に対応節が無い要件（設計検査時のみ）
    unknown_in_design: tuple[str, ...]  # ERROR: 要件に存在しない R を設計が参照
    unknown_in_tasks: tuple[tuple[str, str], ...]  # ERROR: 要件に存在しない R をタスクが参照 (task_id, R)
    tasks_without_req: tuple[str, ...]  # WARN: req 未設定の build タスク

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        """要件ドキュメント由来の要件ID（出現順）。"""
        return tuple(self.req_to_tasks)

    @property
    def uncovered_requirements(self) -> tuple[str, ...]:
        """ERROR: 担う build タスクが無い要件。"""
        return tuple(r for r, tasks in self.req_to_tasks.items() if not tasks)

    @property
    def ok(self) -> bool:
        """ERROR が一つも無ければ True（WARN は ok を崩さない）。"""
        return not (
            self.uncovered_requirements
            or self.requirements_missing_design
            or self.unknown_in_design
            or self.unknown_in_tasks
        )


def trace(graph: Graph, requirement_ids: list[str], design_ids: list[str] | None) -> TraceReport:
    """要件ID・設計ID・タスクの req を突合し、糸の途切れ（カバレッジ欠落・宙吊り参照）を検出する。

    カバレッジは **build 工程のタスク**だけで判定する（verify 由来のバグ修正等は実装計画
    ではないのでカバレッジに数えない）。要件に存在しない R の参照（宙吊り）は工程に関わらず
    ERROR。design_ids=None なら設計ドキュメント不在として設計次元の検査をスキップする
    （早期フェーズや設計差し戻し直後でも落ちないように）。
    """
    req_set = set(requirement_ids)
    req_to_tasks: dict[str, list[str]] = {r: [] for r in requirement_ids}
    unknown_in_tasks: list[tuple[str, str]] = []
    tasks_without_req: list[str] = []
    for t in sorted(graph.tasks, key=lambda t: t.id):
        ids = task_req_ids(t)
        if not ids:
            # build 工程のタスクは対応要件を持つべき（verify 由来のバグ修正等は対象外）。
            if t.phase == "build":
                tasks_without_req.append(t.id)
            continue
        for r in ids:
            if r not in req_set:
                unknown_in_tasks.append((t.id, r))  # 宙吊り参照（工程に関わらず ERROR）
            elif t.phase == "build":
                req_to_tasks[r].append(t.id)  # カバレッジは build タスクのみ

    requirements_missing_design: tuple[str, ...] = ()
    unknown_in_design: tuple[str, ...] = ()
    if design_ids is not None:
        design_set = set(design_ids)
        requirements_missing_design = tuple(r for r in requirement_ids if r not in design_set)
        unknown_in_design = tuple(d for d in design_ids if d not in req_set)

    return TraceReport(
        req_to_tasks=req_to_tasks,
        design_checked=design_ids is not None,
        requirements_missing_design=requirements_missing_design,
        unknown_in_design=unknown_in_design,
        unknown_in_tasks=tuple(unknown_in_tasks),
        tasks_without_req=tuple(tasks_without_req),
    )


def render_trace(report: TraceReport) -> str:
    """整合性トレースの人間向けレポート（カバレッジ表＋検出一覧）を確定出力する。"""
    lines: list[str] = ["## 整合性トレース（要件→設計→タスク）", ""]
    lines.append("### 要件カバレッジ")
    if report.req_to_tasks:
        missing_design = set(report.requirements_missing_design)
        for r, tasks in report.req_to_tasks.items():
            design_mark = ""
            if report.design_checked:
                design_mark = "設計✗ " if r in missing_design else "設計✓ "
            task_mark = ", ".join(tasks) if tasks else "（タスク無し）"
            lines.append(f"- {r}: {design_mark}{task_mark}")
    else:
        lines.append("- （要件ID が見つかりません）")
    lines.append("")

    problems: list[str] = []
    for r in report.uncovered_requirements:
        problems.append(f"ERROR 要件 {r}: 担うタスクが無い（要件が実装計画に落ちていない）")
    for r in report.requirements_missing_design:
        problems.append(f"ERROR 要件 {r}: 設計に対応節が無い（要件→設計が途切れている）")
    for d in report.unknown_in_design:
        problems.append(f"ERROR 設計が未知の要件 {d} を参照（要件に存在しない）")
    for tid, r in report.unknown_in_tasks:
        problems.append(f"ERROR タスク {tid}: 未知の要件 {r} を参照（要件に存在しない）")
    for tid in report.tasks_without_req:
        problems.append(f"WARN  タスク {tid}: req 未設定（build タスクは対応要件を持つべき）")

    lines.append("### 検出")
    if problems:
        lines.extend(f"- {p}" for p in problems)
    else:
        lines.append("- 問題なし（全要件が設計とタスクに連結している）")
    return "\n".join(lines)


def _read_optional(path: str | Path) -> str | None:
    """存在すれば本文を返し、無ければ None（トレースの次元スキップ用）。

    読めない（不在・ディレクトリ・権限不足など）はすべて None に畳む。--trace 分岐の
    呼び出しは load() を包む try/except の外にあるため、ここで OSError を握らないと
    要件/設計パスのディレクトリ指定・権限エラーが未捕捉のまま main を抜けてしまう。
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


# --trace の既定ドキュメントパス（明示指定が無いときに使う）。
_DEFAULT_REQUIREMENTS = "docs/10-requirements.md"
_DEFAULT_DESIGN = "docs/20-design.md"


def _run_trace(graph: Graph, *, requirements_path: str, design_path: str, require_design: bool) -> int:
    """--trace の実行と終了コード決定。

    終了コードは原因を区別する（CI/ゲートが「何が問題か」を取り違えないため）:
      0 = 整合OK
      1 = トレース欠落（未カバー要件・宙吊り参照など。**要対応**）
      2 = 検査を実施できない（要件ドキュメント不在/要件ID 0件、または設計必須なのに設計不在）
    """
    req_text = _read_optional(requirements_path)
    if req_text is None:
        print(f"error: 要件ドキュメントが読めません: {requirements_path}", file=sys.stderr)
        return 2
    requirement_ids = parse_requirement_ids(req_text)
    if not requirement_ids:
        print(
            f"error: 要件ドキュメントから要件ID(R-N)を抽出できません: {requirements_path}"
            "（見出し行に `### R-1: ...` のように書く）",
            file=sys.stderr,
        )
        return 2
    design_text = _read_optional(design_path)
    if design_text is None and require_design:
        print(
            f"error: 設計ドキュメントが読めません: {design_path}（--require-design 指定時は必須）",
            file=sys.stderr,
        )
        return 2
    design_ids = parse_requirement_ids(design_text) if design_text is not None else None
    report = trace(graph, requirement_ids, design_ids)
    print(render_trace(report))
    if design_ids is None:
        print(f"note: 設計 {design_path} が無いため設計カバレッジは未検査", file=sys.stderr)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tasks.yaml から DAG を確定導出する")
    parser.add_argument("path", nargs="?", default=".agentloop/tasks.yaml", help="tasks.yaml のパス")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="/status 用のサマリを出力")
    group.add_argument("--mermaid", action="store_true", help="依存グラフを Mermaid（graph TD）で出力")
    group.add_argument("--frontier", action="store_true", help="最適消化順のフロンティアID を改行区切りで出力")
    group.add_argument("--validate", action="store_true", help="DAG の整合のみ検証（出力なし・異常時は非0）")
    group.add_argument(
        "--impacted",
        metavar="IDS",
        help="差し戻し影響分析: 指定タスク（カンマ区切り）の推移的被依存を改行区切りで出力",
    )
    group.add_argument(
        "--trace",
        action="store_true",
        help="要件→設計→タスクの整合を検査（終了コード 0=OK / 1=欠落 / 2=検査不能）",
    )
    parser.add_argument(
        "--requirements",
        default=None,
        help=f"要件ドキュメントのパス（--trace 用。既定 {_DEFAULT_REQUIREMENTS}）",
    )
    parser.add_argument(
        "--design",
        default=None,
        help=f"設計ドキュメントのパス（--trace 用。既定 {_DEFAULT_DESIGN}。無ければ設計次元はスキップ）",
    )
    parser.add_argument(
        "--require-design",
        action="store_true",
        help="--trace で設計ドキュメント不在を許さず終了2にする（設計承認済みフェーズのゲート用）",
    )
    args = parser.parse_args(argv)

    if not args.trace and (args.requirements is not None or args.design is not None or args.require_design):
        print(
            "warning: --requirements/--design/--require-design は --trace でのみ有効（無視します）",
            file=sys.stderr,
        )

    try:
        graph = load(args.path)
    except (OSError, DagError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        # --trace のときは「検査不能」を 2 で表す（tasks.yaml が読めない＝トレース不成立）。
        return 2 if args.trace else 1

    if args.frontier:
        print("\n".join(t.id for t in graph.order_frontier()))
    elif args.validate:
        pass  # load 成功＝検証OK
    elif args.mermaid:
        print(mermaid(graph))
    elif args.impacted is not None:
        seeds = [s.strip() for s in args.impacted.split(",") if s.strip()]
        print("\n".join(sorted(graph.dependents_closure(seeds))))
    elif args.trace:
        return _run_trace(
            graph,
            requirements_path=args.requirements or _DEFAULT_REQUIREMENTS,
            design_path=args.design or _DEFAULT_DESIGN,
            require_design=args.require_design,
        )
    else:  # --render（既定）
        print(render(graph))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
