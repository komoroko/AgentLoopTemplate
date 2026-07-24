# Trust, signing keys, and the OCI sandbox (0.9.0 setup)

Two things live **outside** the repository on purpose, so a pull request can never widen its own
authority or weaken its own sandbox: the Trust Manifest (who may approve) and the signing keys.
A third, the OCI sandbox image, is built locally and pinned by digest in `config.yaml`. `agentloop
doctor` reports each of these as `FAIL` until it is set up — that is the expected fresh-repo state,
not a bug.

## 1. Signing keys (ssh-keygen)

Gates open only by a **signed attestation**, so each approver needs a key. Generate one per person,
never committed to the repo:

```bash
ssh-keygen -t ed25519 -C "maintainer@example.com" -f ~/.ssh/agentloop_sign
```

The **fingerprint** (`ssh-keygen -lf ~/.ssh/agentloop_sign.pub`) is what the Trust Manifest names —
authority is bound to the key, not to a username a PR could edit.

## 2. The Trust Manifest (external)

Create it outside the repository — the default is `$XDG_CONFIG_HOME/agentloop/trust.yaml`
(`~/.config/agentloop/trust.yaml`), overridable with `AGENTLOOP_TRUST_MANIFEST`:

```yaml
principals:
  - fingerprint: "SHA256:…"        # from ssh-keygen -lf …pub
    email: maintainer@example.com
    roles: [gate_reviewer, release_approver]
    domains: [agentloop-core]
```

Approving a gate then is: `agentloop approve <gate>` (emits a request) → `agentloop attestation sign
<request>` (a separate process holds the key; the web UI never sees it) → `agentloop attestation
import <signed>` (records the receipt and opens the gate).

## 3. The OCI sandbox image

Repository code, tests, and oracles run in a sealed OCI sandbox — never on the host, where a
test file an agent wrote would run with your credentials. Build the bundled images and pin their
digests:

```bash
agentloop oci build --profile python      # prints the built image digest
agentloop oci build --profile reviewer
agentloop oci build --profile implementer
agentloop oci verify                       # checks the pinned digests match the local images
```

`oci build` writes the `image: <ref>@sha256:…` pin back into `config.yaml`. Until every executor
profile is digest-pinned, `doctor` reports the sandbox as `FAIL` (a `kind: host` profile runs repo
code on the host). The Containerfiles are materialized under `.agentloop/oci/`, so the environment
a review ran in is auditable from the repository, not only inside the wheel that built it.

## What doctor checks

`agentloop doctor` groups these under **trust** (manifest present, ssh-keygen available), **sandbox**
(every profile digest-pinned), and **review** (independence groups). A single-adapter environment
that runs two distinct models of one provider gets a `WARN` on independence — mechanically distinct,
but weaker than two providers; a critical change then needs a signed expert attestation or a
deterministic check instead.
