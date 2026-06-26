"""実装フェーズの確定的オーケストレータ（/build の駆動エンジン）。

スケジューリングの制御フロー（どのタスクを・何並列で・どの順にマージし・いつ止めるか）を
**プロンプトではなくコード**で確定的に回す。各タスクの実装コード内容そのものは LLM
（implementer を `claude -p` でヘッドレス起動）が書くため非確定だが、

  - フロンティア計算 / 消化順 / 最大並列数 / worktree 隔離 / マージ順
  - `make test` → `make check` の終了コードによる pass/fail ゲート判定
  - retry 上限 / blocked 判定 / 停止条件 / 前提ゲートの確認

はすべてこのスクリプトが確定的に決める。`.agentloop/config.yaml` が唯一のノブ源。

確定化の境界:
  - 確定（ここ）: 制御フロー・並列・マージ・ゲート判定・停止。
  - 非確定（LLM）: 実装コードと /code-review・/simplify の指摘内容
    → 「ゲートを通るまで retry、駄目なら blocked」で吸収する。

このスクリプトは **gates.build を approved にしない**（ゲートは人だけが開ける）。
全タスク done 後はサマリを出して停止し、人の承認（/build のゲート④）に委ねる。

使い方:
  uv run python scripts/build_loop.py            # 実行
  uv run python scripts/build_loop.py --dry-run  # claude/git を呼ばず制御フローのみ確認
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import dag
import yaml

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"
TASKS_PATH = ".agentloop/tasks.yaml"
LOG_PATH = ".agentloop/build-loop.log"


class StopLoop(Exception):
    """ループを止めて人へエスカレーションする要因。code は終了コード。"""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Config:
    max_parallel: int
    worktree_enabled: bool
    worktree_dir: str
    branch_pattern: str
    test_fix: int
    check_fix: int
    test_cmd: str
    check_cmd: str

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> Config:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        build = data.get("build") or {}
        wt = build.get("worktree") or {}
        retries = build.get("retries") or {}
        qg = build.get("quality_gate") or {}
        return cls(
            max_parallel=int(build.get("max_parallel", 3)),
            worktree_enabled=bool(wt.get("enabled", True)),
            worktree_dir=str(wt.get("dir", ".worktrees")),
            branch_pattern=str(wt.get("branch_pattern", "{branch}/{task_id}")),
            test_fix=int(retries.get("test_fix", 2)),
            check_fix=int(retries.get("check_fix", 2)),
            test_cmd=str(qg.get("test_cmd", "make test")),
            check_cmd=str(qg.get("check_cmd", "make check")),
        )


# --- state.md / tasks.yaml の読み書き --------------------------------------


def read_frontmatter(path: str = STATE_PATH) -> dict[str, object]:
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    loaded = yaml.safe_load(parts[1]) or {}
    return loaded if isinstance(loaded, dict) else {}


def work_branch(front: dict[str, object]) -> str:
    branch = front.get("branch")
    if isinstance(branch, str) and branch and not branch.startswith("<"):
        return branch
    # state.md 未記入なら現在のブランチを使う。
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=".")
    return out.strip() if rc == 0 else "HEAD"


def set_task_status(task_id: str, status: str, tasks_path: str = TASKS_PATH) -> None:
    """tasks.yaml の1タスクの status を更新して書き戻す（機械データなので round-trip 可）。"""
    data = yaml.safe_load(Path(tasks_path).read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    for t in tasks:
        if str(t.get("id")) == task_id:
            t["status"] = status
            break
    header = "# .agentloop/tasks.yaml — タスクグラフ(DAG)の機械可読 SSOT（build_loop が status を更新）\n"
    Path(tasks_path).write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def log_escalation(message: str) -> None:
    with Path(LOG_PATH).open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")
    print(f"[escalation] {message}", file=sys.stderr)


# --- サブプロセス -----------------------------------------------------------


def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


# --- スケジューリング（純粋・テスト対象） -----------------------------------


def plan_batch(graph: dag.Graph, max_parallel: int) -> tuple[str, list[dag.Task]] | None:
    """次に着手するバッチを確定的に決める。

    返り値:
      ("serial", [基盤タスク1個])   — 基盤・高 fan-out は直列に確定する
      ("parallel", [葉タスク 〜max_parallel]) — 独立葉は隔離して並列起動
      None                          — フロンティアが空
    """
    ordered = graph.order_frontier()
    if not ordered:
        return None
    foundations = [t for t in ordered if t.kind == "foundation"]
    if foundations:
        return ("serial", [foundations[0]])
    return ("parallel", ordered[:max_parallel])


# --- オーケストレータ本体 ---------------------------------------------------


class Orchestrator:
    def __init__(self, config: Config, dry_run: bool, claude_bin: str = "claude") -> None:
        self.config = config
        self.dry_run = dry_run
        self.claude_bin = claude_bin
        self.front = read_frontmatter()
        self.branch = work_branch(self.front)

    # -- implementer 起動と品質ゲート --

    def _implementer_prompt(self, task: dag.Task, failure_log: str) -> str:
        prompt = (
            f"あなたは implementer サブエージェントです。担当タスクは {task.id}「{task.title}」のみ。\n"
            f"docs/tasks/{task.id}.md と docs/20-design.md・既存コードを読み、"
            ".claude/agents/implementer.md のプロトコルに従って実装してください。\n"
            f"自動テストを書いて `{self.config.test_cmd}` を green に、`{self.config.check_cmd}` をクリーンにする。\n"
            f'完了したら変更をこのブランチにコミットする: git add -A && git commit -m "{task.id}: <要約>"\n'
            "スコープ外（他タスクの領域）には手を出さない。要件/設計の不備を見つけたら勝手に直さず報告する。"
        )
        if failure_log:
            prompt += f"\n\n前回の品質ゲート失敗を解消してください:\n{failure_log[-3000:]}"
        return prompt

    def _invoke_implementer(self, task: dag.Task, cwd: str, failure_log: str) -> None:
        if self.dry_run:
            print(f"    [dry-run] implementer 起動 (cwd={cwd}) task={task.id}")
            return
        rc, out = _run([self.claude_bin, "-p", self._implementer_prompt(task, failure_log)], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"{task.id}: implementer の起動に失敗 (rc={rc})\n{out[-1000:]}")

    def _quality_gate(self, task: dag.Task, cwd: str) -> tuple[bool, str]:
        """make test → make check を実行し終了コードで確定判定する。"""
        if self.dry_run:
            print(f"    [dry-run] 品質ゲート: {self.config.test_cmd} / {self.config.check_cmd} (cwd={cwd})")
            return True, ""
        for cmd in (self.config.test_cmd, self.config.check_cmd):
            rc, out = _run(cmd.split(), cwd=cwd)
            if rc != 0:
                return False, f"$ {cmd} (rc={rc})\n{out}"
        return True, ""

    def _run_task_to_done(self, task: dag.Task, cwd: str) -> tuple[bool, str]:
        """1タスクを implementer 実装＋品質ゲートで done まで持っていく。

        返り値 (ok, log)。ok=False なら retry 上限超過（呼び出し側で blocked 化）。
        """
        # test_fix と check_fix の大きい方を総試行回数の上限とする。
        attempts = max(self.config.test_fix, self.config.check_fix) + 1
        failure_log = ""
        for i in range(attempts):
            self._invoke_implementer(task, cwd, failure_log)
            ok, failure_log = self._quality_gate(task, cwd)
            if ok:
                return True, ""
            print(f"    品質ゲート fail（試行 {i + 1}/{attempts}）: {task.id}")
        return False, failure_log

    # -- worktree / merge --

    def _git(self, args: list[str], cwd: str = ".") -> None:
        if self.dry_run:
            print(f"    [dry-run] git {' '.join(args)} (cwd={cwd})")
            return
        rc, out = _run(["git", *args], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"git {' '.join(args)} に失敗 (rc={rc})\n{out[-1000:]}")

    def _branch_for(self, task: dag.Task) -> str:
        return self.config.branch_pattern.format(branch=self.branch, task_id=task.id)

    def _worktree_path(self, task: dag.Task) -> str:
        return str(Path(self.config.worktree_dir) / task.id)

    def process_leaf(self, task: dag.Task) -> tuple[str, bool, str]:
        """葉タスクを worktree 隔離で実装する。(branch, ok, log) を返す。マージは呼び出し側。"""
        branch = self._branch_for(task)
        path = self._worktree_path(task)
        self._git(["worktree", "add", "-b", branch, path, self.branch])
        try:
            ok, log = self._run_task_to_done(task, cwd=path)
        finally:
            pass
        return branch, ok, log

    def merge_leaf(self, task: dag.Task, branch: str) -> bool:
        """葉ブランチを work へマージし worktree を撤去する。コンフリクト時は abort して False。"""
        if self.dry_run:
            print(f"    [dry-run] git merge --no-ff {branch} → {self.branch}、worktree 撤去")
            return True
        rc, out = _run(["git", "merge", "--no-ff", "--no-edit", branch], cwd=".")
        if rc != 0:
            _run(["git", "merge", "--abort"], cwd=".")
            log_escalation(f"{task.id}: work へのマージでコンフリクト。手動解消が必要。\n{out[-500:]}")
            return False
        self._git(["worktree", "remove", "--force", self._worktree_path(task)])
        return True

    # -- メインループ --

    def run(self) -> int:
        gates = self.front.get("gates") or {}
        if not (isinstance(gates, dict) and gates.get("tasks") == "approved"):
            print("gates.tasks が approved ではありません。先に /tasks を承認してください。", file=sys.stderr)
            return 2

        while True:
            graph = dag.load(TASKS_PATH)
            counts = graph.counts()
            unfinished = len(graph.tasks) - counts["done"]
            if unfinished == 0:
                return self._present_gate4(graph)

            batch = plan_batch(graph, self.config.max_parallel)
            if batch is None:
                # フロンティア空＆未完あり ＝ 全て blocked/needs-revision。人へ。
                blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
                log_escalation(f"実行可能タスクが無く {unfinished} 件が未完（{', '.join(blocked)}）。人の介入が必要。")
                return 1

            mode, tasks = batch
            print(f"[batch] mode={mode} tasks={[t.id for t in tasks]}")
            try:
                if mode == "serial" or not self.config.worktree_enabled:
                    self._consume_serial(tasks)
                else:
                    self._consume_parallel(tasks)
            except StopLoop as exc:
                print(str(exc), file=sys.stderr)
                return exc.code
            # 1バッチ終えるごとにループ先頭で再計算（チェーン組み直し）。

    def _consume_serial(self, tasks: list[dag.Task]) -> None:
        """基盤タスク等を work ブランチ上で直列に確定する。"""
        for task in tasks:
            set_task_status(task.id, "in_progress")
            print(f"  [serial] {task.id} {task.title}")
            ok, log = self._run_task_to_done(task, cwd=".")
            if not ok:
                set_task_status(task.id, "blocked")
                log_escalation(f"{task.id}: 品質ゲートを規定回数で通せず blocked。\n{log[-500:]}")
                raise StopLoop(f"{task.id} が blocked。人の介入が必要。", code=1)
            if not self.dry_run:
                self._git(["add", "-A"])
                # implementer がコミット済みでなければここで確定（差分があれば）。
                _run(["git", "commit", "-m", f"{task.id}: {task.title}"], cwd=".")
            set_task_status(task.id, "done")

    def _consume_parallel(self, tasks: list[dag.Task]) -> None:
        """独立葉を worktree 隔離で最大 max_parallel 並列実装し、done 順に work へマージ。"""
        for task in tasks:
            set_task_status(task.id, "in_progress")
        results: dict[str, tuple[str, bool, str]] = {}
        with ThreadPoolExecutor(max_workers=self.config.max_parallel) as pool:
            futures = {pool.submit(self.process_leaf, t): t for t in tasks}
            for future, task in futures.items():
                results[task.id] = future.result()

        blocked_any = False
        # 決定的に id 昇順でマージ（sequential join）。
        for task in sorted(tasks, key=lambda t: t.id):
            branch, ok, log = results[task.id]
            if not ok:
                set_task_status(task.id, "blocked")
                log_escalation(f"{task.id}: 品質ゲートを規定回数で通せず blocked。\n{log[-500:]}")
                blocked_any = True
                continue
            if self.merge_leaf(task, branch):
                set_task_status(task.id, "done")
            else:
                set_task_status(task.id, "blocked")
                blocked_any = True
        if blocked_any:
            raise StopLoop("blocked タスクが発生。人の介入が必要。", code=1)

    def _present_gate4(self, graph: dag.Graph) -> int:
        print("\n========== 全タスク done（ゲート④） ==========")
        print(dag.render(graph))
        print(
            "\n次の手順（人の承認が必要）:\n"
            "  1. /security-review を実行し、作業ブランチの差分の脆弱性を解消する。\n"
            "  2. 実装サマリをレビューし、問題なければ /build のゲート④で承認する。\n"
            "  ※ このスクリプトは gates.build を approved にしません（ゲートは人だけが開けます）。"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="実装フェーズの確定的オーケストレータ")
    parser.add_argument("--dry-run", action="store_true", help="claude/git を呼ばず制御フローのみ実行")
    parser.add_argument("--claude-bin", default="claude", help="ヘッドレス起動に使う claude CLI（既定: claude）")
    args = parser.parse_args(argv)
    try:
        config = Config.load()
    except (OSError, yaml.YAMLError) as exc:
        print(f"config 読み込みエラー: {exc}", file=sys.stderr)
        return 1
    return Orchestrator(config, dry_run=args.dry_run, claude_bin=args.claude_bin).run()


if __name__ == "__main__":
    raise SystemExit(main())
