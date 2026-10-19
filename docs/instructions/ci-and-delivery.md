# CI and delivery to GHCR

> **Текущая настройка подписи:** по выбору пользователя от 19.10.2026
> публичная keyless-подпись и SBOM attestations подготовлены, но **выключены**.
> Для включения владелец задаёт `GHCR_PUBLIC_SIGNING=true` в GitHub → Settings →
> Secrets and variables → Actions → Variables. Это разрешает публикацию metadata
> в Rekor. Без этой переменной установка cosign и шаг `sign` пропускаются;
> сборки, проверка SBOM, артефакт `sbom` и публикация API/UI в GHCR работают.
> Разделы о cosign ниже описывают поведение после явного включения.


[← README](../../README.md) · [All documentation](../../README.md#documentation)

The workflow checks every push to `main`, pull request targeting `main`,
manual check, and explicit release tag push matching `v*`.
A first job accepts only stable `vX.Y.Z` tags with no leading zero or suffix,
resolves annotated tags to the exact run commit, and requires that commit
to belong to the history of `origin/main`.

```text
Validate delivery target: exact commit + strict release tag + main ancestry
                                  ↓
Python checks: lock + age + workflow/docs + migrations + Ruff + tests/coverage ≥85% + evaluations
Dependency audit: locked API/UI packages + current vulnerability data
Portability: Linux, macOS, Windows — uv.lock + docs + Ruff + unit tests + real API/UI start
                                  ↓ all three must pass
API/UI builds + UID/tools gates + arm64 build/start under QEMU + SBOM and Trivy (4 images)
  + Compose + knowledge + HTTP + persistence + backup → clean volumes → restore → smoke
  + Streamlit health/page render + demo review branches + license log leak check
                                  ↓
Publish the same tested API and UI images (amd64 + arm64) to GHCR under shared tags
  → cosign keyless signature of each index digest + SBOM attestations → cosign verify
```

The `portability` matrix runs on `ubuntu-24.04`, `macos-15` (arm64), and `windows-2025` with the same uv, Python 3.12, and `uv.lock`: documentation checks, Ruff, all unit tests, offline evaluations, and `scripts/check_local_start.py`, which starts Uvicorn and Streamlit as real processes and checks them. Integration tests and Docker stay on Linux; arm64 images are built and started under QEMU in the container job and published together with amd64.

The Python job installs uv **0.12.23**, requires packages in `uv.lock` to be
at least 14 days old, validates every workflow with checksum-pinned actionlint,
and checks Markdown links, Bash/JSON examples and Python docstrings.
It applies migrations to an isolated PostgreSQL test database, checks schema
drift, runs Ruff, all tests, coverage and offline evaluations. Coverage of
`app`, `operator_ui`, `evaluations` and `scripts` must reach 85%; the PDF
generator is excluded. Reports include XML, JSON and HTML. The independent
security job audits locked API and UI runtime requirements with hashes;
findings or failed verification block delivery. CI uses mock providers
without OpenAI credentials.

API and UI images are built once for amd64 and once for arm64, and all four are scanned with digest-pinned Trivy.
The same Trivy 0.75.0 writes a CycloneDX SBOM for each of the four images
(`sbom` artifact, 30 days); each SBOM records the tested image ID.
The container scenario checks non-root users and excluded development tools,
indexes knowledge, verifies API operations, runs a mock agent and both review
branches, recreates containers while retaining PostgreSQL/Qdrant volumes,
resumes a waiting review, renders the real Streamlit page, and confirms that
license keys did not enter the JSON API log.
Затем `scripts/backup.py` делает копию проекта `ai-support-ci` (дамп PostgreSQL,
snapshot Qdrant, манифест с SHA256), проверяет её и убеждается, что `restore`
отказывается менять работающий стенд. После `down --volumes` копия
восстанавливается в чистые тома; записи, запуск агента, поиск и демо
совпадают побайтно, а прежний токен снова проходит smoke `--check-review`.
Копия остаётся во временном каталоге runner и не загружается артефактом
([резервное копирование](backup.md)). All third-party images and
external actions are pinned by digest or commit.

The `tested-docker-images` artifact contains one `tested-images.tar` archive
with all four images and `tested-images.json` with the full commit and their
image IDs. Export and load check each image's platform. The publication job
loads that archive, checks every ID and never rebuilds. Each image is pushed
under an architecture tag (`<tag>-amd64`, `<tag>-arm64`); then
`docker buildx imagetools create` joins their digests into a shared tag, an
image index from which Docker pulls the image for the local architecture,
so Apple Silicon and ARM servers run it natively. It publishes:

| Package | Main push | Explicit valid release tag |
| --- | --- | --- |
| `ghcr.io/merkertg/ai-support-agent` | `sha-<full SHA>` (+ `-amd64`, `-arm64`) and eligible `latest` | `vX.Y.Z` (+ `-amd64`, `-arm64`) |
| `ghcr.io/merkertg/ai-support-agent-ui` | `sha-<full SHA>` (+ `-amd64`, `-arm64`) and eligible `latest` | `vX.Y.Z` (+ `-amd64`, `-arm64`) |

`latest` is updated only when a main run still matches the current main tip
at the time of the GitHub API check. A release tag changes neither `latest`
nor main SHA tags. Before pushing either release image, all six remote version
references are checked: a missing manifest allows publication, an identical
config/manifest/index ID is reused, a shared tag must be an index of exactly
the tested architecture manifests, and a conflicting image blocks both writes. Network or
authorization errors are failures, not proof of a free tag. Classic Docker config IDs and containerd manifest/index IDs are supported.
For an index, only its full digest can match the tested ID; child manifest
digests cannot approve a different index. Media types, schema and nonempty
index descriptors are validated, and raw response digests are verified after publication. After creating a shared tag the job requires `linux/amd64` and `linux/arm64` to point to the published manifests; attestation entries are skipped, other platforms are rejected. A failed partial push
can resume with the same artifact; a fresh rebuild may require a new version, while a main SHA tag is rewritten for a rebuilt commit.
The workflow does not choose version numbers, create GitHub Releases or
deploy to an external server.

Before the first registry write, `publish` checks that the four SBOMs exist
and carry the tested image IDs, and installs cosign 3.1.3 from the official
release with a pinned SHA256. After publication it writes
`published-images.json`, and `release_delivery.py sign` signs the **index
digest** from that report for both packages (also `latest` if its digest
differs), never a tag. Each platform manifest referenced by the index gets
its SBOM as a `cyclonedx` attestation; nothing is rebuilt. `cosign verify`
and `verify-attestation` then require the identity
`https://github.com/merkertg/ai-support-agent/.github/workflows/ci.yml@<ref>`,
the issuer `https://token.actions.githubusercontent.com` and the run commit
SHA. Signing is keyless: only `publish` has `id-token: write`, and no key or
secret is stored. The certificate and its transparency log entry are public
in Sigstore (Rekor) and reveal the repository name, workflow path, ref and
commit SHA. Verification commands for image users are in
[RELEASES.md](../RELEASES.md#5-подпись-cosign-и-sbom).

A separate workflow, `.github/workflows/ghcr-cleanup.yml`, runs every Sunday
and plans the removal of main SHA versions (`sha-…`, `-amd64`, `-arm64`)
not changed for 30 days. Versions tagged `latest`, `vX.Y.Z` (with or without
an architecture suffix) or any other non-SHA tag, and everything they
reference — platform manifests, BuildKit attestations, cosign signatures and
SBOM attestations — are kept. It is a dry run by default: deletion requires
the `delete` input of a manual run or the repository variable
`GHCR_CLEANUP_DELETE=true`, and the delete job shares the `ghcr-delivery`
queue with `publish`. Selection logic lives in `scripts/ghcr_cleanup.py`.

The six job names are **Validate delivery target**, **Python checks**,
**Audit runtime dependencies**, **Python on ubuntu-24.04 / macos-15 /
windows-2025** (one matrix job), **Build and smoke test Docker**, and
**Publish tested image to GHCR**. Publication is skipped for PR and all
`workflow_dispatch` runs, including dependency update checks. Artifacts
`pytest-report`, `coverage-report`, `evaluation-report`, `dependency-audit`
and `image-scan` last 7 days; `sbom` lasts 30 days; `tested-docker-images`
lasts 1 day.

A separate dependency update workflow prepares changes and opens or updates
a PR; it never merges automatically. It explicitly dispatches ordinary CI
for the update branch because changes made by `GITHUB_TOKEN` do not trigger
PR workflows by themselves. Publication remains disabled for that dispatch.

**[Подробная инструкция CI/CD на русском](../CI_CD.md)** covers permissions,
logs, artifacts, local checks and failures.
**[Версии и восстановление публикации](../RELEASES.md)** explains the exact
release procedure, signature and SBOM verification, cleanup and running an
API/UI pair.
**[Обновление зависимостей](../DEPENDENCY_UPDATES.md)** describes its schedule
and review workflow. A local result is not confirmation of a GitHub run;
confirm the exact commit or tag in Actions and both package digests.
