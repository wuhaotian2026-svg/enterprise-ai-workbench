# Security Policy

## Supported scope

This repository is a portfolio release of the current `main` branch. Security fixes are evaluated against the latest published version only.

## Reporting a vulnerability

Please do not publish credentials, exploit payloads, personal data, or production access details in a public issue. Before a remote repository is created, security reports should be sent privately to the repository owner through the contact channel listed on their GitHub profile. A dedicated security advisory channel can be enabled after publication.

Useful report contents include:

- affected commit and component;
- reproducible steps using fictional data;
- expected and actual authorization boundary;
- whether the issue can create, modify, disclose, or delete business data.

## Security boundaries

- LLM output is untrusted candidate data;
- write tools create proposals, not direct business writes;
- confirmation does not bypass authorization, idempotency, validation, auditing, or transactions;
- frontend visibility is not an authorization boundary;
- public sample data is fictional;
- `.env`, model weights, database volumes, uploads, browser profiles, and production logs are excluded from the repository.

See [docs/security-and-permissions.md](docs/security-and-permissions.md) for the application security model.
