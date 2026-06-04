# Implementation Plan: VIN confirmation UI

Date: 2026-06-04
Status: implemented
Linked research brief: `docs/ai/research/2026-06-04-vin-confirmation-ui.md`

## Objective

Сделать понятным шаг подтверждения VIN после присвоения без расширения прав логиста.

## Non-goals

- Не давать логисту право подтверждать VIN.
- Не менять route permissions.
- Не менять модели, миграции, документы, реализации и перемещения.

## File touch list

| File | Change | Reason | Risk |
|---|---|---|---|
| `templates/vin_registry_list.html` | Добавить действие/подсказку для `assigned`. | Понятный следующий шаг в реестре. | low |
| `templates/vin_registry_detail.html` | Добавить alert для логиста при `assigned`. | Не оставлять пустой экран без действия. | low |

## Step-by-step plan

### Step 1

- Files: `templates/vin_registry_list.html`
- Action: добавить колонку `Действие`; для `assigned` менеджеру/директору показать POST `Подтвердить`, логисту подсказку.
- Verification: diff и compile.

### Step 2

- Files: `templates/vin_registry_detail.html`
- Action: добавить блок-пояснение для логиста, если `row.status == 'assigned'`.
- Verification: diff и compile.

## Test plan

```bash
python -m compileall .
git -c core.quotepath=false diff --ignore-cr-at-eol -- templates/vin_registry_list.html templates/vin_registry_detail.html
```

## Acceptance criteria

- [x] Логист видит, что назначенный VIN подтверждает менеджер/директор.
- [x] Менеджер/директор видят кнопку подтверждения там, где уже имеют право.
- [x] `@role_required` не изменён.

## Rollback plan

Вернуть изменения в двух шаблонах и удалить документы текущего плана/research.

## Approval

Approved by user: yes
Date: 2026-06-04
Notes: Пользователь подтвердил безопасный UI-фикс без расширения прав логиста.
