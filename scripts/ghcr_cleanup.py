"""Удалить из GHCR старые SHA-версии образов main, не трогая latest и релизы.

Каждый push в main оставляет в пакетах API и UI три тега: `sha-<commit>`,
`sha-<commit>-amd64` и `sha-<commit>-arm64`. Кандидат на удаление — версия,
у которой все теги такие и которая не менялась дольше заданного числа дней.
Версии с `latest`, `vX.Y.Z` или любым другим тегом и всё, на что они ссылаются
(manifest архитектур, подписи cosign, SBOM-attestation), остаются. Подписи
и manifest старого index удаляются вместе с ним, только если на них не ссылается
ничего сохраняемого. Без `--delete` скрипт лишь показывает план.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

try:  # Запуск `python3 scripts/ghcr_cleanup.py`: папка скрипта в sys.path.
    from release_delivery import (
        GITHUB_REPOSITORY,
        INDEX_TYPES,
        MANIFEST_TYPES,
        REPOSITORY,
        DeliveryError,
        NoRedirect,
        Registry,
    )
except ModuleNotFoundError:  # Импорт `scripts.ghcr_cleanup` из тестов.
    from scripts.release_delivery import (
        GITHUB_REPOSITORY,
        INDEX_TYPES,
        MANIFEST_TYPES,
        REPOSITORY,
        DeliveryError,
        NoRedirect,
        Registry,
    )

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA_TAG = re.compile(r"sha-[0-9a-f]{40}(?:-amd64|-arm64)?")
# cosign v2: sha256-<hex>.sig/.att/.sbom; OCI 1.1 без Referrers API: sha256-<hex>.
REFERRER_TAG = re.compile(r"sha256-(?P<hex>[0-9a-f]{64})(?:\.sig|\.att|\.sbom)?")
MAX_RESPONSE = 5 * 1024 * 1024


@dataclass(frozen=True)
class Version:
    """Версия пакета GHCR: один manifest со всеми его тегами."""

    id: int
    digest: str
    tags: tuple[str, ...]
    updated: datetime

    @property
    def kind(self) -> str:
        """Роль версии: sha, referrer (подпись), untagged или protected."""
        if not self.tags:
            return "untagged"
        if all(SHA_TAG.fullmatch(tag) for tag in self.tags):
            return "sha"
        if all(REFERRER_TAG.fullmatch(tag) for tag in self.tags):
            return "referrer"
        # latest, vX.Y.Z, смешанные и любые незнакомые теги не удаляются.
        return "protected"


def parse_time(value: object) -> datetime:
    """Прочитать время GitHub API в формате ISO 8601 с часовым поясом."""
    if not isinstance(value, str):
        raise DeliveryError("GitHub API вернул версию без даты")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise DeliveryError("GitHub API вернул неверную дату версии") from None
    if moment.tzinfo is None:
        raise DeliveryError("GitHub API вернул дату версии без часового пояса")
    return moment


def parse_versions(payload: object) -> list[Version]:
    """Проверить страницу ответа GitHub API и вернуть версии пакета."""
    if not isinstance(payload, list):
        raise DeliveryError("GitHub API вернул неожиданный список версий")
    versions = []
    for item in payload:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        container = metadata.get("container") if isinstance(metadata, dict) else None
        tags = container.get("tags") if isinstance(container, dict) else None
        if (
            not isinstance(item, dict)
            or type(item.get("id")) is not int
            or item["id"] <= 0
            or not isinstance(item.get("name"), str)
            or not DIGEST.fullmatch(item["name"])
            or not isinstance(tags, list)
            or not all(isinstance(tag, str) for tag in tags)
        ):
            raise DeliveryError("GitHub API вернул неверное описание версии")
        # Новый тег на старом manifest обновляет updated_at: берём более позднюю.
        updated = max(
            parse_time(item.get("created_at")), parse_time(item.get("updated_at"))
        )
        versions.append(Version(item["id"], item["name"], tuple(tags), updated))
    return versions


def children(data: object) -> list[str]:
    """Вернуть digest всех manifest, на которые ссылается index или список."""
    if not isinstance(data, dict):
        raise DeliveryError("Manifest не является JSON-объектом: связи не определены")
    if "manifests" not in data:
        if data.get("mediaType") in INDEX_TYPES:
            raise DeliveryError("Image index не содержит список manifests")
        return []
    manifests = data["manifests"]
    if not isinstance(manifests, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("digest"), str)
        and DIGEST.fullmatch(item["digest"])
        for item in manifests
    ):
        raise DeliveryError("Неверный список manifests: связи версий не определены")
    return [item["digest"] for item in manifests]


def subject(data: object) -> str | None:
    """Вернуть digest образа, к которому относится подпись или attestation OCI 1.1."""
    if not isinstance(data, dict) or "subject" not in data:
        return None
    target = data["subject"]
    digest = target.get("digest") if isinstance(target, dict) else None
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise DeliveryError("Неверный subject: связи OCI-артефакта не определены")
    return digest


def require_days(days: int) -> None:
    """Срок 0 удалил бы и только что опубликованный main: допускаем 1–3650 дней."""
    if not 1 <= days <= 3650:
        raise DeliveryError("Срок хранения должен быть от 1 до 3650 дней")


def plan_cleanup(versions: list[Version], fetch, *, now: datetime, days: int) -> dict:
    """Выбрать версии для удаления; fetch(digest) обязан вернуть manifest.

    Сохраняемые корни — защищённые версии, свежие SHA и все версии, которые
    не относятся к старым SHA. Сохраняем замыкание их ссылок: даже index без
    тега не потеряет нужный ему manifest. Удаляем только старые SHA и их
    уникальные связи. Неизвестный manifest останавливает весь план.
    """
    require_days(days)
    cutoff = now - timedelta(days=days)
    by_digest = {version.digest: version for version in versions}
    if len(by_digest) != len(versions) or len({v.id for v in versions}) != len(
        versions
    ):
        raise DeliveryError("Список версий содержит дубликаты; нужен новый снимок")
    cache: dict[str, object] = {}

    def manifest(digest: str) -> object:
        """Прочитать manifest один раз за запуск."""
        if digest not in cache:
            data = fetch(digest)
            if not isinstance(data, dict):
                raise DeliveryError(
                    f"Manifest {digest} недоступен: безопасный план не определён"
                )
            if (
                type(data.get("schemaVersion")) is not int
                or data["schemaVersion"] != 2
                or data.get("mediaType") not in (*INDEX_TYPES, *MANIFEST_TYPES)
            ):
                raise DeliveryError(
                    f"Manifest {digest}: неподдерживаемый формат, связи неизвестны"
                )
            cache[digest] = data
        return cache[digest]

    attached: dict[str, set[str]] = defaultdict(set)
    for version in versions:
        for tag in version.tags:
            if match := REFERRER_TAG.fullmatch(tag):
                attached["sha256:" + match["hex"]].add(version.digest)
        # Подпись OCI 1.1 может не иметь тега: её образ указан в поле subject.
        if version.kind in ("untagged", "referrer"):
            if target := subject(manifest(version.digest)):
                attached[target].add(version.digest)

    def links(digest: str) -> list[str]:
        """Manifest из index и подписи/attestation этого digest."""
        data = manifest(digest)
        targets = children(data) + sorted(attached.get(digest, ()))
        # Сохраняемый OCI-артефакт с subject тоже требует свой исходный образ.
        if target := subject(data):
            targets.append(target)
        return targets

    def closure(roots: list[str]) -> set[str]:
        """Все digest, достижимые из корней по ссылкам."""
        seen: set[str] = set()
        queue = deque(roots)
        while queue:
            digest = queue.popleft()
            if digest not in seen:
                seen.add(digest)
                queue.extend(links(digest))
        return seen

    protected = [v.digest for v in versions if v.kind == "protected"]
    recent = [v.digest for v in versions if v.kind == "sha" and v.updated >= cutoff]
    old = [v.digest for v in versions if v.kind == "sha" and v.updated < cutoff]
    candidates = closure(old)
    # Несвязанный untagged/referrer остаётся: все его ссылки тоже должны
    # остаться, даже если child имеет старый SHA-тег в другом index.
    unrelated = sorted(set(by_digest) - candidates)
    keep = closure(protected + recent + unrelated)
    selected = (candidates - keep) & set(by_digest)
    # Родитель раньше потомков: прерванное удаление не оставит этот index
    # со ссылками на уже удалённые manifest. Несвязанные остатки сохраняются.
    order: list[str] = []
    visited: set[str] = set()

    def visit(digest: str) -> None:
        """Добавить digest после всех его потомков (обратный порядок DFS)."""
        visited.add(digest)
        for target in links(digest):
            if target in selected and target not in visited:
                visit(target)
        order.append(digest)

    for digest in [version.digest for version in versions]:
        if digest in selected and digest not in visited:
            visit(digest)
    remove = [by_digest[digest] for digest in reversed(order)]
    reasons = {
        "sha": "SHA-теги старше срока",
        "referrer": "подпись или attestation удаляемого образа",
        "untagged": "manifest или подпись удаляемого образа",
    }
    return {
        "cutoff": cutoff.isoformat(),
        "versions": len(versions),
        "protected": len(protected),
        "recent": len(recent),
        "keep": len(keep & set(by_digest)),
        "remove": [
            {
                "id": version.id,
                "digest": version.digest,
                "tags": list(version.tags),
                "updated": version.updated.isoformat(),
                "reason": reasons[version.kind],
            }
            for version in remove
        ],
    }


class Packages:
    """Читать и удалять версии контейнерных пакетов через REST API GitHub."""

    def __init__(self, token: str, api_url: str, repository: str) -> None:
        """Создать клиент; токен передаётся только в заголовке Authorization."""
        if not token:
            raise DeliveryError("Для GitHub API нужен GITHUB_TOKEN")
        if not api_url.startswith("https://"):
            raise DeliveryError("Адрес GitHub API должен использовать HTTPS")
        if not GITHUB_REPOSITORY.fullmatch(repository):
            raise DeliveryError("Неверное имя GitHub-репозитория")
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.repository = repository
        self.opener = build_opener(NoRedirect())
        self.base = ""

    def request(self, method: str, path: str) -> object:
        """Выполнить запрос; любой отказ останавливает очистку без вывода токена."""
        request = Request(
            self.api_url + path,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(MAX_RESPONSE + 1)
        except HTTPError as error:
            raise DeliveryError(
                f"GitHub API отклонил запрос: HTTP {error.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise DeliveryError("GitHub API недоступен; очистка остановлена") from None
        if len(body) > MAX_RESPONSE:
            raise DeliveryError("GitHub API вернул слишком большой ответ")
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise DeliveryError("GitHub API вернул неверный JSON") from None

    def package_path(self, package: str) -> str:
        """Путь пакета владельца репозитория: пользователя или организации."""
        if not self.base:
            data = self.request("GET", f"/repos/{self.repository}")
            owner = data.get("owner") if isinstance(data, dict) else None
            login = owner.get("login") if isinstance(owner, dict) else None
            kind = owner.get("type") if isinstance(owner, dict) else None
            if not isinstance(login, str) or kind not in ("User", "Organization"):
                raise DeliveryError("GitHub API не сообщил владельца репозитория")
            scope = "users" if kind == "User" else "orgs"
            self.base = f"/{scope}/{quote(login, safe='')}"
        return f"{self.base}/packages/container/{quote(package, safe='')}"

    def versions(self, package: str) -> list[Version]:
        """Прочитать все активные версии пакета постранично."""
        result = []
        for page in range(1, 1001):
            payload = self.request(
                "GET",
                f"{self.package_path(package)}/versions"
                f"?state=active&per_page=100&page={page}",
            )
            versions = parse_versions(payload)
            result.extend(versions)
            if len(versions) < 100:
                return result
        raise DeliveryError("Слишком много версий пакета для одного запуска")

    def version(self, package: str, version_id: int) -> Version:
        """Перечитать одну версию непосредственно перед необратимым запросом."""
        data = self.request(
            "GET", f"{self.package_path(package)}/versions/{version_id}"
        )
        return parse_versions([data])[0]

    def delete(self, package: str, version: Version) -> None:
        """Удалить одну версию; GitHub хранит её для восстановления 30 дней."""
        self.request("DELETE", f"{self.package_path(package)}/versions/{version.id}")


def manifest_reader(registry, remote: str):
    """Вернуть функцию чтения manifest по digest со сверкой SHA256 тела ответа."""

    def fetch(digest: str) -> object:
        """Прочитать manifest; None означает HTTP 404 registry."""
        response = registry.fetch(remote, digest)
        if response is None:
            return None
        data, body, _ = response
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise DeliveryError(f"GHCR вернул для {remote}@{digest} другое содержимое")
        return data

    return fetch


def clean_packages(
    packages: Packages,
    registry,
    repository: str,
    *,
    days: int,
    delete: bool,
    now: datetime,
) -> list[dict]:
    """Составить план для API и UI и при `delete` удалить выбранные версии."""
    require_days(days)
    repository = repository.lower()
    if not REPOSITORY.fullmatch(repository) or len(repository) > 250:
        raise DeliveryError("Неверное имя GitHub-репозитория для GHCR")
    owner, name = repository.split("/")
    report = []
    snapshots = {}
    for package in (name, name + "-ui"):
        versions = packages.versions(package)
        plan = plan_cleanup(
            versions,
            manifest_reader(registry, f"{owner}/{package}"),
            now=now,
            days=days,
        )
        plan["package"] = f"{registry.host}/{owner}/{package}"
        plan["deleted"] = []
        snapshots[package] = {version.id: version for version in versions}
        report.append(plan)
    if not delete:
        return report

    # Ошибка чтения UI или его графа должна остановить очистку до удаления API.
    # Перед первой DELETE сверяем оба полных снимка: новый защищённый index
    # мог появиться после чтения первого пакета и ссылаться на его кандидата.
    for package, snapshot in snapshots.items():
        current_versions = packages.versions(package)
        current = {version.id: version for version in current_versions}
        if len(current) != len(current_versions) or current != snapshot:
            raise DeliveryError(
                f"Версии {package} изменились после плана; нужен новый запуск"
            )
    for package, plan in zip(snapshots, report, strict=True):
        for candidate in plan["remove"]:
            version = snapshots[package][candidate["id"]]
            if version.kind == "protected":
                raise DeliveryError(f"Защищённая версия {version.digest} в плане")
            # Не удаляем по старому списку: версия могла получить latest,
            # vX.Y.Z или новый SHA-тег. Любое изменение требует нового плана.
            current = packages.version(package, version.id)
            if current != version:
                raise DeliveryError(
                    f"Версия {version.id} изменена после плана; очистка остановлена"
                )
            packages.delete(package, current)
            plan["deleted"].append(version.id)
            print(f"Удалена версия {version.id}: {version.digest}", flush=True)
    return report


def write_summary(report: list[dict], *, days: int, delete: bool) -> None:
    """Показать план или результат в журнале и сводке GitHub Actions."""
    mode = "удаление" if delete else "dry-run: ничего не удалено"
    lines = [f"## Очистка SHA-версий GHCR старше {days} дн. ({mode})", ""]
    for plan in report:
        lines += [
            f"### `{plan['package']}`",
            "",
            f"- Версий: {plan['versions']}; защищённых корней: {plan['protected']}; "
            f"свежих SHA: {plan['recent']}; сохраняется: {plan['keep']}",
            f"- К удалению: {len(plan['remove'])}; удалено: {len(plan['deleted'])}",
        ]
        for item in plan["remove"][:50]:
            tags = ", ".join(item["tags"]) or "без тега"
            lines.append(f"  - `{item['digest']}` ({tags}) — {item['reason']}")
        if len(plan["remove"]) > 50:
            lines.append(f"  - … ещё {len(plan['remove']) - 50}, полный список в JSON")
        lines.append("")
    result = "\n".join(lines) + "\n"
    print(result)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(result)


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Запустить план или удаление и вернуть код ошибки для GitHub Actions."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        packages = Packages(
            token,
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            repository,
        )
        registry = Registry(os.environ.get("GITHUB_ACTOR", ""), token, scope="pull")
        report = clean_packages(
            packages,
            registry,
            repository,
            days=arguments.days,
            delete=arguments.delete,
            now=datetime.now(UTC),
        )
    except DeliveryError as error:
        print(f"Очистка остановлена: {error}", file=sys.stderr)
        return 1
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(
                {
                    "days": arguments.days,
                    "delete": arguments.delete,
                    "packages": report,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    write_summary(report, days=arguments.days, delete=arguments.delete)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
