"""tasks.yaml を GitHub Issues へ一方向ミラーする（人向けの可視化・opt-in）。

`.agentloop/tasks.yaml`（タスクグラフの SSOT）を**真実**として、各タスク T-NNN を GitHub Issue
1件へ冪等に射影する。**一方向のみ**——Issues 側の編集は読み戻さない（確定駆動・オフライン性を保つ）。
orchestrator（build_loop.py）の制御フローには入れず、副作用としてスラッシュコマンドから呼ばれる。

挙動:
  - 既定オフ。`.agentloop/config.yaml` の `github.enabled: true` で有効化。
  - `gh` 不在 / remote 不在 / 無効 のいずれかなら **明示メッセージを出して 0 終了**（自動スキップ）。
  - issue 番号は tasks.yaml に書かない。`gh issue list` の結果をタイトル接頭辞 `T-NNN:` で突き合わせる
    （T-NNN は dag が一意性を検証済み）。これで SSOT を汚さず drift を防ぐ。
  - 無い issue は作成、内容差分があれば更新、`status==done` は close（`close_on_done`）。削除はしない。

`--dry-run` は `gh` を一切呼ばず、tasks.yaml から作成予定の plan を出力する（オフライン・テスト用）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import dag
import yaml

CONFIG_PATH = ".agentloop/config.yaml"


class IssueSyncError(RuntimeError):
    """gh 連携の失敗を表す。"""


@dataclass(frozen=True)
class GithubConfig:
    enabled: bool
    label: str
    close_on_done: bool
    repo: str

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> GithubConfig:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        gh = (data.get("github") if isinstance(data, dict) else None) or {}
        return cls(
            enabled=bool(gh.get("enabled", False)),
            label=str(gh.get("label", "agentloop")),
            close_on_done=bool(gh.get("close_on_done", True)),
            repo=str(gh.get("repo", "") or ""),
        )


@dataclass(frozen=True)
class DesiredIssue:
    title: str
    body: str
    labels: tuple[str, ...]
    closed: bool


@dataclass(frozen=True)
class ExistingIssue:
    number: int
    title: str
    state: str  # "OPEN" | "CLOSED"
    labels: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class Action:
    op: str  # create | update | close | reopen
    task_id: str
    number: int | None
    desired: DesiredIssue
    add_labels: tuple[str, ...] = field(default=())
    remove_labels: tuple[str, ...] = field(default=())


# --- 純粋ロジック（テスト対象） --------------------------------------------


def _managed(label: str, base_label: str) -> bool:
    """このツールが管理するラベルか（agentloop / kind: / status: / phase: / req:）。他人のラベルは触らない。"""
    return label == base_label or label.startswith(("kind:", "status:", "phase:", "req:"))


def _req_tokens(req: str) -> list[str]:
    """対応要件フィールドをラベル用トークンに分解する（カンマ区切り対応、空は除外）。"""
    return [tok.strip() for tok in req.split(",") if tok.strip()]


def _issue_body(task: dag.Task) -> str:
    deps = ", ".join(task.blocked_by) if task.blocked_by else "なし"
    return "\n".join(
        [
            f"`{task.id}` — AgentLoop タスク（SSOT: `.agentloop/tasks.yaml` / 詳細: `docs/tasks/{task.id}.md`）",
            "",
            f"- 種別(kind): {task.kind}",
            f"- 工程(phase): {task.phase}",
            f"- 対応要件(req): {task.req or '(未設定)'}",
            f"- 依存(blockedBy): {deps}",
            f"- テスト: {task.test or '(未設定)'}",
            "",
            "> この issue は tasks.yaml からの **一方向ミラー**。ここを編集しても SSOT には反映されません。",
        ]
    )


def desired_issue(task: dag.Task, *, base_label: str, close_on_done: bool) -> DesiredIssue:
    labels = [base_label, f"kind:{task.kind}", f"status:{task.status}", f"phase:{task.phase}"]
    labels += [f"req:{token}" for token in _req_tokens(task.req)]
    return DesiredIssue(
        title=f"{task.id}: {task.title}",
        body=_issue_body(task),
        labels=tuple(labels),
        closed=task.is_done and close_on_done,
    )


def _content_differs(ex: ExistingIssue, desired: DesiredIssue, base_label: str) -> bool:
    ex_managed = {label for label in ex.labels if _managed(label, base_label)}
    return ex.title != desired.title or ex.body != desired.body or ex_managed != set(desired.labels)


def plan_actions(
    tasks: tuple[dag.Task, ...],
    existing_by_id: dict[str, ExistingIssue],
    *,
    base_label: str,
    close_on_done: bool,
) -> list[Action]:
    """tasks と既存 issue 群から、確定的な差分操作リストを導出する（id 昇順）。"""
    actions: list[Action] = []
    for task in sorted(tasks, key=lambda t: t.id):
        desired = desired_issue(task, base_label=base_label, close_on_done=close_on_done)
        ex = existing_by_id.get(task.id)
        if ex is None:
            actions.append(Action("create", task.id, None, desired, add_labels=desired.labels))
            continue
        if _content_differs(ex, desired, base_label):
            ex_managed = {label for label in ex.labels if _managed(label, base_label)}
            add = tuple(sorted(set(desired.labels) - ex_managed))
            remove = tuple(sorted(ex_managed - set(desired.labels)))
            actions.append(Action("update", task.id, ex.number, desired, add_labels=add, remove_labels=remove))
        if desired.closed and ex.state == "OPEN":
            actions.append(Action("close", task.id, ex.number, desired))
        elif not desired.closed and ex.state == "CLOSED":
            actions.append(Action("reopen", task.id, ex.number, desired))
    return actions


def format_plan(actions: list[Action]) -> str:
    if not actions:
        return "（ミラー差分なし: Issues は tasks.yaml と一致）"
    return "\n".join(f"- {a.op:<6} {a.task_id} :: {a.desired.title}" for a in actions)


# --- ラベル provisioning（gh issue create は対象ラベルが repo に無いと失敗するため冪等に作る） ----

_DEFAULT_COLOR = "ededed"
_BASE_COLOR = "cccccc"
_REQ_COLOR = "0a3069"
_KIND_COLORS = {"foundation": "8250df", "parallel": "0969da", "integration": "1a7f37"}
_STATUS_COLORS = {
    "todo": "eeeeee",
    "in_progress": "bf8700",
    "blocked": "cf222e",
    "needs-revision": "e16f24",
    "done": "1a7f37",
}
# 工程の語彙（既定 build）。requirements/design は「要件・設計そのものを扱う作業」を起票したい場合の受け皿。
_PHASE_VALUES = ("requirements", "design", "build", "verify")
_PHASE_COLORS = {"requirements": "8250df", "design": "0969da", "build": "1a7f37", "verify": "cf222e"}


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


def label_specs(graph: dag.Graph, base_label: str) -> list[LabelSpec]:
    """このツールが使う全ラベルの確定集合。固定（kind/status/phase）＋動的（現タスクの req）。"""
    specs: list[LabelSpec] = [LabelSpec(base_label, _BASE_COLOR, "AgentLoop タスクのミラー issue")]
    for kind in sorted(dag.KIND_VALUES):
        specs.append(LabelSpec(f"kind:{kind}", _KIND_COLORS.get(kind, _DEFAULT_COLOR), f"種別: {kind}"))
    for status in sorted(dag.STATUS_VALUES):
        specs.append(LabelSpec(f"status:{status}", _STATUS_COLORS.get(status, _DEFAULT_COLOR), f"状態: {status}"))
    for phase in _PHASE_VALUES:
        specs.append(LabelSpec(f"phase:{phase}", _PHASE_COLORS.get(phase, _DEFAULT_COLOR), f"工程: {phase}"))
    reqs: set[str] = set()
    for task in graph.tasks:
        reqs.update(_req_tokens(task.req))
    for token in sorted(reqs):
        specs.append(LabelSpec(f"req:{token}", _REQ_COLOR, f"対応要件: {token}"))
    return specs


# --- gh 実行 ---------------------------------------------------------------


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def preflight(cfg: GithubConfig) -> tuple[bool, str]:
    """連携可能かを判定する。不可なら (False, 理由) を返し、呼び出し側は 0 終了でスキップする。"""
    if not cfg.enabled:
        return False, "github.enabled=false のため Issues ミラーをスキップしました。"
    if shutil.which("gh") is None:
        return False, "gh CLI が見つからないため Issues ミラーをスキップしました。"
    if not cfg.repo:
        rc, out = _run(["git", "remote"])
        if rc != 0 or not out.strip():
            return False, "git remote が無いため Issues ミラーをスキップしました（config の github.repo で明示も可）。"
    return True, ""


def fetch_existing(cfg: GithubConfig) -> dict[str, ExistingIssue]:
    args = [
        "gh",
        "issue",
        "list",
        "--label",
        cfg.label,
        "--state",
        "all",
        "--json",
        "number,title,state,labels,body",
        "--limit",
        "1000",
    ]
    if cfg.repo:
        args += ["--repo", cfg.repo]
    rc, out = _run(args)
    if rc != 0:
        raise IssueSyncError(f"gh issue list に失敗しました:\n{out[-500:]}")
    try:
        data: Any = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise IssueSyncError(f"gh issue list の出力を解釈できません: {exc}") from exc
    result: dict[str, ExistingIssue] = {}
    for item in data:
        title = str(item.get("title", ""))
        task_id = title.split(":", 1)[0].strip()
        labels = tuple(str(label.get("name", "")) for label in (item.get("labels") or []))
        result[task_id] = ExistingIssue(
            number=int(item["number"]),
            title=title,
            state=str(item.get("state", "")).upper(),
            labels=labels,
            body=str(item.get("body", "")),
        )
    return result


def _gh(args: list[str], cfg: GithubConfig) -> tuple[int, str]:
    cmd = ["gh", *args]
    if cfg.repo:
        cmd += ["--repo", cfg.repo]
    return _run(cmd)


def ensure_labels(graph: dag.Graph, cfg: GithubConfig) -> None:
    """使用ラベルを冪等に provisioning する（--force で作成/更新）。best-effort（失敗で raise しない）。"""
    for spec in label_specs(graph, cfg.label):
        _gh(["label", "create", spec.name, "--color", spec.color, "--description", spec.description, "--force"], cfg)


def _apply_one(action: Action, cfg: GithubConfig) -> None:
    if action.op == "create":
        args = ["issue", "create", "--title", action.desired.title, "--body", action.desired.body]
        for label in action.desired.labels:
            args += ["--label", label]
        rc, out = _gh(args, cfg)
        if rc != 0:
            raise IssueSyncError(f"{action.task_id}: issue 作成に失敗:\n{out[-500:]}")
        if action.desired.closed:
            # gh issue create は URL を出力する。stdout+stderr に警告が混ざっても確実に番号を取るため
            # /issues/<n> を正規表現で抽出する（末尾分割だと余分な出力で壊れる）。
            match = re.search(r"/issues/(\d+)", out)
            if match:
                _gh(["issue", "close", match.group(1)], cfg)
    elif action.op == "update":
        args = ["issue", "edit", str(action.number), "--title", action.desired.title, "--body", action.desired.body]
        if action.add_labels:
            args += ["--add-label", ",".join(action.add_labels)]
        if action.remove_labels:
            args += ["--remove-label", ",".join(action.remove_labels)]
        rc, out = _gh(args, cfg)
        if rc != 0:
            raise IssueSyncError(f"{action.task_id}: issue 更新に失敗:\n{out[-500:]}")
    elif action.op == "close":
        _gh(["issue", "close", str(action.number)], cfg)
    elif action.op == "reopen":
        _gh(["issue", "reopen", str(action.number)], cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tasks.yaml を GitHub Issues へ一方向ミラーする")
    parser.add_argument("--dry-run", action="store_true", help="gh を呼ばず作成予定を出力（オフライン）")
    args = parser.parse_args(argv)

    cfg = GithubConfig.load()

    if args.dry_run:
        graph = dag.load()
        print("[dry-run] 作成/確認するラベル:")
        print(", ".join(spec.name for spec in label_specs(graph, cfg.label)))
        actions = plan_actions(graph.tasks, {}, base_label=cfg.label, close_on_done=cfg.close_on_done)
        print("[dry-run] Issues ミラー予定（既存 issue は未取得＝全件 create 想定）:")
        print(format_plan(actions))
        return 0

    ready, reason = preflight(cfg)
    if not ready:
        print(reason)
        return 0

    try:
        graph = dag.load()
        ensure_labels(graph, cfg)  # issue 作成前にラベルを provisioning（無いと create が失敗する）
        existing = fetch_existing(cfg)
        actions = plan_actions(graph.tasks, existing, base_label=cfg.label, close_on_done=cfg.close_on_done)
        for action in actions:
            _apply_one(action, cfg)
    except (OSError, dag.DagError, yaml.YAMLError, IssueSyncError) as exc:
        print(f"Issues ミラーに失敗しました（SSOT には影響しません）: {exc}", file=sys.stderr)
        return 1
    print(f"Issues ミラー完了: {len(actions)} 件の操作。" if actions else "Issues は tasks.yaml と一致（操作なし）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
