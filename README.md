# Latchway documentation

This repository is the Mintlify source for Latchway's public documentation.
Latchway is a self-hostable gateway that lets mobile and web applications call
configured AI infrastructure without embedding upstream-provider credentials.

The documentation currently describes the repository-local tested version 1
source candidate. It is not an independent security review or an announcement
that the server image or SDK packages have been published. Release status is
tracked on the site and in the core repository's evidence ledger.

## Local development

Requirements:

- Node.js 20.17 or newer
- pnpm 10.15.0

Install the pinned Mintlify CLI and validate the site:

```sh
pnpm install --frozen-lockfile
pnpm check
```

Start a local preview at `http://localhost:3000`:

```sh
pnpm dev
```

## Deployment

Mintlify deployment is GitHub-App driven. Connect this repository and its
`main` branch in the Mintlify dashboard; every successful push is then built
and deployed by Mintlify. The workflow in `.github/workflows/docs-checks.yml`
validates configuration, internal links, anchors, redirects, snippets, and
accessibility before changes merge. It does not deploy and needs no secret.

## Content policy

- Keep security and protocol claims aligned with the canonical core and SDK
  repositories.
- Do not describe a source candidate as a published release.
- Never add credentials, real user identifiers, raw attestation evidence, or
  private deployment endpoints.
- Use root-relative links for pages in this site.

## License

Apache License 2.0. See the canonical project repositories for `LICENSE` and
`NOTICE`.
