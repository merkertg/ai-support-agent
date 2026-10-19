"""Ограничение частоты изменений: «ведро токенов» на сотрудника в памяти процесса.

Ведро вмещает N разрешений (WRITE_RATE_LIMIT_PER_MINUTE) и пополняется
равномерно: N за минуту. Короткая серия до N запросов проходит сразу, а
постоянный поток выше лимита получает HTTP 429 с заголовком Retry-After.
Ключ — id сотрудника, а не токена: второй токен не удваивает лимит.

Состояние живёт в одном процессе Uvicorn. Compose запускает один процесс; при
нескольких процессах или репликах лимит действует в каждом отдельно, и общий
лимит задают на шлюзе или в Redis.
"""

import math
import threading
import time
from collections.abc import Callable, Hashable


class RateLimiter:
    """Потокобезопасное ведро токенов на каждый ключ."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Создать пустой набор вёдер; часы подменяются в тестах."""
        self._clock = clock
        self._lock = threading.Lock()
        # Ключ → (доступные разрешения, время последнего пересчёта).
        self._buckets: dict[Hashable, tuple[float, float]] = {}

    def acquire(self, key: Hashable, per_minute: int) -> int | None:
        """Взять разрешение; вернуть None или целые секунды до следующего.

        Синхронные обработчики FastAPI выполняются в пуле потоков, поэтому
        чтение и запись ведра защищены блокировкой.
        """
        rate = per_minute / 60
        with self._lock:
            now = self._clock()
            available, updated = self._buckets.get(key, (float(per_minute), now))
            available = min(float(per_minute), available + (now - updated) * rate)
            if available >= 1:
                self._buckets[key] = (available - 1, now)
                return None
            self._buckets[key] = (available, now)
            return max(1, math.ceil((1 - available) / rate))

    def reset(self) -> None:
        """Забыть все вёдра: нужно тестам и не используется в работе API."""
        with self._lock:
            self._buckets.clear()


# Один ограничитель на процесс API для всех маршрутов изменения данных.
write_limiter = RateLimiter()
