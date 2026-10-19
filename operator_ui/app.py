"""Русский рабочий стол оператора без прямого доступа к PostgreSQL или модели.

Все изменения находятся внутри форм с явной кнопкой отправки. Обычный rerun
делает только GET; длинные операции и решения никогда не повторяются клиентом.
Сотрудник входит своим API-токеном: API определяет по нему имя, роль и автора
решения. Интерфейс лишь скрывает недоступные роли формы; права проверяет API.
"""

import streamlit as st

from operator_ui.client import MAX_ID, PAGE_SIZE, APIClient, APIError

TICKET_STATUSES = {
    "Все": None,
    "Открыто": "open",
    "В работе": "in_progress",
    "Ожидает клиента": "waiting_customer",
    "Решено": "resolved",
    "Закрыто": "closed",
}
# Закрытое обращение сервер не анализирует и не доставляет по нему ответ.
CLOSED_STATUSES = {"resolved", "closed"}
PRIORITIES = {
    "Низкий": "low",
    "Средний": "medium",
    "Высокий": "high",
    "Критический": "critical",
}
ROLES = {"Оператор": "operator", "Рецензент": "reviewer", "Администратор": "admin"}
# Почему у обращения удалены тексты (поле anonymized_reason API).
ANONYMIZATION_REASONS = {
    "customer_request": "по запросу клиента",
    "retention": "по сроку хранения",
}
ROLE_NAMES = {value: label for label, value in ROLES.items()}
# Решение по предложению агента принимают рецензент и администратор.
REVIEW_ROLES = {"reviewer", "admin"}
TOKEN_HELP = (
    "Токен выдаёт администратор: python -m app.tokens issue или "
    "POST /api/users/{id}/tokens. Токен хранится только в этом сеансе браузера."
)


def make_client(token: str) -> APIClient:
    """Создать HTTP-клиент; отдельная фабрика позволяет проверять UI без сети."""
    return APIClient(token)


def show_error(error: APIError) -> None:
    """Показать безопасное сообщение и ссылку на сохранённый неудачный запуск."""
    st.error(str(error))
    if error.run_id:
        st.info(f"Запуск №{error.run_id} сохранён. Откройте его в истории обращения.")
    if error.request_id:
        st.caption(f"Код запроса для администратора: {error.request_id}")


def read_status(client: APIClient, area: str) -> dict:
    """Не останавливать весь интерфейс, если один статус временно недоступен."""
    try:
        return client.status(area)
    except APIError as error:
        show_error(error)
        return {}


def page_offset(key: str, signature: tuple = ()) -> int:
    """Начинать список с первой страницы при изменении его фильтров."""
    if st.session_state.get(f"{key}_filters") != signature:
        st.session_state[f"{key}_filters"] = signature
        st.session_state[f"{key}_offset"] = 0
    return st.session_state.get(f"{key}_offset", 0)


def pagination(key: str, offset: int, count: int) -> None:
    """Переключать ограниченные страницы, не загружая таблицу целиком."""
    left, center, right = st.columns([1, 3, 1])
    center.caption(f"Страница {offset // PAGE_SIZE + 1} · записей на странице: {count}")
    if left.button("← Назад", key=f"{key}_previous", disabled=offset == 0):
        st.session_state[f"{key}_offset"] = max(0, offset - PAGE_SIZE)
        st.rerun()
    if right.button(
        "Далее →",
        key=f"{key}_next",
        disabled=count < PAGE_SIZE or offset > MAX_ID - PAGE_SIZE,
    ):
        st.session_state[f"{key}_offset"] = offset + PAGE_SIZE
        st.rerun()


def id_input(label: str, *, key: str, optional: bool = False) -> int:
    """Принимать точный десятичный ID без ограничения точности JavaScript-чисел."""
    text = st.text_input(label, value="0" if optional else "1", key=key, max_chars=19)
    if not text.isascii() or not text.isdigit() or not 0 <= int(text) <= MAX_ID:
        raise APIError(f"Поле «{label}»: введите целое число от 1 до {MAX_ID}.")
    value = int(text)
    if value == 0 and not optional:
        raise APIError(f"Поле «{label}»: идентификатор должен быть больше нуля.")
    return value


def confirmed_provider(status: dict, *, key: str, purpose: str) -> bool:
    """Потребовать отдельное разрешение оператора перед возможным платным вызовом."""
    if not status.get("configured"):
        st.warning("Провайдер не настроен. Обратитесь к администратору.")
        return False
    provider = status.get("provider", status.get("embedding_provider"))
    if provider == "mock":
        st.caption("Демонстрационный режим: платный API не вызывается.")
        return True
    if provider != "openai":
        st.warning("Не удалось определить провайдера. Действие недоступно.")
        return False
    st.warning(f"{purpose} использует OpenAI и может расходовать средства аккаунта.")
    return st.checkbox("Подтверждаю вызов реального OpenAI API", key=key)


def render_run(run: dict) -> None:
    """Показать сохранённый итог, источники, трассу и ошибку выбранного запуска."""
    st.subheader(f"Запуск №{run['id']}")
    st.caption(
        f"Статус: {run['status']} · провайдер: {run['provider']} · "
        f"шагов: {run['steps']} · модель: {run.get('model') or '—'}"
    )
    if run.get("error_message"):
        st.error(run["error_message"])
    result = run.get("result")
    if result:
        st.text(result["summary"])
        st.caption(f"Категория: {result['category']}")
        st.text_area(
            "Черновик ответа", result["draft_reply"], height=180, disabled=True
        )
        sources = result.get("sources", [])
        st.write("Источники базы знаний")
        st.text(", ".join(sources) if sources else "Источники не использованы.")
    if run.get("suggestion"):
        suggestion = run["suggestion"]
        st.info(
            f"Предложение №{suggestion['id']}: {suggestion['status']}. "
            "Решение принимает сотрудник."
        )
    with st.expander("Вызовы инструментов и расход токенов"):
        st.json({"tool_trace": run.get("tool_trace", []), "usage": run.get("usage")})


def record_selector(
    client: APIClient, resource: str, label: str, *, key: str
) -> int | None:
    """Выбрать запись по названию на одной странице, сохраняя точность BIGINT.

    Email или код различают одинаковые названия. Скрытый выбор сбрасывается;
    каждый GET читает только текущую страницу и не кеширует удалённые записи.
    """
    offset = page_offset(key)
    rows = client.list_records(resource, offset=offset)
    detail = "email" if resource == "customers" else "code"
    choices = {str(row["id"]): f"{row['name']} — {row[detail]}" for row in rows}
    choice_key = f"{key}_choice"
    if st.session_state.get(choice_key) not in choices:
        st.session_state[choice_key] = None
    selected = st.selectbox(
        label,
        list(choices),
        index=None,
        format_func=lambda value: choices[value],
        key=choice_key,
        placeholder=f"Выберите: {label.lower()}",
        disabled=not choices,
    )
    pagination(key, offset, len(rows))
    if not rows:
        records = "клиентов" if resource == "customers" else "продуктов"
        st.info(
            f"На этой странице нет {records}. Вернитесь на предыдущую страницу "
            "или добавьте запись в разделе «Справочники»."
        )
    return int(selected) if selected is not None else None


def create_ticket(client: APIClient) -> None:
    """Выбрать клиента и продукт по названию и явно отправить форму обращения."""
    with st.expander("Создать обращение"):
        st.caption(
            "Сначала выберите клиента и продукт, затем заполните тему и описание. "
            "Поиск в списке работает на текущей странице; для остальных записей "
            "используйте кнопки «Назад» и «Далее»."
        )
        try:
            customer = record_selector(
                client, "customers", "Клиент", key="create_ticket_customer"
            )
            product = record_selector(
                client, "products", "Продукт", key="create_ticket_product"
            )
        except APIError as error:
            show_error(error)
            return
        with st.form("create_ticket", clear_on_submit=False):
            subject = st.text_input("Тема", max_chars=300)
            description = st.text_area("Описание проблемы", max_chars=10_000)
            priority = st.selectbox("Приоритет", list(PRIORITIES), index=1)
            submitted = st.form_submit_button(
                "Создать обращение", disabled=customer is None or product is None
            )
        if submitted:
            if customer is None or product is None:
                st.error("Выберите клиента и продукт.")
                return
            if not subject.strip() or not description.strip():
                st.error("Заполните тему и описание проблемы.")
                return
            ticket = client.create_record(
                "tickets",
                {
                    "customer_id": customer,
                    "product_id": product,
                    "subject": subject,
                    "description": description,
                    "priority": PRIORITIES[priority],
                },
            )
            st.session_state["selected_ticket"] = str(ticket["id"])
            st.success(f"Создано обращение №{ticket['id']}.")


def erase_customer_form(client: APIClient, staff: dict) -> None:
    """Обезличить клиента по его запросу: только администратор и с подтверждением.

    Сервер требует текущий email клиента; флажок защищает от случайного нажатия.
    После успеха показываются только счётчики — данные клиента уже удалены.
    """
    if staff["role"] != "admin":
        st.caption("Обезличить клиента по его запросу может администратор.")
        return
    with st.expander("Обезличить клиента по запросу"):
        st.warning(
            "Действие необратимо: имя, email, компания и тексты всех обращений "
            "клиента, решений и запусков агента будут удалены. Лицензии и "
            "статистика сохранятся. Резервные копии, сделанные раньше, данные "
            "по-прежнему содержат."
        )
        with st.form("erase_customer"):
            customer_id = id_input("ID клиента для обезличивания", key="erase_id")
            confirm_email = st.text_input(
                "Текущий email клиента для подтверждения", max_chars=320
            )
            understood = st.checkbox("Понимаю, что восстановить данные нельзя")
            submitted = st.form_submit_button("Обезличить клиента")
        if not submitted:
            return
        if not understood or not confirm_email.strip():
            st.error("Введите email клиента и подтвердите, что действие необратимо.")
            return
        report = client.erase_customer(customer_id, confirm_email.strip())
        st.success(
            f"Клиент №{report['customer_id']} обезличен. Обращений: "
            f"{report['tickets']}, сообщений: {report['messages']}, запусков: "
            f"{report['agent_runs']}, предложений: {report['suggestions']}. "
            f"Лицензий сохранено: {report['licenses_kept']}."
        )


def ticket_card(client: APIClient, ticket_id: int, llm: dict, knowledge: dict) -> None:
    """Объединить данные карточки и историю, загружая только нужные страницы."""
    ticket = client.get_record("tickets", ticket_id)
    st.subheader(f"Обращение №{ticket_id}")
    st.text(ticket["subject"])
    st.caption(f"Статус: {ticket['status']} · приоритет: {ticket['priority']}")
    st.text(ticket["description"])
    anonymized = ticket.get("anonymized_at") is not None
    if anonymized:
        reason = ANONYMIZATION_REASONS.get(ticket.get("anonymized_reason"), "")
        st.info(
            f"Обращение обезличено {reason}: тексты удалены. Статус и анализ "
            "больше не меняются; история запусков осталась без текстов."
        )
    else:
        with st.form(f"ticket_status_{ticket_id}"):
            statuses = {
                label: value for label, value in TICKET_STATUSES.items() if value
            }
            current = list(statuses.values()).index(ticket["status"])
            selected_status = st.selectbox(
                "Новый статус", list(statuses), index=current
            )
            update_status = st.form_submit_button("Изменить статус")
        if update_status:
            client.update_ticket_status(ticket_id, statuses[selected_status])
            st.session_state["ticket_notice"] = "Статус обращения изменён."
            st.rerun()
    if notice := st.session_state.pop("ticket_notice", None):
        st.success(notice)
    with st.expander("Клиент, продукт и лицензии"):
        customer = client.get_record("customers", ticket["customer_id"])
        product = client.get_record("products", ticket["product_id"])
        st.json({"клиент": customer, "продукт": product})
        offset = page_offset("ticket_licenses", (ticket_id,))
        licenses = client.list_records(
            "licenses",
            offset=offset,
            customer_id=customer["id"],
            product_id=product["id"],
        )
        st.dataframe(
            [{**item, "license_key": "••••••••"} for item in licenses],
            hide_index=True,
            width="stretch",
        )
        pagination("ticket_licenses", offset, len(licenses))
    providers = {llm.get("provider"), knowledge.get("embedding_provider")}
    known_providers = providers.issubset({"mock", "openai"})
    if knowledge.get("index") != "ready":
        st.warning("Индекс базы знаний не готов: источники могут быть недоступны.")
    permitted = confirmed_provider(
        {
            "configured": llm.get("configured", False) and known_providers,
            "provider": "openai" if "openai" in providers else "mock",
        },
        key=f"analyze_confirm_{ticket_id}",
        purpose="Модель или поиск базы знаний при анализе",
    )
    closed = ticket["status"] in CLOSED_STATUSES or anonymized
    if closed and not anonymized:
        st.info(
            "Обращение закрыто: новый анализ недоступен. Чтобы проанализировать "
            "его снова, сначала смените статус."
        )
    with st.form(f"analyze_ticket_{ticket_id}"):
        license_key = st.text_input(
            "Ключ лицензии для проверки (необязательно)", type="password", max_chars=255
        )
        analyze = st.form_submit_button(
            "Запустить анализ", disabled=not permitted or closed
        )
    if analyze and permitted and not closed:
        with st.spinner("Агент изучает обращение…"):
            run = client.analyze(ticket_id, license_key.strip() or None)
        st.session_state[f"selected_run_{ticket_id}"] = str(run["id"])
        st.success(f"Анализ №{run['id']} завершён; предложение ожидает решения.")
    st.subheader("История запусков")
    offset = page_offset("ticket_runs", (ticket_id,))
    runs = client.list_records("agent-runs", offset=offset, ticket_id=ticket_id)
    st.dataframe(
        [
            {
                key: row.get(key)
                for key in ("id", "status", "provider", "started_at", "steps")
            }
            for row in runs
        ],
        hide_index=True,
        width="stretch",
    )
    pagination("ticket_runs", offset, len(runs))
    if runs:
        selected_key = f"selected_run_{ticket_id}"
        if selected_key not in st.session_state:
            st.session_state[selected_key] = str(runs[0]["id"])
        run_id = id_input("ID запуска для просмотра", key=selected_key)
        run = client.get_record("agent-runs", run_id)
        if run["ticket_id"] != ticket_id:
            st.warning("Этот запуск относится к другому обращению.")
        else:
            render_run(run)
    st.subheader("Сообщения обращения")
    offset = page_offset("ticket_messages", (ticket_id,))
    messages = client.messages(ticket_id, offset=offset)
    if not messages:
        st.caption("Сообщений пока нет. Одобрение создаёт учебное сообщение в БД.")
    for message in messages:
        st.caption(
            f"№{message['id']} · {message['author_type']} · {message['created_at']}"
        )
        st.text(message["content"])
    pagination("ticket_messages", offset, len(messages))


def tickets_page(client: APIClient, llm: dict, knowledge: dict) -> None:
    """Показать фильтруемый список и выбранное обращение без изменений при чтении."""
    st.header("Обращения")
    create_ticket(client)
    left, center, right = st.columns(3)
    with left:
        customer_id = id_input(
            "Фильтр: ID клиента (0 — все)", key="filter_customer", optional=True
        )
    with center:
        product_id = id_input(
            "Фильтр: ID продукта (0 — все)", key="filter_product", optional=True
        )
    with right:
        status = TICKET_STATUSES[
            st.selectbox("Статус обращения", list(TICKET_STATUSES))
        ]
    offset = page_offset("tickets", (customer_id, product_id, status))
    tickets = client.list_records(
        "tickets",
        offset=offset,
        customer_id=customer_id or None,
        product_id=product_id or None,
        status=status,
    )
    st.dataframe(tickets, hide_index=True, width="stretch")
    pagination("tickets", offset, len(tickets))
    if not tickets:
        st.info(
            "На этой странице нет обращений. Создайте обращение или измените фильтры."
        )
    selected = st.text_input(
        "Открыть обращение по ID", key="selected_ticket", max_chars=19
    )
    if selected:
        if (
            not selected.isascii()
            or not selected.isdigit()
            or not 1 <= int(selected) <= MAX_ID
        ):
            st.error("Введите положительный целочисленный ID обращения.")
        else:
            ticket_card(client, int(selected), llm, knowledge)


def review_form(client: APIClient, suggestion: dict, staff: dict) -> None:
    """Отправить решение сотрудника только после явного подтверждения формы.

    Автора решения тело не содержит: API берёт его из токена сеанса.
    """
    st.text(suggestion["content"])
    if suggestion["status"] != "pending":
        st.info(f"Решение уже принято: {suggestion['status']}.")
        st.json(
            {
                key: suggestion.get(key)
                for key in (
                    "reviewed_by",
                    "reviewed_at",
                    "final_content",
                    "review_comment",
                    "delivered_message_id",
                )
            }
        )
        return
    if staff["role"] not in REVIEW_ROLES:
        st.info(
            "Решение по предложению принимает рецензент или администратор. "
            f"Ваша роль — {ROLE_NAMES.get(staff['role'], staff['role'])}."
        )
        return
    st.caption(
        "Одобрение сохранит сообщение от вашего имени и статус waiting_customer. "
        "Внешнее письмо клиенту не отправляется. Предложение по закрытому "
        "обращению (resolved или closed) можно только отклонить."
    )
    with st.form(f"review_{suggestion['id']}"):
        decision = st.radio("Решение", ["Одобрить", "Отклонить"], horizontal=True)
        content = st.text_area(
            "Итоговый ответ при одобрении",
            suggestion["content"],
            max_chars=6000,
            height=180,
        )
        comment = st.text_area(
            "Комментарий (обязателен при отклонении)", max_chars=2000
        )
        confirmed = st.checkbox("Я проверил текст и подтверждаю решение")
        submitted = st.form_submit_button("Сохранить решение")
    if submitted:
        if not confirmed:
            st.error("Подтвердите, что проверили предложение и выбранное решение.")
            return
        if decision == "Отклонить" and not comment.strip():
            st.error("Укажите причину отклонения.")
            return
        if decision == "Одобрить" and not content.strip():
            st.error("Итоговый ответ не может быть пустым.")
            return
        payload = {
            "decision": "approve" if decision == "Одобрить" else "reject",
            "final_content": content.strip() if decision == "Одобрить" else None,
            "comment": comment.strip() or None,
        }
        client.review(suggestion["id"], payload)
        st.session_state["review_notice"] = (
            f"Решение по предложению №{suggestion['id']} сохранено."
        )
        st.rerun()


def queue_page(client: APIClient, staff: dict) -> None:
    """Показать очередь и решение по выбранному предложению с ограниченной страницей."""
    st.header("Очередь проверки")
    if notice := st.session_state.pop("review_notice", None):
        st.success(notice)
    statuses = {
        "Ожидают проверки": "pending",
        "Одобрены": "approved",
        "Отклонены": "rejected",
    }
    status = statuses[st.selectbox("Состояние предложения", list(statuses))]
    ticket_id = id_input(
        "Фильтр: ID обращения (0 — все)", key="queue_ticket", optional=True
    )
    offset = page_offset("queue", (status, ticket_id))
    suggestions = client.list_records(
        "suggestions", offset=offset, status=status, ticket_id=ticket_id or None
    )
    st.dataframe(suggestions, hide_index=True, width="stretch")
    pagination("queue", offset, len(suggestions))
    if not suggestions:
        st.info("На этой странице нет предложений с выбранным состоянием.")
    selected = st.text_input(
        "Открыть предложение по ID", key="selected_suggestion", max_chars=19
    )
    if selected:
        if (
            not selected.isascii()
            or not selected.isdigit()
            or not 1 <= int(selected) <= MAX_ID
        ):
            st.error("Введите положительный целочисленный ID предложения.")
            return
        suggestion = client.get_record("suggestions", int(selected))
        st.subheader(
            f"Предложение №{suggestion['id']} · обращение №{suggestion['ticket_id']}"
        )
        review_form(client, suggestion, staff)


def knowledge_page(client: APIClient, knowledge: dict) -> None:
    """Искать статьи только при отправке формы, сохраняя результат между rerun."""
    st.header("База знаний")
    st.caption(
        f"Индекс: {knowledge.get('index', 'недоступен')} · "
        f"статей: {knowledge.get('documents', '—')}"
    )
    permitted = confirmed_provider(knowledge, key="search_confirm", purpose="Поиск")
    with st.form("knowledge_search"):
        query = st.text_input("Что найти?", max_chars=300)
        product = st.text_input("Код продукта (необязательно)", max_chars=64)
        limit = st.slider("Число фрагментов", 1, 10, 5)
        submitted = st.form_submit_button("Найти статьи", disabled=not permitted)
    if submitted and permitted:
        if len(query.strip()) < 3:
            st.error("Введите минимум три символа для поиска.")
        else:
            st.session_state["knowledge_result"] = client.search(
                query.strip(), product.strip() or None, limit
            )
    result = st.session_state.get("knowledge_result")
    if result:
        st.caption(f"Результат запроса: {result['query']}")
        if not result["results"]:
            st.info("Подходящих фрагментов не найдено.")
        for hit in result["results"]:
            with st.expander(f"{hit['document_key']} · {hit['title']}"):
                st.caption(f"Раздел: {hit['section']} · сходство: {hit['score']:.3f}")
                st.text(hit["text"])
                st.text(f"Источник: {hit['source_path']}")


def catalog_page(client: APIClient, staff: dict) -> None:
    """Создать минимальные справочники без загрузки всей БД или доступа к SQL.

    Сотрудников создаёт только администратор; токен новому сотруднику он
    выдаёт отдельно (CLI или API), в интерфейсе секреты не показываются.
    """
    st.header("Справочники")
    resources = {
        "Клиенты": "customers",
        "Продукты": "products",
        "Лицензии": "licenses",
        "Сотрудники": "users",
    }
    resource = resources[st.selectbox("Справочник", list(resources))]
    offset = page_offset("catalog", (resource,))
    rows = client.list_records(resource, offset=offset)
    if resource == "licenses":
        rows = [{**item, "license_key": "••••••••"} for item in rows]
    st.dataframe(rows, hide_index=True, width="stretch")
    pagination("catalog", offset, len(rows))
    if resource == "customers":
        erase_customer_form(client, staff)
    if resource == "users" and staff["role"] != "admin":
        st.info("Создавать сотрудников и выдавать им токены может администратор.")
        return
    with st.form(f"create_{resource}"):
        payload = {}
        if resource in {"customers", "products", "users"}:
            payload["name"] = st.text_input("Название / имя", max_chars=200)
        if resource in {"customers", "users"}:
            payload["email"] = st.text_input("Email", max_chars=320)
        if resource == "customers":
            payload["company"] = (
                st.text_input("Компания (необязательно)", max_chars=200) or None
            )
        if resource == "products":
            payload["code"] = st.text_input("Код продукта", max_chars=64)
            payload["description"] = (
                st.text_area("Описание продукта (необязательно)", max_chars=10_000)
                or None
            )
        if resource == "licenses":
            payload["customer_id"] = id_input(
                "ID клиента лицензии", key="license_customer"
            )
            payload["product_id"] = id_input(
                "ID продукта лицензии", key="license_product"
            )
            payload["license_key"] = st.text_input(
                "Ключ новой лицензии", type="password", max_chars=255
            )
            st.caption("Создаётся активная бессрочная лицензия с текущей датой выдачи.")
        if resource == "users":
            payload["role"] = ROLES[st.selectbox("Роль", list(ROLES))]
            st.caption("Токен для входа выдаёт администратор после создания.")
        submitted = st.form_submit_button("Создать запись")
    if submitted:
        required = {"name", "email", "code", "license_key"}.intersection(payload)
        if any(not payload[key].strip() for key in required):
            st.error("Заполните обязательные поля.")
            return
        record = client.create_record(resource, payload)
        st.success(
            f"Запись №{record['id']} создана. Обновите страницу списка для просмотра."
        )


def dashboard_page(client: APIClient) -> None:
    """Показать полные агрегаты API и явно отметить отсутствие статистики."""
    st.header("Обзор поддержки")
    values = client.dashboard()
    first = st.columns(3)
    first[0].metric("Всего обращений", values["tickets_total"])
    first[1].metric("Открытых обращений", values["open_tickets"])
    first[2].metric("Высокий приоритет", values["high_priority_tickets"])
    second = st.columns(3)
    second[0].metric("Проанализировано обращений", values["analyzed_tickets"])
    average = values["average_analysis_seconds"]
    second[1].metric(
        "Среднее время анализа", "Нет данных" if average is None else f"{average:.1f} с"
    )
    rate = values["approval_rate"]
    second[2].metric("Доля одобрений", "Нет данных" if rate is None else f"{rate:.1%}")
    st.caption(
        "Открытые — только status=open. Высокий приоритет — high/critical среди "
        "незакрытых. Среднее время — только завершённые запуски. Доля одобрений — "
        "approved / (approved + rejected); ожидающие предложения в неё не входят."
    )
    st.write("Ожидают проверки:", values["pending_suggestions"])
    st.write(
        "Завершённые / неудачные запуски:",
        values["completed_runs"],
        "/",
        values["failed_runs"],
    )


def sidebar_token() -> str:
    """Показать поле токена; значение живёт только в сеансе этого браузера."""
    with st.sidebar:
        st.header("Рабочая панель")
        return st.text_input(
            "Токен сотрудника",
            type="password",
            key="api_token",
            max_chars=64,
            help=TOKEN_HELP,
        ).strip()


def main() -> None:
    """Открыть рабочий стол; обычное обновление страницы выполняет только чтение."""
    st.set_page_config(
        page_title="AI Support · оператор", page_icon="💬", layout="wide"
    )
    st.title("AI Support")
    st.caption("Рабочее место оператора технической поддержки")
    token = sidebar_token()
    if not token:
        st.info(
            "Введите свой токен сотрудника на боковой панели. "
            "Без токена API не показывает данные и не принимает решения."
        )
        st.caption(TOKEN_HELP)
        return
    try:
        with make_client(token) as client:
            staff = client.me()
            with st.sidebar:
                st.caption(
                    f"Вы: {staff['name']} · "
                    f"{ROLE_NAMES.get(staff['role'], staff['role'])}"
                )
                area = st.radio(
                    "Раздел",
                    [
                        "Обзор",
                        "Обращения",
                        "Очередь проверки",
                        "База знаний",
                        "Справочники",
                    ],
                )
                st.button("Обновить данные")
                llm = read_status(client, "llm")
                knowledge = read_status(client, "knowledge")
                st.caption(
                    f"Модель: {llm.get('provider', 'недоступна')} · "
                    f"{llm.get('model') or '—'}"
                )
                st.caption(
                    "UI использует только HTTP API. "
                    "Ключ OpenAI в интерфейс не вводится."
                )
            if area == "Обзор":
                dashboard_page(client)
            elif area == "Обращения":
                tickets_page(client, llm, knowledge)
            elif area == "Очередь проверки":
                queue_page(client, staff)
            elif area == "База знаний":
                knowledge_page(client, knowledge)
            else:
                catalog_page(client, staff)
    except APIError as error:
        show_error(error)
