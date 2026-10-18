"""Сторонние Docker-образы закреплены по digest и совпадают во всех файлах."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"^(?P<name>[^@\s]+)@sha256:[0-9a-f]{64}$")
# FROM <образ>, COPY --from=<образ> и image: <образ> в YAML.
REFERENCES = {
    "Dockerfile": re.compile(r"^\s*(?:FROM\s+(\S+)|COPY\s+--from=(\S+))", re.M),
    "compose.yaml": re.compile(r"^\s*image:\s*(\S+)", re.M),
    # В CI образы сканера и эмулятора QEMU передаются через переменные шага.
    ".github/workflows/ci.yml": re.compile(
        r"^\s*(?:image|TRIVY_IMAGE|BINFMT_IMAGE):\s*(\S+)", re.M
    ),
}
# Стадии многоэтапной сборки и собственные образы проекта из переменных.
LOCAL_STAGES = {"python-base", "builder", "ui-builder"}


def external_images() -> list[tuple[str, str]]:
    """Собрать ссылки на образы из реестров вместе с именем файла."""
    found = []
    for name, pattern in REFERENCES.items():
        for match in pattern.finditer((ROOT / name).read_text(encoding="utf-8")):
            reference = next(group for group in match.groups() if group)
            if reference in LOCAL_STAGES or reference.startswith("${"):
                continue
            found.append((name, reference))
    return found


def test_every_external_image_is_pinned_by_digest() -> None:
    """Тег без digest можно подменить в реестре: такие ссылки запрещены."""
    images = external_images()
    assert {name for name, _ in images} == set(REFERENCES)
    unpinned = [(name, ref) for name, ref in images if not DIGEST.match(ref)]
    assert unpinned == []


def test_same_image_uses_same_digest_everywhere() -> None:
    """Локальный стенд, тестовые сервисы и CI запускают одни и те же образы."""
    by_name: dict[str, set[str]] = {}
    for _, reference in external_images():
        by_name.setdefault(DIGEST.match(reference)["name"], set()).add(reference)
    assert {name: refs for name, refs in by_name.items() if len(refs) > 1} == {}
    assert {
        "python:3.12-slim",
        "postgres:17",
        "qdrant/qdrant:v1.19.1",
        "aquasec/trivy:0.75.0",
        "tonistiigi/binfmt:qemu-v10.2.3",
    } <= set(by_name)
