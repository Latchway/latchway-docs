# Mintlify production evidence

The production-evidence workflow is a fail-closed observer. It does not deploy
the documentation or promote a Mintlify preview. Before it can pass, all of the
following external controls must exist:

- `docs.latchway.dev` resolves over public DNS and serves the Mintlify production
  site over HTTPS.
- Mintlify creates a GitHub deployment for `main` with environment `production`,
  `production_environment: true`, and `transient_environment: false`.
- The GitHub environment `documentation-production-evidence` is protected with
  required reviewers and a `main`-only deployment branch rule.

Staging and transient Mintlify deployment events are intentionally ignored. A
maintainer can replay a fresh production deployment with the workflow's manual
`deployment_id` input; the same deployment, actor, commit, status, URL, and
freshness checks apply.

## Retained artifact contract

The artifact name is:

```text
latchway-mintlify-production-<documentation_commit>-<deployment_id>-<run_id>-<run_attempt>
```

It is retained for 90 days and contains exactly these three regular files:

```text
latchway-mintlify-production-evidence.json
latchway-mintlify-production-evidence.SHA256SUMS
latchway-mintlify-production-evidence.attestation.sigstore.json
```

The JSON document conforms to
`schemas/mintlify-production-evidence.schema.json`. Its `workflow` object binds
the run event, expected successful conclusion, head SHA, run ID and attempt to
`.github/workflows/mintlify-production-evidence.yml` on `refs/heads/main`. Its
source checkpoint binds the exact documentation deployment commit to the
canonical core commit, source-manifest hash, source-tree hash, and owned-file
count. The newest successful production deployment status must be no more than
86,400 seconds old (with at most 300 seconds of future clock skew) and both the
deployment and status must be owned by GitHub actor ID `109931778`
(`mintlify[bot]`).

## Independent verification

First verify the checksum and the evidence semantics:

```bash
sha256sum --check latchway-mintlify-production-evidence.SHA256SUMS
python3 scripts/mintlify-production-evidence.py verify \
  --evidence latchway-mintlify-production-evidence.json \
  --deployment-id "$DEPLOYMENT_ID" \
  --documentation-commit "$DOCUMENTATION_COMMIT"
```

The verifier prints `Mintlify production evidence verification passed` only
after every invariant and observation digest passes.

Then verify the retained Sigstore bundle, binding both the signer and source
digests to `workflow.head_sha` from the evidence:

```bash
gh attestation verify latchway-mintlify-production-evidence.json \
  --repo Latchway/latchway-docs \
  --bundle latchway-mintlify-production-evidence.attestation.sigstore.json \
  --signer-workflow Latchway/latchway-docs/.github/workflows/mintlify-production-evidence.yml \
  --source-ref refs/heads/main \
  --source-digest "$WORKFLOW_HEAD_SHA" \
  --signer-digest "$WORKFLOW_HEAD_SHA" \
  --deny-self-hosted-runners \
  --format json
```

The release observer must also query GitHub rather than trusting user-controlled
predicate fields. It must require the exact workflow path, run ID and attempt,
event, `main` ref, head SHA, and a completed `success` conclusion; re-fetch the
deployment and newest status by their IDs; enforce the production and Mintlify
actor invariants; and recompute the evidence/checksum/observation hashes without
executing code from the documentation repository under release credentials.
