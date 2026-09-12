# Проверка sync v1.0 — 2026-09-12

Python 3.12.3. Существующий importer сохранён, поверх него добавлены transactional sync,
immutable snapshots/current pointer, content-addressed media cache, diff/verify/status,
exclusive lock и whole-run retry. CLI доступен через `python -m gims_open_data`.

Offline:

- `ruff check`: All checks passed.
- `ruff format --check`: 24 files already formatted.
- `mypy` strict: no issues in 14 source files.
- `pytest -q`: 83 passed. Реальные sockets запрещены в tests.

Проверены: динамический total и его изменение между/внутри snapshots, старый current
при сбоях question/media/pointer write, новый client после transient ошибки, отсутствие
POST replay, KeyboardInterrupt без restart, завершение без лишнего POST, matching/diff,
304 и новые bytes того же URL, неизвестные поля и критический schema drift, media
allowlist/redirect/размер/type/signature, checksums и повреждения index/raw/media/questions,
locks, повторная проверка immutable старого snapshot после изменения media cache.

Единственный v1 live smoke:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data sync --limit 20 --delay 1.0 --media-delay 1.0 --attempts 1 --output data/smoke-v1 --json-summary
```

Локальный candidate: `data/smoke-v1/runs/20260912T083346.641065Z/snapshot`.

| Метрика | Результат |
| --- | ---: |
| Позиции | 20 |
| Reported total | 1513 |
| Известные official UUID | 19 |
| Unresolved official UUID | 1 |
| Single / multiple | 20 / 0 |
| Вопросы с media | 20 |
| Вопросы с media ответов | 0 |
| Media URL | 20 |
| Уникальные SHA-256 objects | 18 |
| Media bytes | 2 971 444 |
| Дубли UUID / fingerprint | 0 / 0 |
| Ошибки валидации | 0 |

Загружены реальные bytes, выполнен offline verify candidate с повторным разбором raw.
Затем два уже загруженных URL проверены conditional GET: **2 × HTTP 304**, **0 downloads**;
SHA сохранены, candidate повторно verified. Результат cache-check локально:
`data/smoke-v1/cache-check.json`.

Full sync не запускался: полный обход вопросов и media оставлен пользователю, чтобы
не удерживать длительный процесс и не расходовать контекст на polling. Production
`data/state/current.json` и smoke current отсутствуют. Diff против предыдущего
успешного snapshot отсутствует; candidate содержит baseline diff с 20 added.
Реальный multiple не встретился; его обработка покрыта offline-тестами.

`scripts/sync.ps1` создан, но запуск `.ps1` запрещён текущей Windows ExecutionPolicy;
системные настройки не изменялись. Прямой Python CLI работает. Periodic команда:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data sync
```

Изменены package metadata, модели/fingerprints, client/importer/storage, README и
gitignore. Добавлены `__main__.py`, `sync.py`, `snapshot.py`, `media.py`, `diff.py`,
`lock.py`, `scripts/sync.ps1` и новые offline tests. Все generated данные в `data/`
gitignored. Commit, push и создание scheduled task не выполнялись.
