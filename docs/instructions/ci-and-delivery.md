# CI and delivery to GHCR

[← README](../../README.md) · [All documentation](../../README.md#documentation)

What the GitHub Actions workflow checks and how the tested image is published.

The workflow runs for pushes to `main`, pull requests targeting `main`, and manual checks:

```text
Python checks: lock + workflow/docs + PostgreSQL/Qdrant + migrations + Ruff + tests
Dependency audit: locked runtime packages + current PyPI vulnerability data
                                  ↓ both must pass
Docker build + runtime UID/tools gate + Compose + knowledge + HTTP + persistence
                                  ↓
Publish the tested image to GHCR (push to main only)
```

The Python job validates the workflow with checksum-pinned actionlint 1.7.12, checks Markdown links, Bash/JSON examples and Python docstrings with `scripts/check_project.py`, applies migrations to a separate PostgreSQL test database and runs `alembic check` to detect differences between the schema and models. The independent `security` job exports runtime requirements with hashes from `uv.lock` and uses pinned `pip-audit==2.10.1` to query current vulnerability information. Findings, missing package data or network errors fail the job; the report upload runs with `always()`. The container job waits for both checks, builds once, confirms UID/GID 10001 and absence of pytest, Ruff and uv, starts `db → migrate`, `qdrant → api`, indexes the knowledge base, checks health/readiness, the business API, knowledge search, a mock draft, a saved mock agent run with sources, and an approved review with delivery, and leaves one more workflow waiting. It compares saved demo records, the reviewed run, and search results before and after removing and recreating the Compose containers while retaining the PostgreSQL and Qdrant volumes; a second indexing run must report no changes, and the waiting LangGraph workflow is then resumed in the new containers with `--review-run`. CI explicitly selects mock with no OpenAI credentials. The draft and agent smoke checks create disposable fixture records only after confirming mock configuration. A failed check blocks publication. The publication job receives that same image as an artifact and does not rebuild it.

Images use `ghcr.io/merkertg/ai-support-agent:sha-<full-commit-SHA>`. The current tip of `main` can also update `latest`. The workflow delivers an image to the registry; running it on an external server is a later step.

**[Подробная инструкция CI/CD на русском](../CI_CD.md)** explains GitHub permissions, logs, artifacts, image tags, local reproduction, and troubleshooting. A local `git commit` starts no GitHub jobs until the commit is pushed. Confirm execution and publication in an actual GitHub Actions run.


The four job names are **Python checks**, **Audit runtime dependencies**, **Build and smoke test Docker**, and **Publish tested image to GHCR**. The last job is skipped for PRs and manual checks. Artifacts include `pytest-report` and `dependency-audit` (7 days), plus `tested-docker-image` for main pushes (1 day). A local test result does not confirm a remote Actions run.
