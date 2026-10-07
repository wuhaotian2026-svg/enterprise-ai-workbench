# Third-party components

This document is an engineering inventory, not legal advice. The lockfiles and upstream projects remain the authoritative source for exact dependency versions and license terms.

## Runtime services and models

- DeepSeek is accessed as an external API service. No DeepSeek model weights are redistributed by this repository. Users must comply with the terms of their chosen Provider.
- `intfloat/multilingual-e5-small` is loaded locally through ONNX Runtime. The Hugging Face model metadata reported `MIT` for revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` during the release audit. Model weights are not included in this repository; users should re-check the upstream model card before download or redistribution.
- PostgreSQL, pgvector, Python, Node.js, nginx and Docker images retain their upstream licenses and notices.

## Frontend dependencies

The release-audit `package-lock.json` contained license metadata for every dependency. The observed distribution was:

| License | Packages |
|---|---:|
| MIT | 177 |
| Apache-2.0 | 8 |
| ISC | 9 |
| BSD-2-Clause | 2 |
| BSD-3-Clause | 2 |
| OFL-1.1 | 2 |
| MIT-0 | 1 |
| CC-BY-4.0 | 1 |

The current lockfile contains 202 packages and no missing license metadata. Consult `frontend/package-lock.json` before distribution because future dependency upgrades can change the transitive inventory.

## Project license

No open-source license is granted by this repository at this time. The project is published primarily as a personal engineering portfolio. Third-party components remain governed by their own licenses.
