# Test Plan

> `/verify` runs and records functional tests (bugs) and non-functional requirement tests following this plan.
> Material for the human's release decision at **gate ⑤**.

## 1. Functional tests (requirement satisfaction)

Confirm each requirement's acceptance criteria are satisfied.

| Requirement | What to check | Means (auto/manual) | Result | Notes |
|------|----------|-------------------|------|------|
| R-1 | | auto | ⬜ | |
| R-2 | | | ⬜ | |

Legend: ✅ pass / ❌ fail / ⬜ not run

## 2. Non-functional requirement tests (criteria checklist)

> Criteria-based since it is stack-independent. Make it concrete for your product.

### Performance
- [ ] Main operations' response time within requirement
- [ ] No degradation at expected data volume

### Security (mandatory in `/verify`)
- [ ] Run **`/security-review`** and resolve findings (code vulnerability review)
- [ ] Run **`make audit`** and have no known dependency vulnerabilities (Python: pip-audit / frontend: pnpm audit)
- [ ] No plaintext storage / log output of secrets (gitleaks mechanically prevents this at the commit stage)
- [ ] Input validation / injection countermeasures

| Check | Result | Severity | Notes |
|------|------|--------|------|
| /security-review | ⬜ | | |
| make audit (Python) | ⬜ | | |
| make audit (frontend) | ⬜ | | |

### Reliability / operations
- [ ] Behavior on error is as defined
- [ ] Logs/monitoring emit the necessary information

## 3. Manual verification checklist (human-run acceptance)

Acceptance that automated tests can't cover — a human runs these and records the result. Make it concrete for
your product; unrun items become remaining issues at gate ⑤.

| Check | How | Result | Notes |
|-------|-----|--------|-------|
| Real player / device playback | open the output in the real target app / device | ⬜ | |
| Visual / aesthetic review | eyeball the output against the intent | ⬜ | |
| Supported-OS matrix | run on each Must-support OS | ⬜ | |
| Long-input / end-to-end performance | measure on a realistic full-size input | ⬜ | |

Legend: ✅ pass / ❌ fail / ⬜ not run

## 4. Defects found
| ID | Content | Severity | Task | Status |
|----|------|--------|-----------|------|
| | | | | |

## 5. Overall judgment (filled by the human)
- **Release decision**: hold / go / conditional go
- **Remaining issues**:
