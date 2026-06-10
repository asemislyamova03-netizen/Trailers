# Customer Search Input Inactive — UI Hotfix Report

**Дата:** 2026-06-10
**Симптом:** на `/orders/new` поле «Поиск клиента» видно, но нельзя ввести текст (user: Жинова Виктория, manager).
**Связь:** регрессия после deploy customer replacement (`65ea6bf`) — атрибут `disabled` в шаблонах.
**Тип:** UI hotfix (templates + tests), без DB/migrations/VIN/config.

---

## 1. Root cause

**WTForms + Jinja передавали `disabled=(выражение)` даже когда выражение ложно.**
WTForms рендерит это как HTML-атрибут `disabled="None"` или `disabled="[]"`.

В HTML5 **наличие атрибута `disabled` блокирует поле независимо от значения** (`disabled="false"`, `disabled="None"`, `disabled="[]"` — всё равно disabled).

### Production evidence (до fix, `test_client`, user id=2)

| URL | Rendered input |
|-----|----------------|
| `/orders/new` | `disabled="None"` — выражение `form.order_id and ...` → `None` |
| `/orders/83/edit` | `disabled="[]"` — `customer_change_blockers and not can_replace` → `[]` (пустой list в Jinja `and`) |

```html
<input ... disabled="None" id="customer_search" ...>
```

### Что НЕ было причиной

| Проверка | Результат |
|----------|-----------|
| CSS `pointer-events` / overlay на input-group | нет в `app.css` для этого блока |
| JS id mismatch (`#customer_search`) | id корректный |
| JS crash до `addEventListener` | не причина неактивности (disabled в HTML) |
| API `/api/customers/search?q=Zhana` | **200**, клиент 990 возвращается |
| Кнопка «Карточка» поверх input | нет (Bootstrap input-group, не overlay) |

---

## 2. Fix

Передавать `disabled` **только когда блокировка реально нужна**:

```jinja
{% set customer_search_disabled = ... %}
{{ form.customer_search(..., **({'disabled': True} if customer_search_disabled else {})) }}
```

Дополнительно: `flex-grow-1` на input в `input-group` — поле не сжимается кнопками.

### Changed files

| Файл | Изменение |
|------|-----------|
| `templates/order_form.html` | conditional `disabled`; `flex-grow-1` |
| `templates/order_header_form.html` | то же |
| `tests/test_order_customer_replacement.py` | +2 теста: `/orders/new` и edit без blockers — input без `disabled` |

**Не менялось:** `views.py`, DB, migrations, `app.css`, VIN/config.

---

## 3. Tests

```bash
python -m unittest discover -s tests -p "test_order_customer_replacement.py" -v
```

**Результат:** 14/14 OK (локально).

Новые тесты:
- `test_new_order_customer_search_is_enabled`
- `test_edit_order_customer_search_enabled_without_blockers`

---

## 4. Post-fix verification (local)

| URL | `disabled` в input |
|-----|-------------------|
| `/orders/new` | **отсутствует** |
| `/orders/80/edit` (без blockers) | **отсутствует** |

---

## 5. Deploy recommendation

**Срочный scp hotfix** (как `65ea6bf`):

1. Backup:
   ```bash
   BACKUP=/home/ubuntu/Trailers/backups/customer_search_disabled_fix_$(date +%Y%m%d_%H%M%S)
   mkdir -p $BACKUP
   cp /home/ubuntu/Trailers/templates/order_form.html $BACKUP/
   cp /home/ubuntu/Trailers/templates/order_header_form.html $BACKUP/
   ```

2. Deploy:
   - `templates/order_form.html`
   - `templates/order_header_form.html`
   - `tests/test_order_customer_replacement.py` (опционально, для server unittest)

3. Restart: `sudo systemctl restart trailers.service`

4. Smoke (manager):
   - `/orders/new` → ввести `Zhana` → datalist → hidden `customer_id` → Сохранить
   - `/orders/83/edit` → поле поиска активно

**Rollback:** восстановить файлы из `$BACKUP`, restart service.

---

## 6. Business rules (без изменений)

- Поиск клиента активен на: новый заказ, шапка, полная форма.
- Замена: поиск → datalist → Сохранить.
- «Карточка» — только реквизиты, не замена.
- Blockers на POST остаются в `views.py` (`_order_customer_change_blockers`).

---

## 7. Risks

| Риск | Уровень |
|------|---------|
| Регрессия blockers UI | низкий — `disabled=True` только при реальной блокировке |
| Scope creep | нет — только 2 templates + tests |

---

## 8. Commit

См. git log после коммита в ветке `crm-roles-production-logistics`.
