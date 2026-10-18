"""Точка запуска: streamlit run operator_ui/main.py из любой текущей папки."""

import sys
from pathlib import Path

# streamlit run добавляет в sys.path только папку скрипта, а не корень проекта.
# Без корня импорт пакета operator_ui завершается ModuleNotFoundError.
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from operator_ui.app import main  # noqa: E402 — импорт возможен только после пути.

# Streamlit исполняет скрипт как __main__; тест импорта не отрисовывает страницу.
if __name__ == "__main__":
    main()
