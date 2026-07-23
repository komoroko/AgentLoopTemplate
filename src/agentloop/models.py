"""The 0.9.0 document model: the vocabulary, the typed views, and the validation entry point.

Four artifacts carry the cycle (plan §5.1) and each has exactly one writer:

  ``plan.yaml``    the Expected Model and its Evidence Obligations — frozen at gate ③
  ``state.yaml``   mutable state only: phase, gate receipts, run status, task status
  ``review.yaml``  the machine review and, separately, the human review
  ``events.ndjson`` the append-only hash-chained audit log

**The parsed mapping is the truth; these classes are views over it.** A digest is taken over
that mapping (via :mod:`agentloop.digests`), never over a reconstructed object graph — so a
field this module does not yet know about still contributes to the digest a human signed,
and adding an accessor here can never move a signature. That is the reason `Plan` wraps a
`raw` mapping instead of being a full dataclass tree: a lossy round-trip would be a silent
integrity bug, and there is no test that reliably catches "lossy in a way nobody wrote down".

Validation runs in three layers, all fail-closed:

  1. :mod:`agentloop.strict_yaml` — the document is unambiguous at all (no duplicate keys …)
  2. JSON Schema (``data/schema/*.schema.json``) — shape, ``additionalProperties: false``, enums
  3. :func:`cross_reference_errors` — every ID reference resolves (plan §23)

Layer 2's enums and this module's vocabulary constants are two spellings of one fact, so
``tests/test_models.py`` asserts they agree; drift between them is exactly the kind of quiet
divergence the 0.9.0 release exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentloop import data, digests, strict_yaml

# --- vocabulary ---------------------------------------------------------------
#
# Ordered tuples where display/comparison order matters (risk ladders, gate chains), plain
# frozensets where only membership does. Every one of these appears as an `enum` in a schema.

RISK_ORDER: tuple[str, ...] = ("low", "medium", "high", "critical")
RISK_VALUES = frozenset(RISK_ORDER)
#: Risks that pull in the strict evidence, coverage, and independence requirements.
ELEVATED_RISKS = frozenset({"high", "critical"})

#: The forward gate order. A roll back resets a chain of these (plan §16).
GATE_ORDER: tuple[str, ...] = ("requirements", "design", "tasks", "build", "release")
GATE_VALUES = frozenset(GATE_ORDER)
GATE_STATUS_VALUES = frozenset({"pending", "approved"})

#: current_phase values in lifecycle order (`brief` precedes gate ①, `done` follows gate ⑤).
PHASE_ORDER: tuple[str, ...] = ("brief", "requirements", "design", "tasks", "build", "verify", "done")
PHASE_VALUES = frozenset(PHASE_ORDER)

#: The phase each gate opens the door to — `approve <gate>` advances current_phase to this.
PHASE_AFTER_GATE: Mapping[str, str] = {
    "requirements": "design",
    "design": "tasks",
    "tasks": "build",
    "build": "verify",
    "release": "done",
}

PLAN_STATUS_VALUES = frozenset({"draft", "frozen", "invalidated"})

# Task vocabulary. `kind` is the DAG role that drives build orchestration (consumption order,
# parallelism, merge) — deliberately kept from 0.8.x rather than collapsed into a single
# "implementation", because build_loop derives layers and the critical path from it.
TASK_KIND_ORDER: tuple[str, ...] = ("foundation", "parallel", "integration")
TASK_KIND_VALUES = frozenset(TASK_KIND_ORDER)
TASK_STATUS_ORDER: tuple[str, ...] = ("todo", "in-progress", "blocked", "needs-revision", "done")
TASK_STATUS_VALUES = frozenset(TASK_STATUS_ORDER)

#: Source authority, derived by the Policy Engine — never taken from what an AI wrote (plan §6.2).
#: `assumed` is deliberately absent: "we assumed it" is not a class of evidence.
AUTHORITY_CLASS_VALUES = frozenset(
    {"executable", "normative", "oracle", "expert", "experimental", "descriptive", "inferred"}
)
#: Authority classes that may ground a high/critical claim on their own.
NORMATIVE_AUTHORITY = frozenset({"normative", "executable", "oracle", "expert", "experimental"})

SOURCE_KIND_VALUES = frozenset(
    {
        "official_external_spec",
        "internal_spec",
        "repository_code",
        "repository_test",
        "runtime_trace",
        "human_decision",
        "expert_statement",
        "experiment_receipt",
    }
)

#: What kind of question a claim answers — it decides which evidence path can settle it.
DECISION_CLASS_VALUES = frozenset({"business_policy", "technical_fact", "implementation_choice"})

CLAIM_KIND_VALUES = frozenset({"invariant", "behavior", "constraint", "prohibition"})

#: A claim/technical fact is grounded, or it is honestly not (plan §2.3). There is no fourth
#: value, and prose may not be promoted into the first.
EPISTEMIC_STATUS_VALUES = frozenset({"grounded", "unknown", "conflicted"})

#: Did the search *run*? — kept strictly apart from whether it *sufficed* (plan §6.4).
EXECUTION_STATUS_VALUES = frozenset({"pending", "complete", "failed"})
#: Did the obligation get met?
COVERAGE_STATUS_VALUES = frozenset({"satisfied", "unsatisfied"})
#: Did the search cover the obligation it was run for?
SEARCH_COVERAGE_VALUES = frozenset({"sufficient", "insufficient"})
#: One provider attempt's outcome. `no_match` means "searched, nothing found"; `unavailable`
#: means "could not search" — collapsing them is how "no documentation exists" gets invented.
PROVIDER_RESULT_VALUES = frozenset({"matched", "no_match", "unavailable"})

EVIDENCE_RELATION_VALUES = frozenset({"supports", "contradicts", "context"})

ORACLE_KIND_VALUES = frozenset({"property_test", "conformance_test", "integration_test", "schema_test"})
EXECUTOR_VALUES = frozenset({"oci", "host"})

# The shapes an obligation's alternative evidence path can demand (plan §6.4's `requires`).
ORACLE_MODE_VALUES = frozenset({"hermetic", "observational"})
EXPERIMENT_MODE_VALUES = frozenset({"reproducible", "one_off"})
TEST_LEVEL_VALUES = frozenset({"unit", "integration"})

# Sandbox knobs (plan §10.2).
MOUNT_MODE_VALUES = frozenset({"none", "read_only", "read_write"})
HOME_MODE_VALUES = frozenset({"ephemeral", "host"})
QUALITY_GATE_KIND_VALUES = frozenset({"command", "agent"})
AGENT_ROLE_VALUES = frozenset(
    {
        "implementer",
        "code_reviewer",
        "plan_reviewer",
        "actual_extractor",
        "comparator",
        "security_reviewer",
        "cold_maintainer",
    }
)

# --- review vocabulary (plan §6.7) --------------------------------------------
#
# The single `verified` of 0.8.x is gone. Three axes are reported separately because they
# answer three different questions, and one of them (`semantic_support`) is an opinion.

INTEGRITY_STATUS_VALUES = frozenset({"verified", "failed", "unavailable"})
SEMANTIC_SUPPORT_VALUES = frozenset({"supported", "contradicted", "conflicted", "unknown"})
#: How the semantic judgement was reached. `machine_assessed` is an AI's opinion and must
#: never be rendered with the same weight as the other three (plan §6.7, §21.4).
ASSESSMENT_BASIS_ORDER: tuple[str, ...] = ("machine_assessed", "experimental", "expert_attested", "formal")
ASSESSMENT_BASIS_VALUES = frozenset(ASSESSMENT_BASIS_ORDER)
CONFORMANCE_STATUS_VALUES = frozenset({"oracle_passed", "oracle_failed", "observed", "partial", "unknown"})
VERDICT_VALUES = frozenset({"aligned", "diverged", "missing", "unverified", "unknown"})

#: Every human-facing sentence carries one of these (plan §6.8). `machine_inferred` is the
#: honest label for "an AI wrote this"; there is no status meaning "reads plausibly".
STATEMENT_STATUS_VALUES = frozenset(
    {
        "source_supported",
        "code_observed",
        "oracle_observed",
        "expert_attested",
        "machine_inferred",
        "unknown",
        "conflicted",
    }
)

EXPERTISE_LEVEL_VALUES = frozenset({"familiar", "partial", "unfamiliar"})
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})

#: What a human may do about a mismatch or a gap. Note what is *not* here: "accept the risk"
#: is not an available disposition for a critical unknown (plan §15.4).
DISPOSITION_VALUES = frozenset(
    {
        "acknowledge_corrected_model",
        "revise_requirement",
        "revise_design",
        "revise_implementation",
        "request_expert",
        "run_experiment",
        "reduce_scope",
    }
)

HUMAN_REVIEW_STATUS_ORDER: tuple[str, ...] = ("not_started", "in_progress", "frozen", "attested")
HUMAN_REVIEW_STATUS_VALUES = frozenset(HUMAN_REVIEW_STATUS_ORDER)
#: Whether a machine review exists at all. An explicit status rather than an inference from
#: empty lists — "we did not measure" has to be a state you can name (plan §2.4).
MACHINE_REVIEW_STATUS_VALUES = frozenset({"not_generated", "generated"})

#: Source-language analysis depth reported per language in the Coverage Manifest (plan §13.3).
ANALYSIS_DEPTH_VALUES = frozenset({"ast", "ast_plus_llm", "token_only", "unsupported"})
#: Why a changed file could not be analysed. Each of these is a reason an "extra behaviours: 0"
#: line would be a lie, so each forces the count to render as "undeterminable" (plan §13.4).
UNSUPPORTED_REASON_VALUES = frozenset(
    {"binary", "generated", "unsupported_language", "parser_failure", "too_large", "vendored"}
)

#: What an Actual Statement is about — the axes the extractor is asked to sweep.
ACTUAL_CATEGORY_VALUES = frozenset(
    {
        "state_propagation",
        "control_flow",
        "side_effect",
        "default_value",
        "failure_handling",
        "concurrency",
        "persistence",
        "security_boundary",
        "observability",
        "public_interface",
        "dependency",
    }
)

#: Behaviour present in the code that no claim accounts for (plan §14.6).
EXTRA_BEHAVIOR_CATEGORY_VALUES = frozenset(
    {
        "new_default",
        "retry_timeout_fallback",
        "external_side_effect",
        "public_interface",
        "persistence",
        "exception_suppression",
        "security_boundary",
        "observability_reduction",
        "dependency_change",
        "concurrency",
    }
)

#: Why something could not be settled. Each kind names a *different* missing thing, so that
#: "we have no source" never renders the same as "the source does not support the claim".
GAP_KIND_VALUES = frozenset(
    {
        "evidence_gap",
        "authority_gap",
        "entailment_gap",
        "actual_coverage_gap",
        "maintainability_gap",
        "knowledge_gap",
        "independence_gap",
        "oracle_gap",
    }
)

SECURITY_CATEGORY_VALUES = frozenset(
    {
        "credential_exposure",
        "injection",
        "authz_bypass",
        "authn_weakness",
        "crypto_misuse",
        "ssrf",
        "path_traversal",
        "deserialization",
        "supply_chain",
        "sandbox_escape",
        "information_disclosure",
        "denial_of_service",
        "other",
    }
)

SCENARIO_KIND_ORDER: tuple[str, ...] = ("happy_path", "failure_path", "rollback_path")
SCENARIO_KIND_VALUES = frozenset(SCENARIO_KIND_ORDER)

#: The fixed order the human review is presented in (plan §14.1). Challenge comes first and
#: expected/actual comes fourth: seeing the answer before thinking about the scenario is the
#: priming this whole sequence exists to prevent.
REVIEW_STAGE_ORDER: tuple[str, ...] = (
    "challenge",
    "overview",
    "risk_brief",
    "expected_actual",
    "scenarios",
    "evidence_matrix",
    "module_delta",
    "decision",
    "security",
    "raw_diff",
    "attestation",
)
REVIEW_STAGE_VALUES = frozenset(REVIEW_STAGE_ORDER)
#: Stages the API refuses to serve until the unprimed challenge stage is complete (plan §21.2).
PRIMING_STAGES = frozenset({"expected_actual", "scenarios", "evidence_matrix", "module_delta", "decision"})

RUN_STATUS_VALUES = frozenset({"idle", "running", "waiting_for_review", "blocked", "complete"})
REVIEW_STATUS_VALUES = frozenset(
    {"none", "generating", "awaiting_human", "human_in_progress", "frozen", "attested", "stale"}
)

BUDGET_NAMES: tuple[str, ...] = (
    "max_critical_decisions",
    "max_critical_modules",
    "max_human_statements",
    "max_scenarios",
    "max_unresolved_low_medium_unknowns",
    "max_diff_bytes_per_partition",
)
BUDGET_NAME_VALUES = frozenset(BUDGET_NAMES)

# --- attestation vocabulary (plan §7) -----------------------------------------

ATTESTATION_TYPE_VALUES = frozenset(
    {
        "requirements_approval",
        "design_approval",
        "tasks_approval",
        "human_review_approval",
        "release_approval",
        "expert_confirmation",
        "human_decision",
    }
)
#: attestation type → the gate it can open (types that open no gate are absent).
ATTESTATION_GATE: Mapping[str, str] = {
    "requirements_approval": "requirements",
    "design_approval": "design",
    "tasks_approval": "tasks",
    "human_review_approval": "build",
    "release_approval": "release",
}
ROLE_VALUES = frozenset({"gate_reviewer", "release_approver", "expert", "security_approver"})
#: role required per attestation type — checked against the *external* Trust Manifest, never
#: against anything the PR head can change (plan §7.5).
REQUIRED_ROLE: Mapping[str, frozenset[str]] = {
    "requirements_approval": frozenset({"gate_reviewer"}),
    "design_approval": frozenset({"gate_reviewer"}),
    "tasks_approval": frozenset({"gate_reviewer"}),
    "human_review_approval": frozenset({"gate_reviewer"}),
    "release_approval": frozenset({"release_approver"}),
    "expert_confirmation": frozenset({"expert", "security_approver"}),
    "human_decision": frozenset({"gate_reviewer", "release_approver"}),
}

# --- event vocabulary (plan §25) ----------------------------------------------

EVENT_ORDER: tuple[str, ...] = (
    "cycle_initialized",
    "source_search_started",
    "source_search_completed",
    "source_unavailable",
    "source_snapshot_created",
    "evidence_obligation_satisfied",
    "evidence_obligation_failed",
    "human_decision_requested",
    "human_decision_attested",
    "knowledge_gap",
    "gate_approval_requested",
    "gate_approved",
    "gate_revised",
    "plan_frozen",
    "plan_invalidated",
    "task_started",
    "task_failed",
    "task_completed",
    "decision_declared",
    "oracle_negative_control_started",
    "oracle_negative_control_passed",
    "oracle_negative_control_failed",
    "oracle_started",
    "oracle_passed",
    "oracle_failed",
    "coverage_generated",
    "actual_extraction_started",
    "actual_extraction_generated",
    "actual_extraction_failed",
    "comparison_generated",
    "security_review_generated",
    "cold_maintainer_generated",
    "review_generated",
    "review_failed",
    "challenge_answered",
    "counterfactual_answered",
    "expert_requested",
    "expert_attested",
    "disposition_recorded",
    "human_review_frozen",
    "human_review_attested",
    "release_verified",
    "release_approved",
    "cycle_closed",
)
EVENT_VALUES = frozenset(EVENT_ORDER)

#: Capabilities the control plane can grant. A leaf agent never receives any of
#: :data:`CENTRAL_ONLY_CAPABILITIES` (plan §11.3).
CAPABILITY_VALUES = frozenset(
    {
        "decision.declare",
        "knowledge_gap.create",
        "task.status",
        "event.append",
        "gate.approve",
        "expert.confirm",
        "human.review.complete",
        "review.machine.replace",
        "state.replace",
        "attestation.import",
    }
)
CENTRAL_ONLY_CAPABILITIES = frozenset(
    {
        "gate.approve",
        "expert.confirm",
        "human.review.complete",
        "review.machine.replace",
        "state.replace",
        "attestation.import",
    }
)

# --- identifiers --------------------------------------------------------------
#
# Every cross-reference is by ID, so an ID has to be a *shape* a validator can check rather
# than free text — otherwise "SRC-004" and "SRC-4" are two sources to a human and one typo
# to a reviewer.

ID_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "claim": re.compile(r"^C-\d{3,}$"),
    "technical_fact": re.compile(r"^TF-\d{3,}$"),
    "solution": re.compile(r"^D-\d{3,}$"),
    "source": re.compile(r"^SRC-\d{3,}$"),
    "search": re.compile(r"^SEARCH-\d{3,}$"),
    "obligation": re.compile(r"^EO-[A-Z0-9-]+$"),
    "oracle": re.compile(r"^O-\d{3,}$"),
    "task": re.compile(r"^T-\d{3,}$"),
    "non_goal": re.compile(r"^NG-\d{3,}$"),
    "statement": re.compile(r"^STMT-\d{3,}$"),
    "actual_statement": re.compile(r"^AST-\d{3,}$"),
    "challenge": re.compile(r"^CH-\d{3,}$"),
    "decision_card": re.compile(r"^DC-\d{3,}$"),
    "attestation": re.compile(r"^ATT-[A-Z0-9-]+$"),
    "finding": re.compile(r"^SEC-\d{3,}$"),
    "extra_behavior": re.compile(r"^EXTRA-\d{3,}$"),
}

#: Repo-relative POSIX paths only: no absolute path, no `..`, no backslash, no leading slash.
REPO_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._][A-Za-z0-9._/@+-]*$")


def is_repo_path(value: object) -> bool:
    """True when `value` is a safe repo-relative POSIX path (no escape, no absolute form)."""
    return isinstance(value, str) and bool(REPO_PATH_RE.match(value)) and "\\" not in value


def risk_at_least(risk: str, floor: str) -> bool:
    """True when `risk` sits at or above `floor` on the risk ladder."""
    return RISK_ORDER.index(risk) >= RISK_ORDER.index(floor)


def max_risk(risks: Iterable[str]) -> str:
    """The highest risk in `risks` (`low` when empty) — the shape effective risk is built from."""
    return max(risks, key=RISK_ORDER.index, default="low")


# --- errors -------------------------------------------------------------------


class DocumentError(ValueError):
    """A document failed validation. `errors` lists every problem found, not just the first.

    Reporting all of them matters for the human loop: fixing one error, re-running, and being
    handed the next one is exactly the review friction plan §2.6 budgets against.
    """

    def __init__(self, what: str, errors: Sequence[str]) -> None:
        self.what = what
        self.errors = list(errors)
        joined = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"{what}: {len(self.errors)} validation error(s)\n{joined}")


# --- JSON Schema layer --------------------------------------------------------

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}


def schema(name: str) -> Mapping[str, Any]:
    """The packaged JSON Schema `name` (e.g. "plan"), parsed once per process."""
    if name not in _SCHEMA_CACHE:
        loaded = strict_yaml.load_json_mapping(
            data.read_text(f"schema/{name}.schema.json"),
            limits=strict_yaml.DEFAULT_LIMITS,
            what=f"{name}.schema.json",
        )
        _SCHEMA_CACHE[name] = loaded
    return _SCHEMA_CACHE[name]


def schema_errors(document: Any, name: str) -> list[str]:
    """Every JSON Schema violation in `document`, as human-readable "path: message" strings.

    jsonschema is a hard dependency in 0.9.0 (it was optional in 0.8.x, degrading to a WARN):
    a structural check that can silently turn itself off is not a boundary, and plan §15.4
    makes schema conformance an absolute block.
    """
    import jsonschema  # deferred: keeps `import models` cheap for the gate-guard hook path

    validator_cls = jsonschema.validators.validator_for(schema(name))
    validator = validator_cls(schema(name))
    errors = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{location}: {error.message}")
    return errors


# --- element views ------------------------------------------------------------
#
# Thin, read-only projections of one entry. Each keeps `raw` so a consumer can reach a field
# no accessor covers yet, without anyone being tempted to re-serialize the projection.


def _str(mapping: Mapping[str, Any], key: str, default: str = "") -> str:
    value = mapping.get(key, default)
    return value if isinstance(value, str) else default


def _ids(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key) or []
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _maps(mapping: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = mapping.get(key) or []
    return tuple(v for v in value if isinstance(v, dict)) if isinstance(value, list) else ()


@dataclass(frozen=True)
class Element:
    """Base view: an `id` plus the raw entry it projects."""

    raw: Mapping[str, Any]

    @property
    def id(self) -> str:
        return _str(self.raw, "id")


@dataclass(frozen=True)
class Source(Element):
    """One piece of evidence, with the authority the Policy Engine derived for it."""

    @property
    def provider(self) -> str:
        return _str(self.raw, "provider")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind")

    @property
    def authority_class(self) -> str:
        authority = self.raw.get("authority")
        return _str(authority, "class") if isinstance(authority, dict) else ""

    @property
    def is_normative(self) -> bool:
        """True when this source may ground a high/critical claim on its own.

        Existing code and README text are `descriptive`: they say what the system *does*, which
        is never by itself a statement of what it *should* do (plan §8.5, E2E-06).
        """
        return self.authority_class in NORMATIVE_AUTHORITY

    @property
    def snapshot_digest(self) -> str:
        snapshot = self.raw.get("snapshot")
        return _str(snapshot, "digest") if isinstance(snapshot, dict) else ""

    @property
    def verification_status(self) -> str:
        verification = self.raw.get("verification")
        return _str(verification, "status") if isinstance(verification, dict) else ""


@dataclass(frozen=True)
class EvidenceObligation(Element):
    """What has to be true before a claim may be treated as settled (plan §6.4)."""

    @property
    def subject_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "subject_ids")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def execution_status(self) -> str:
        return _str(self.raw, "execution_status", "pending")

    @property
    def coverage_status(self) -> str:
        return _str(self.raw, "coverage_status", "unsatisfied")

    @property
    def satisfied_by(self) -> tuple[str, ...]:
        return _ids(self.raw, "satisfied_by")

    @property
    def alternatives(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.raw, "alternatives")

    @property
    def satisfied(self) -> bool:
        return self.coverage_status == "satisfied"


@dataclass(frozen=True)
class Search(Element):
    """One evidence search: which providers were asked, and what each one answered."""

    @property
    def obligation_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "obligation_ids")

    @property
    def provider_attempts(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.raw, "provider_attempts")

    @property
    def execution_status(self) -> str:
        return _str(self.raw, "execution_status", "pending")

    @property
    def coverage_status(self) -> str:
        return _str(self.raw, "coverage_status", "insufficient")

    @property
    def unavailable_providers(self) -> tuple[str, ...]:
        """Providers that could not be searched — surfaced even when an alternate path
        satisfied the obligation, so a provider outage is never hidden (plan §15.3)."""
        return tuple(_str(a, "provider") for a in self.provider_attempts if _str(a, "result") == "unavailable")


@dataclass(frozen=True)
class Claim(Element):
    """One statement about intended behaviour, with its evidence and epistemic status."""

    @property
    def statement(self) -> str:
        return _str(self.raw, "statement")

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "requirement_ids")

    @property
    def decision_class(self) -> str:
        return _str(self.raw, "decision_class")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.raw, "domains")

    @property
    def obligation_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "evidence_obligation_ids")

    @property
    def oracle_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "oracle_ids")

    @property
    def epistemic_status(self) -> str:
        return _str(self.raw, "epistemic_status", "unknown")

    @property
    def evidence(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.raw, "evidence")

    @property
    def supporting_source_ids(self) -> tuple[str, ...]:
        return tuple(_str(e, "source_id") for e in self.evidence if _str(e, "relation") == "supports")

    @property
    def contradicting_source_ids(self) -> tuple[str, ...]:
        return tuple(_str(e, "source_id") for e in self.evidence if _str(e, "relation") == "contradicts")


@dataclass(frozen=True)
class TechnicalFact(Element):
    """A statement about how something actually works. A human decision cannot make one true."""

    @property
    def statement(self) -> str:
        return _str(self.raw, "statement")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.raw, "domains")

    @property
    def obligation_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "evidence_obligation_ids")

    @property
    def epistemic_status(self) -> str:
        return _str(self.raw, "epistemic_status", "unknown")

    @property
    def evidence(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.raw, "evidence")

    @property
    def supporting_source_ids(self) -> tuple[str, ...]:
        return tuple(_str(e, "source_id") for e in self.evidence if _str(e, "relation") == "supports")


@dataclass(frozen=True)
class Solution(Element):
    """A design decision, its alternatives, and how to undo it."""

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "claim_ids")

    @property
    def technical_fact_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "technical_fact_ids")

    @property
    def rationale_source_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "rationale_source_ids")

    @property
    def alternatives(self) -> tuple[str, ...]:
        return _ids(self.raw, "alternatives")

    @property
    def rollback_obligation_id(self) -> str:
        rollback = self.raw.get("rollback_claim")
        return _str(rollback, "evidence_obligation_id") if isinstance(rollback, dict) else ""


@dataclass(frozen=True)
class Oracle(Element):
    """An acceptance oracle: a judgement boundary frozen at gate ③, separate from unit tests."""

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "claim_ids")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind")

    @property
    def bundle_root(self) -> str:
        bundle = self.raw.get("bundle")
        return _str(bundle, "root") if isinstance(bundle, dict) else ""

    @property
    def bundle_digest(self) -> str:
        bundle = self.raw.get("bundle")
        return _str(bundle, "digest") if isinstance(bundle, dict) else ""

    @property
    def negative_controls(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.raw, "negative_controls")

    @property
    def subject_paths(self) -> tuple[str, ...]:
        return _ids(self.raw, "subject_paths")

    @property
    def requires_negative_control(self) -> bool:
        """High/critical oracles must demonstrably fail on a known violation (plan §9.4)."""
        return self.risk in ELEVATED_RISKS


@dataclass(frozen=True)
class Task(Element):
    """One unit of implementation work and the claims it is answerable for."""

    @property
    def title(self) -> str:
        return _str(self.raw, "title")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "parallel")

    @property
    def blocked_by(self) -> tuple[str, ...]:
        return _ids(self.raw, "blocked_by")

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "claim_ids")

    @property
    def oracle_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "oracle_ids")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.raw, "domains")

    @property
    def scope_include(self) -> tuple[str, ...]:
        scope = self.raw.get("scope")
        return _ids(scope, "include") if isinstance(scope, dict) else ()

    @property
    def scope_exclude(self) -> tuple[str, ...]:
        scope = self.raw.get("scope")
        return _ids(scope, "exclude") if isinstance(scope, dict) else ()


_PLAN_SECTIONS: Mapping[str, type[Element]] = {
    "evidence_obligations": EvidenceObligation,
    "searches": Search,
    "sources": Source,
    "claims": Claim,
    "technical_facts": TechnicalFact,
    "solutions": Solution,
    "oracles": Oracle,
    "tasks": Task,
    "non_goals": Element,
}


# --- documents ----------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """``plan.yaml`` — the Expected Model, frozen at gate ③ (plan §6.1).

    Everything a reviewer compares reality against lives here, and after the freeze the only
    way to change it is `agentloop revise --to tasks`. The views below are built once in
    `__post_init__`; `raw` stays the digest subject.
    """

    raw: Mapping[str, Any]
    _index: dict[str, dict[str, Element]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[str, dict[str, Element]] = {}
        for section, view in _PLAN_SECTIONS.items():
            entries = _maps(self.raw, section)
            index[section] = {_str(e, "id"): view(e) for e in entries}
        object.__setattr__(self, "_index", index)

    # -- construction ---------------------------------------------------------

    @classmethod
    def parse(cls, text: str, *, what: str = "plan.yaml", cross_reference: bool = True) -> Plan:
        """Parse and fully validate `text`. Raises :class:`DocumentError` listing every problem."""
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "plan")
        plan = cls(document)
        if not errors and cross_reference:
            errors = cross_reference_errors(plan)
        if errors:
            raise DocumentError(what, errors)
        return plan

    # -- identity -------------------------------------------------------------

    def digest(self) -> str:
        """The canonical plan digest a gate receipt binds (plan §17.1)."""
        return digests.of(self.raw, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    @property
    def cycle(self) -> Mapping[str, Any]:
        value = self.raw.get("cycle")
        return value if isinstance(value, dict) else {}

    @property
    def cycle_id(self) -> str:
        return _str(self.cycle, "id")

    @property
    def base_commit(self) -> str:
        return _str(self.cycle, "base_commit")

    @property
    def branch(self) -> str:
        return _str(self.cycle, "branch")

    # -- sections -------------------------------------------------------------

    def _section(self, name: str) -> tuple[Any, ...]:
        return tuple(self._index[name].values())

    @property
    def obligations(self) -> tuple[EvidenceObligation, ...]:
        return self._section("evidence_obligations")

    @property
    def searches(self) -> tuple[Search, ...]:
        return self._section("searches")

    @property
    def sources(self) -> tuple[Source, ...]:
        return self._section("sources")

    @property
    def claims(self) -> tuple[Claim, ...]:
        return self._section("claims")

    @property
    def technical_facts(self) -> tuple[TechnicalFact, ...]:
        return self._section("technical_facts")

    @property
    def solutions(self) -> tuple[Solution, ...]:
        return self._section("solutions")

    @property
    def oracles(self) -> tuple[Oracle, ...]:
        return self._section("oracles")

    @property
    def tasks(self) -> tuple[Task, ...]:
        return self._section("tasks")

    @property
    def non_goals(self) -> tuple[Element, ...]:
        return self._section("non_goals")

    # -- lookup ---------------------------------------------------------------

    def get(self, section: str, element_id: str) -> Element | None:
        """The element `element_id` of `section`, or None. Never raises on an unknown ID —
        callers that must not proceed on a dangling reference use `cross_reference_errors`."""
        return self._index.get(section, {}).get(element_id)

    def claim(self, claim_id: str) -> Claim | None:
        found = self.get("claims", claim_id)
        return found if isinstance(found, Claim) else None

    def source(self, source_id: str) -> Source | None:
        found = self.get("sources", source_id)
        return found if isinstance(found, Source) else None

    def oracle(self, oracle_id: str) -> Oracle | None:
        found = self.get("oracles", oracle_id)
        return found if isinstance(found, Oracle) else None

    def task(self, task_id: str) -> Task | None:
        found = self.get("tasks", task_id)
        return found if isinstance(found, Task) else None

    def obligation(self, obligation_id: str) -> EvidenceObligation | None:
        found = self.get("evidence_obligations", obligation_id)
        return found if isinstance(found, EvidenceObligation) else None

    def ids(self, section: str) -> frozenset[str]:
        return frozenset(self._index.get(section, {}))

    # -- derived questions the gates ask --------------------------------------

    def subjects_of(self, obligation_id: str) -> tuple[Element, ...]:
        """The claims and technical facts an obligation covers."""
        obligation = self.obligation(obligation_id)
        if obligation is None:
            return ()
        found = (self.get("claims", sid) or self.get("technical_facts", sid) for sid in obligation.subject_ids)
        return tuple(e for e in found if e is not None)

    def unsatisfied_obligations(self, *, floor: str = "low") -> tuple[EvidenceObligation, ...]:
        """Obligations at or above `floor` that are not satisfied — an absolute block (§15.4)."""
        return tuple(o for o in self.obligations if not o.satisfied and risk_at_least(o.risk, floor))

    def ungrounded(self, *, floor: str = "high") -> tuple[Element, ...]:
        """Claims and technical facts at or above `floor` whose status is unknown/conflicted."""
        subjects: list[Element] = [*self.claims, *self.technical_facts]
        return tuple(
            s
            for s in subjects
            if getattr(s, "epistemic_status", "unknown") != "grounded"
            and risk_at_least(getattr(s, "risk", "low"), floor)
        )


@dataclass(frozen=True)
class State:
    """``state.yaml`` — mutable state only (plan §6.5).

    Deliberately holds no task title, dependency, or claim mapping: those live in the frozen
    plan. Duplicating them here is how 0.8.x's state.md and tasks.yaml drifted apart.
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "state.yaml") -> State:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "state")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    @property
    def project(self) -> str:
        return _str(self.raw, "project")

    @property
    def cycle_id(self) -> str:
        return _str(self.raw, "cycle_id")

    @property
    def current_phase(self) -> str:
        return _str(self.raw, "current_phase", "brief")

    @property
    def gates(self) -> Mapping[str, Mapping[str, Any]]:
        value = self.raw.get("gates")
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(v, dict)}

    def gate_status(self, gate: str) -> str:
        """`pending` unless the gate is explicitly approved — an unreadable gate reads as pending."""
        entry = self.gates.get(gate)
        return _str(entry, "status", "pending") if entry else "pending"

    def gate_receipt(self, gate: str) -> Mapping[str, Any] | None:
        entry = self.gates.get(gate)
        receipt = entry.get("receipt") if entry else None
        return receipt if isinstance(receipt, dict) else None

    @property
    def approved_gates(self) -> tuple[str, ...]:
        return tuple(g for g in GATE_ORDER if self.gate_status(g) == "approved")

    @property
    def plan_status(self) -> str:
        plan = self.raw.get("plan")
        return _str(plan, "status", "draft") if isinstance(plan, dict) else "draft"

    @property
    def plan_digest(self) -> str:
        plan = self.raw.get("plan")
        return _str(plan, "digest") if isinstance(plan, dict) else ""

    @property
    def execution(self) -> Mapping[str, Any]:
        value = self.raw.get("execution")
        return value if isinstance(value, dict) else {}

    @property
    def review(self) -> Mapping[str, Any]:
        value = self.raw.get("review")
        return value if isinstance(value, dict) else {}

    @property
    def task_status(self) -> Mapping[str, str]:
        value = self.raw.get("tasks")
        if not isinstance(value, dict):
            return {}
        return {k: _str(v, "status", "todo") for k, v in value.items() if isinstance(v, dict)}

    def gate_chain_violations(self) -> list[tuple[str, str]]:
        """Every (approved gate, first pending gate upstream of it) pair.

        A non-empty result means an approval survived a roll back: downstream work is standing
        on a decision that has been withdrawn (AGENTS.md "Roll back").
        """
        violations: list[tuple[str, str]] = []
        first_pending: str | None = None
        for gate in GATE_ORDER:
            if self.gate_status(gate) != "approved":
                first_pending = first_pending or gate
            elif first_pending is not None:
                violations.append((gate, first_pending))
        return violations

    def pending_upstream(self, gate: str) -> str | None:
        """The first not-approved gate upstream of `gate`, or None when the chain is clear."""
        for upstream in GATE_ORDER[: GATE_ORDER.index(gate)]:
            if self.gate_status(upstream) != "approved":
                return upstream
        return None


@dataclass(frozen=True)
class Review:
    """``review.yaml`` — the machine review and the human review, digested separately (plan §6.6).

    The split is the point: regenerating the machine review resets the human section, while a
    human answering a challenge must *not* make the machine review stale (plan §17.5).
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "review.yaml") -> Review:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "review")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    @property
    def machine(self) -> Mapping[str, Any]:
        value = self.raw.get("machine")
        return value if isinstance(value, dict) else {}

    @property
    def human(self) -> Mapping[str, Any]:
        value = self.raw.get("human")
        return value if isinstance(value, dict) else {}

    def machine_digest(self) -> str:
        return digests.of(self.machine, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    def human_digest(self) -> str:
        return digests.of(self.human, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    @property
    def machine_status(self) -> str:
        return _str(self.machine, "status", "not_generated")

    @property
    def is_generated(self) -> bool:
        return self.machine_status == "generated"

    @property
    def human_status(self) -> str:
        return _str(self.human, "status", "not_started")

    @property
    def actual_statements(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "actual_extraction")

    @property
    def claim_results(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "claims")

    @property
    def extra_behaviors(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "extra_behaviors")

    @property
    def security_findings(self) -> tuple[Mapping[str, Any], ...]:
        security = self.machine.get("security")
        return _maps(security, "findings") if isinstance(security, dict) else ()

    @property
    def blocking_security_findings(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(f for f in self.security_findings if f.get("blocking") is True)

    @property
    def coverage(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "coverage")

    @property
    def coverage_sufficient(self) -> bool:
        """True only when every coverage entry says so.

        An absent coverage manifest is *not* sufficient, and neither is an ungenerated review:
        "we did not measure" and "we measured nothing missing" must never render the same
        (plan §2.4).
        """
        entries = self.coverage
        return self.is_generated and bool(entries) and all(_str(c, "coverage_status") == "sufficient" for c in entries)


@dataclass(frozen=True)
class ExecutorProfile:
    """One sandbox definition (plan §10.2)."""

    name: str
    raw: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "host")

    @property
    def is_sandboxed(self) -> bool:
        return self.kind == "oci"

    @property
    def image(self) -> str:
        return _str(self.raw, "image")

    @property
    def containerfile(self) -> str:
        return _str(self.raw, "containerfile")

    @property
    def image_digest(self) -> str:
        """The `sha256:…` half of a digest-pinned image reference ("" when unpinned)."""
        _, _, digest = self.image.partition("@")
        return digest

    @property
    def network_profile(self) -> str:
        return _str(self.raw, "network_profile")

    @property
    def env_allowlist(self) -> tuple[str, ...]:
        return _ids(self.raw, "env_allowlist")


@dataclass(frozen=True)
class GateStep:
    """One quality-gate step — the DoD is exactly this list, in order (plan §19)."""

    raw: Mapping[str, Any]

    @property
    def name(self) -> str:
        return _str(self.raw, "name")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "command")

    @property
    def command(self) -> tuple[str, ...]:
        return _ids(self.raw, "command")

    @property
    def agent_role(self) -> str:
        return _str(self.raw, "agent_role")

    @property
    def executor_profile(self) -> str:
        return _str(self.raw, "executor_profile")

    @property
    def retries(self) -> int:
        value = self.raw.get("retries")
        return value if isinstance(value, int) else 0

    @property
    def required(self) -> bool:
        return self.raw.get("required") is not False


@dataclass(frozen=True)
class Config:
    """``config.yaml`` — execution knobs, frozen wholesale at gate ③.

    Read by the guard hook, the build loop, the executors, and doctor. Note what it cannot
    answer: *who* may approve anything. That question is only ever answered by the external
    Trust Manifest, so a pull request cannot widen its own permissions (plan §2.2).
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "config.yaml") -> Config:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "config")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    def digest(self) -> str:
        return digests.of(self.raw, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    @property
    def project_name(self) -> str:
        project = self.raw.get("project")
        return _str(project, "name") if isinstance(project, dict) else ""

    @property
    def work_branch(self) -> str:
        project = self.raw.get("project")
        return _str(project, "work_branch") if isinstance(project, dict) else ""

    @property
    def execution(self) -> Mapping[str, Any]:
        value = self.raw.get("execution")
        return value if isinstance(value, dict) else {}

    def _int(self, section: Mapping[str, Any], key: str, default: int) -> int:
        value = section.get(key)
        return value if isinstance(value, int) else default

    @property
    def max_parallel(self) -> int:
        return self._int(self.execution, "max_parallel", 3)

    @property
    def worktree_dir(self) -> str:
        return _str(self.execution, "worktree_dir", ".worktrees")

    @property
    def command_timeout_sec(self) -> int:
        return self._int(self.execution, "command_timeout_sec", 1800)

    @property
    def agent_timeout_sec(self) -> int:
        return self._int(self.execution, "agent_timeout_sec", 3600)

    @property
    def profiles(self) -> dict[str, ExecutorProfile]:
        raw = self.raw.get("executor_profiles")
        if not isinstance(raw, dict):
            return {}
        return {name: ExecutorProfile(name, body) for name, body in raw.items() if isinstance(body, dict)}

    def profile_for(self, role: str) -> ExecutorProfile | None:
        """The sandbox a role runs in ("implementer" / "reviewer" / "oracle" / "quality_gate")."""
        executors = self.raw.get("executors")
        if not isinstance(executors, dict):
            return None
        return self.profiles.get(_str(executors, f"{role}_profile"))

    @property
    def quality_gate(self) -> tuple[GateStep, ...]:
        return tuple(GateStep(step) for step in _maps(self.raw, "quality_gate"))

    @property
    def agents(self) -> Mapping[str, Mapping[str, Any]]:
        raw = self.raw.get("agents")
        if not isinstance(raw, dict):
            return {}
        return {name: body for name, body in raw.items() if isinstance(body, dict)}

    def independence_group(self, role: str) -> str:
        return _str(self.agents.get(role, {}), "independence_group")

    def adapter(self, role: str) -> str:
        return _str(self.agents.get(role, {}), "adapter")

    @property
    def template_mode(self) -> bool:
        guard = self.raw.get("guard")
        return bool(guard.get("template_mode")) if isinstance(guard, dict) else False

    @property
    def guard_paths(self) -> dict[str, str]:
        """Guarded path → the gate it requires. A trailing "/" makes it a prefix rule."""
        guard = self.raw.get("guard")
        entries = _maps(guard, "paths") if isinstance(guard, dict) else ()
        return {_str(e, "path"): _str(e, "requires_gate") for e in entries if _str(e, "path")}

    @property
    def budgets(self) -> dict[str, int]:
        policy = self.raw.get("review_policy")
        raw = policy.get("budgets") if isinstance(policy, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, int) and k in BUDGET_NAME_VALUES}

    @property
    def cold_maintainer_for(self) -> frozenset[str]:
        policy = self.raw.get("review_policy")
        value = _ids(policy, "cold_maintainer_for") if isinstance(policy, dict) else ()
        return frozenset(value) or ELEVATED_RISKS

    @property
    def github(self) -> Mapping[str, Any]:
        value = self.raw.get("github")
        return value if isinstance(value, dict) else {}

    def unsandboxed_code_profiles(self) -> list[str]:
        """Profiles that run repository-derived code on the host — a policy failure (plan §10.1).

        Reported by `doctor` rather than raised: a freshly initialized repository legitimately
        starts here, and the honest answer is "not compliant yet, here is the command", not a
        crash on first run.
        """
        executors = self.raw.get("executors")
        if not isinstance(executors, dict):
            return []
        offenders = []
        for role in ("implementer", "reviewer", "oracle", "quality_gate"):
            profile = self.profile_for(role)
            if profile is not None and not profile.is_sandboxed:
                offenders.append(profile.name)
        return sorted(set(offenders))


# --- Event and Attestation ----------------------------------------------------
#
# Unlike the documents above these are *constructed* by AgentLoop rather than read from
# author-written files, so they are real dataclasses with an explicit canonical mapping.


@dataclass(frozen=True)
class Event:
    """One append-only audit record, chained to its predecessor (plan §18.5).

    `event_digest` covers the canonical form of every other field, so a rewritten event breaks
    its own digest; `prev_event_digest` chains it, so a deleted or reordered event breaks the
    next one's link. Both are computed by :mod:`agentloop.event_chain`, not here.
    """

    seq: int
    id: str
    tx_id: str
    ts: str
    event: str
    cycle_id: str
    actor: str = ""
    subject_ids: tuple[str, ...] = ()
    prev_event_digest: str = ""
    event_digest: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """The canonical mapping `event_digest` is computed over — every field but the digest itself."""
        return {
            "seq": self.seq,
            "id": self.id,
            "tx_id": self.tx_id,
            "ts": self.ts,
            "event": self.event,
            "cycle_id": self.cycle_id,
            "actor": self.actor,
            "subject_ids": list(self.subject_ids),
            "prev_event_digest": self.prev_event_digest,
            "detail": dict(self.detail),
        }

    def to_mapping(self) -> dict[str, Any]:
        """The full record as written to `events.ndjson` (payload plus `event_digest`)."""
        return {**self.payload(), "event_digest": self.event_digest}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Event:
        """Build an Event from a parsed NDJSON line. Assumes the line already passed the schema."""
        subject_ids = raw.get("subject_ids") or []
        detail = raw.get("detail") or {}
        return cls(
            seq=int(raw["seq"]),
            id=str(raw["id"]),
            tx_id=str(raw.get("tx_id", "")),
            ts=str(raw.get("ts", "")),
            event=str(raw["event"]),
            cycle_id=str(raw.get("cycle_id", "")),
            actor=str(raw.get("actor", "")),
            subject_ids=tuple(str(s) for s in subject_ids) if isinstance(subject_ids, list) else (),
            prev_event_digest=str(raw.get("prev_event_digest", "")),
            event_digest=str(raw.get("event_digest", "")),
            detail=detail if isinstance(detail, dict) else {},
        )


@dataclass(frozen=True)
class Attestation:
    """A signed human or expert action (plan §7.3).

    `subject` binds the action to exact digests, which is what stops a signature from being
    replayed onto a later review, a different cycle, or another repository. The signature
    itself is verified by :mod:`agentloop.attestations` against the *external* Trust Manifest —
    nothing in this repository can add an authorized signer.
    """

    id: str
    type: str
    subject: Mapping[str, Any]
    actor: Mapping[str, Any]
    issued_at: str
    signature: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str, *, what: str = "attestation") -> Attestation:
        document = strict_yaml.load_json_mapping(text, what=what)
        errors = schema_errors(document, "attestation")
        if errors:
            raise DocumentError(what, errors)
        return cls.from_mapping(document)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Attestation:
        return cls(
            id=str(raw["id"]),
            type=str(raw["type"]),
            subject=raw.get("subject") or {},
            actor=raw.get("actor") or {},
            issued_at=str(raw.get("issued_at", "")),
            signature=raw.get("signature") or {},
        )

    def payload(self) -> dict[str, Any]:
        """The canonical mapping that gets signed — everything except the signature block.

        `issued_at` is inside it deliberately: without it a signature could be lifted onto a
        later action by the same principal (plan §7.5 "stale digest" rejection).
        """
        return {
            "id": self.id,
            "type": self.type,
            "subject": dict(self.subject),
            "actor": dict(self.actor),
            "issued_at": self.issued_at,
        }

    def payload_digest(self) -> str:
        return digests.of(self.payload())

    def to_mapping(self) -> dict[str, Any]:
        return {**self.payload(), "signature": dict(self.signature)}

    @property
    def principal(self) -> str:
        return _str(self.actor, "principal")

    @property
    def role(self) -> str:
        return _str(self.actor, "role")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.actor, "domains")

    @property
    def gate(self) -> str:
        """The gate this attestation can open, or "" when its type opens none."""
        return ATTESTATION_GATE.get(self.type, "")

    def subject_digest(self, name: str) -> str:
        return _str(self.subject, name)


# --- cross-reference validation (plan §23) ------------------------------------


def _dangling(
    plan: Plan, section: str, id_getter: str, refs: Iterable[str], target: str, target_label: str
) -> Iterator[str]:
    known = plan.ids(target)
    for ref in refs:
        if ref not in known:
            yield f"{section}/{id_getter}: unknown {target_label} id {ref!r}"


def cross_reference_errors(plan: Plan) -> list[str]:
    """Every dangling or malformed ID reference in `plan` (plan §23's table).

    JSON Schema can check that ``claim_ids`` is a list of ``C-\\d+`` strings; only this can
    check that ``C-002`` names a claim that exists. A dangling reference is where an AI's
    invented ID would otherwise survive review looking exactly like a real one.
    """
    errors: list[str] = []

    # ID shape and uniqueness, per section.
    for section, kind in (
        ("claims", "claim"),
        ("technical_facts", "technical_fact"),
        ("solutions", "solution"),
        ("sources", "source"),
        ("searches", "search"),
        ("evidence_obligations", "obligation"),
        ("oracles", "oracle"),
        ("tasks", "task"),
        ("non_goals", "non_goal"),
    ):
        seen: set[str] = set()
        for entry in _maps(plan.raw, section):
            element_id = _str(entry, "id")
            if not ID_PATTERNS[kind].match(element_id):
                errors.append(f"{section}: {element_id!r} does not match the {kind} id pattern")
            if element_id in seen:
                errors.append(f"{section}: duplicate id {element_id!r}")
            seen.add(element_id)

    for claim in plan.claims:
        errors += _dangling(plan, "claims", claim.id, claim.obligation_ids, "evidence_obligations", "obligation")
        errors += _dangling(plan, "claims", claim.id, claim.oracle_ids, "oracles", "oracle")
        errors += _dangling(
            plan, "claims", claim.id, (_str(e, "source_id") for e in claim.evidence), "sources", "source"
        )

    for fact in plan.technical_facts:
        errors += _dangling(plan, "technical_facts", fact.id, fact.obligation_ids, "evidence_obligations", "obligation")
        errors += _dangling(
            plan, "technical_facts", fact.id, (_str(e, "source_id") for e in fact.evidence), "sources", "source"
        )

    for solution in plan.solutions:
        errors += _dangling(plan, "solutions", solution.id, solution.claim_ids, "claims", "claim")
        errors += _dangling(
            plan, "solutions", solution.id, solution.technical_fact_ids, "technical_facts", "technical fact"
        )
        errors += _dangling(plan, "solutions", solution.id, solution.rationale_source_ids, "sources", "source")
        if solution.rollback_obligation_id:
            errors += _dangling(
                plan,
                "solutions",
                solution.id,
                [solution.rollback_obligation_id],
                "evidence_obligations",
                "obligation",
            )

    for oracle in plan.oracles:
        errors += _dangling(plan, "oracles", oracle.id, oracle.claim_ids, "claims", "claim")
        if oracle.requires_negative_control and not oracle.negative_controls:
            errors.append(
                f"oracles/{oracle.id}: risk {oracle.risk} requires at least one negative control "
                "(an oracle that never fails proves nothing — plan §9.4)"
            )
        for path in oracle.subject_paths:
            if not is_repo_path(path):
                errors.append(f"oracles/{oracle.id}: subject path {path!r} is not a safe repo-relative path")

    for obligation in plan.obligations:
        for subject_id in obligation.subject_ids:
            if subject_id not in plan.ids("claims") and subject_id not in plan.ids("technical_facts"):
                errors.append(
                    f"evidence_obligations/{obligation.id}: subject {subject_id!r} is neither a claim "
                    "nor a technical fact"
                )
        for satisfier in obligation.satisfied_by:
            known = plan.ids("sources") | plan.ids("oracles")
            if satisfier not in known and not ID_PATTERNS["attestation"].match(satisfier):
                errors.append(
                    f"evidence_obligations/{obligation.id}: satisfied_by {satisfier!r} is not a source, "
                    "oracle, or attestation id"
                )

    for search in plan.searches:
        errors += _dangling(plan, "searches", search.id, search.obligation_ids, "evidence_obligations", "obligation")
        for attempt in search.provider_attempts:
            errors += _dangling(plan, "searches", search.id, _ids(attempt, "source_ids"), "sources", "source")

    task_ids = plan.ids("tasks")
    for task in plan.tasks:
        errors += _dangling(plan, "tasks", task.id, task.claim_ids, "claims", "claim")
        errors += _dangling(plan, "tasks", task.id, task.oracle_ids, "oracles", "oracle")
        for blocker in task.blocked_by:
            if blocker not in task_ids:
                errors.append(f"tasks/{task.id}: unknown blocked_by task id {blocker!r}")
            elif blocker == task.id:
                errors.append(f"tasks/{task.id}: blocked_by lists itself")
        for path in (*task.scope_include, *task.scope_exclude):
            if not is_repo_path(path):
                errors.append(f"tasks/{task.id}: scope path {path!r} is not a safe repo-relative path")

    errors += _cycle_errors(plan)
    return errors


def _cycle_errors(plan: Plan) -> list[str]:
    """Cycles in the task DAG, reported as the participating ids (plan §16.4 "DAG acyclic")."""
    blocked: dict[str, tuple[str, ...]] = {t.id: t.blocked_by for t in plan.tasks}
    state: dict[str, int] = {}  # 0 = unvisited, 1 = on stack, 2 = done
    found: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = trail[trail.index(node) :]
            found.append("tasks: dependency cycle " + " -> ".join([*cycle, node]))
            return
        state[node] = 1
        for parent in blocked.get(node, ()):
            if parent in blocked:
                visit(parent, [*trail, node])
        state[node] = 2

    for task_id in blocked:
        visit(task_id, [])
    return sorted(set(found))
