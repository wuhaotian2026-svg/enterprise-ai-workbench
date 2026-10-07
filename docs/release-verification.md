# Public release candidate verification

This report records checks performed on the isolated public candidate on 2026-10-07. It is not a production certification and does not replace the module-specific evidence summarized in [`evaluation.md`](evaluation.md).

## Provenance and isolation

- Private source commit: `2839d6a7d3c234d3d022f86661a2ff0d85f9a345`.
- Export method: Git tracked-only archive with an explicit public allowlist; the private `.git` directory was not copied.
- Candidate repository: independent `main` with GitHub noreply identity, published at <https://github.com/wuhaotian2026-svg/enterprise-ai-workbench> after a private-first remote verification.
- License: no open-source license is granted in this candidate.
- Runtime isolation: Docker Compose project `enterprise_ai_workbench_public_candidate`, local port `18081`, dedicated network/database/uploads volumes and database `enterprise_ai_workbench_public_candidate`.
- Provider isolation: the candidate stack used `https://example.invalid/v1`; no real DeepSeek request or production credential was used.
- Data isolation: all seeded users, departments, requests and screenshots are fictional. No production database, cloud host or online Demo was accessed.
- Private source protection: source HEAD remained `2839d6a7d3c234d3d022f86661a2ff0d85f9a345` and its tracked worktree remained clean. Existing private untracked backups and outputs were neither copied nor changed.

## Security checks

- `.env`, uploads, database volumes, model weights, browser profiles, outputs, backups and private planning files are excluded by the public allowlist and `.gitignore`.
- The public candidate contains 459 non-ignored files before the initial commit; a path-policy check found zero forbidden or suspicious paths.
- Gitleaks 8.30.1 was installed from the official GitHub Release, and its archive SHA-256 was verified against the official checksums file.
- An early scan reported three generic-key findings. Structural review confirmed they were pinned SHA-256 values for tokenizer artifacts rather than credentials. `.gitleaks.toml` contains a narrow path-and-line-pattern allowlist for those three checksums only.
- Final fresh Gitleaks result: **0 findings** across approximately 12.58 MB scanned.
- GitHub Secret Scanning and Push Protection are enabled. Dependabot Security Updates are enabled; the first post-publication checks reported zero secret-scanning and Dependabot alerts.
- Fresh `npm audit` initially found one high-severity advisory in transitive `source-map-js` 1.2.1. A narrow lockfile update moved only that transitive package to 1.2.2; no force fix or unrelated major upgrade was used. The final audit reported zero known vulnerabilities.
- Local Markdown validation found zero broken local links.

## Frontend verification

| Check | Result |
|---|---|
| Procurement date-fixture RED | 5 failures caused by the now-historical fixed delivery date |
| Procurement focused GREEN | 39/39 passed |
| Vitest full | 19 files, 264/264 passed |
| TypeScript | exit 0 |
| Vite production build | 1,615 modules, exit 0 |
| npm audit | 0 vulnerabilities |

The source snapshot used Vitest 3.x and was affected by GHSA-82fw-gwwq-j7x9. The candidate pins Vitest 5.0.1. Two procurement test paths contained fixed dates that later became historical. The candidate injects the component's existing `currentDate` test seam; production procurement behavior is unchanged.

## Backend verification

| Check | Result |
|---|---|
| Workbench ProductEvent RED | catalog modules `procurement` and `approval-center` rejected by a stale duplicate contract |
| Workbench ProductEvent focused GREEN | 39/39 passed |
| Backend unit final | 1,867/1,867 passed |
| Python compileall | exit 0 |

The ProductEvent fix makes the event module allowlist reuse the authoritative workbench catalog. It does not add a new module or change business behavior. A real browser replay after rebuilding only the isolated API produced HTTP 204 for the approval-center module-open event and zero console errors or warnings.

## Compose and browser verification

- Docker operations used the required `wsl-engine` context; Docker Desktop and unrelated containers were not changed.
- `docker compose config --quiet` passed with `.env.example` as the public configuration contract.
- The fresh candidate database required administrator installation of `pg_trgm`, matching the documented fail-closed migration contract. Both `pg_trgm` and `vector` were then present.
- Alembic reached `0008_slot_extraction_operations (head)`.
- Database, embeddings, API and web containers were all healthy.
- `scripts/verify_compose.ps1` passed against `http://127.0.0.1:18081`.
- The built-in seed created 118 fictional records. A procurement request was submitted through the real deterministic form and confirmation flow, and appeared in the fictional manager's approval queue.
- Five representative screenshots were captured and visually inspected: knowledge assistant, HR assistant, procurement assistant, approval center and organization administration. They expose no password, cookie, token, real person or production record.

## GitHub publication verification

- The repository was created as Private, and `main` was pushed without force. The remote commit matched local commit `ce1ddcbdebb093ac1ad007319940e32fcf55c2f4`, with ahead/behind `0/0`, before visibility changed.
- The default branch is `main`; the repository description, online Demo homepage and eleven technical topics are configured.
- Visibility was changed to Public only after the private-stage checks passed.
- An unauthenticated request to the repository page returned HTTP 200 and contained the project title.
- Unauthenticated raw requests for the README and all five screenshots returned HTTP 200. The remote README SHA-256 matched the local file exactly.
- The unauthenticated GitHub API reported `visibility=public`, `private=false` and `default_branch=main`.
- No open-source license was added, and no production host, database, model provider or secret was changed as part of publication.

## Scope limits and known debt

- No real-model evaluation was rerun because it would require provider credentials and external API usage. README metrics were transcribed only after checking the committed evaluation evidence.
- The full PostgreSQL integration suite was not rerun in this publication pass. Isolation was instead verified through a fresh dedicated PostgreSQL database, migrations, seed, health smoke and real browser flows; backend unit coverage was run in full.
- Python dependencies use version ranges and do not yet have a lock file.
- The previously documented strict retrieval-only P95 debt remains disclosed in [`evaluation.md`](evaluation.md); no threshold was relaxed for publication.
- Candidate containers and volumes remain available for review. They were not deleted because this task did not authorize destructive cleanup.
