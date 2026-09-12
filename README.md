# gims-open-data — sync v1.0

Последовательный sync официального марафона МЧС: вопросы → media → offline verify →
diff → immutable snapshot → атомарный `state/current.json`. При неудачной попытке
предыдущий current сохраняется. Протокол: [docs/mchs-testing-protocol.md](docs/mchs-testing-protocol.md).

## Установка

Только Python **3.12.x**. Windows PowerShell, из корня репозитория:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Periodic update

Одна команда для полного обновления, включая media:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data sync
```

Эквивалентный wrapper, если системная ExecutionPolicy разрешает запуск `.ps1`:

```powershell
.\scripts\sync.ps1
```

Полный sync использует `questions_count` сервера. 1513 — только историческая справка
в summary, не условие успешности. Изменение total между snapshots нормально;
изменение внутри одного марафона останавливает попытку. Последний вопрос сохраняется
без дополнительного answer POST.

По умолчанию: `--delay 1.0 --media-delay 1.0 --attempts 3 --output data`.
Вопросы и media загружаются последовательно, без параллельного обхода. Для исторического
объёма одни вопросы занимают от 25 минут плюс HTTP/диск; media добавляет время.

Options:

- `--delay N`: минимум 1 секунда между прикладными запросами, включая redirects.
- `--media-delay N`: пауза между media GET, default 1 секунда, минимум 0.
- `--attempts N`: количество **новых марафонов**, default 3. После временной ошибки
  backoff 30, 60, 120… секунд, максимум 300. POST внутри instance не повторяется.
- `--json-summary`: machine-readable итог в stdout, progress/errors в stderr.
- `--verbose`: каждое сохранение; без него progress каждые 50 позиций и важные события.
- `--no-media`: явный режим snapshot только с вопросами/URL. Он может стать current,
  но `media_enabled=false`; SHA/bytes/type ресурсов остаются null. Для приложения с
  offline media используйте обычный sync. Следующий обычный sync загрузит media.
- `--limit N`: диагностический smoke, **никогда не меняет current**, даже если N >= total.
- `--output PATH`: весь локальный data root. Для произвольного PATH самостоятельно
  обеспечьте исключение generated data из git.

Безопасный smoke с media в отдельном root:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data sync --limit 20 --attempts 1 --output data/smoke-v1 --json-summary
```

Windows Task Scheduler можно настроить вручную: запускать `powershell.exe` с аргументами
`-NoProfile -File "C:\path\gims-open-data\scripts\sync.ps1"`; скрипт сам выбирает корень
репозитория и `.venv`, передаёт exit code. Выбирайте интервал с запасом на весь sync.
Задача автоматически не создаётся. Скрипт принимает дополнительные CLI arguments.
Если `.ps1` запрещены политикой Windows (так было в проверенном окружении), используйте
непосредственно `.venv\Scripts\python.exe`, аргументы `-m gims_open_data sync` и укажите
корень репозитория в поле Start in. Менять ExecutionPolicy для sync не требуется.

## Локальные данные

```text
data/
  runs/<run-id>/
    sync.log
    failure.json                  # только при ошибке
    import/<id>/                  # исходный importer: raw, checkpoint, normalized
    snapshot/                     # проверяемый candidate; smoke остаётся здесь
  snapshots/<snapshot-id>/
    manifest.json
    questions.jsonl
    summary.json
    media-manifest.json
    diff.json
    raw/0001.html, 0002.json, ...
  media/
    objects/<sha256>               # исходные media bytes, без перекодирования
    index.json                    # latest URL metadata + история URL/SHA
  state/
    current.json                   # snapshot_id, path, manifest sha256, schema_version
    sync.lock                      # существует только во время sync
```

`data/` целиком gitignored. Snapshot-файлы после promotion не редактируются.
Manifest включает SHA-256 каждого артефакта и отдельный `raw_sha256`. Проверка
выполняется до переноса candidate и после переноса в snapshots; только затем
`os.replace` атомарно меняет current pointer. Symlink не нужен. При сбое записи pointer
candidate возвращается в runs. После принудительного завершения процесса может остаться
полностью проверенный orphan snapshot: он не считается current и не выбирается автоматически.

Raw тела сохраняются перед парсингом, byte-faithful за исключением маскирования секретов.
Instance разрешён только в локальных raw/checkpoint. Cookie, CSRF, activation/start
значения и auth fields не сохраняются; HTTP headers и redirect bodies не архивируются.
Snapshot содержит raw для повторного offline-парсинга. Публикация fixtures потребует
отдельных sanitized-копий, исходный raw не редактируется.

## Media cache

Собирается объединение **всех** URL вопроса и всех вариантов. Разрешены исключительно
`https://digital.mchs.gov.ru:85/testing_bucket/...`, без credentials, traversal и
переходов на другие origins; media redirects отклоняются. Новая официальная схема URL
требует явного обновления allowlist. URL сохраняется в исходном виде.

GET ограничен timeout и 25 MiB на файл; проверяются Content-Length, Content-Type и
сигнатура bytes. Поддерживаются JPEG/PNG/GIF/WebP/BMP/SVG/AVIF, PDF, MP4, MPEG audio, Ogg.
Файлы не исполняются и не рендерятся. Для неизвестного формата snapshot не продвигается.
Временные media GET повторяются до 3 раз с backoff 2/4 секунды; затем ошибка передаётся
whole-run retry. HTTP 403, schema/content-type/validation errors не повторяют марафон.

Cache хранит SHA-256 bytes, размер, Content-Type/Length, ETag, Last-Modified,
retrieved_at/checked_at и исходный URL. При следующем sync используется conditional GET
с If-None-Match/If-Modified-Since; 304 использует проверенный объект без перезаписи.
Без validators выполняется обычный GET: недоказанная неизменность не предполагается.
Изменённые bytes того же URL создают новый SHA и `media_changed`; старые объекты и
история остаются для проверки прошлых snapshots. Автоматического удаления cache нет.

## Identity и changelog

Первый вопрос: `official_id=null`, `id_status=unresolved_initial_html`.
`stable_key=official:<uuid>` для известных ID; для первого — `fallback:<match fingerprint>`.
Это внутренний ID проекта, а не официальный UUID.

`match-fingerprint-v1`: NFC/whitespace-normalized текст, ordered ответы с их UUID/текстами
и ordered URL ресурсов. Timestamp, position и правильность в matching не участвуют.
Fallback сопоставляется только однозначно; неоднозначные ключи отражаются в diff и не
угадываются. Уникальность stable keys обязательна для успешного snapshot.

`revision-fingerprint-v2` дополнительно включает type/multiple, is_additional,
correct_answer_ids и correct flags. Изменение только правильности меняет revision.
Старый `content_fingerprint` сохранён как compatibility field и не используется для
revision diff. Media bytes отслеживаются отдельно от URL через media diff.

`diff.json`: previous/current snapshot, previous/current/delta total, added, removed,
changed с field names, moved, unchanged_count, ambiguous_keys и media changes.
Перемещение без правки содержания относится только к moved. Сравнение `--no-media`
с media snapshots отражает различие доступного media coverage; используйте одинаковый
режим для содержательного сравнения media.

## Offline-команды

```powershell
.\.venv\Scripts\python.exe -m gims_open_data status
.\.venv\Scripts\python.exe -m gims_open_data verify
.\.venv\Scripts\python.exe -m gims_open_data verify --snapshot SNAPSHOT_ID
.\.venv\Scripts\python.exe -m gims_open_data diff OLD_ID NEW_ID --json-summary
```

Все поддерживают `--output data` и `--json-summary`. Verify без сети проверяет manifest,
checksums, JSONL/model invariants, positions/total, UUID/stable-key uniqueness,
правильность ответов, fingerprints, повторный разбор raw и соответствие normalized,
media bytes и историческую запись в index. Проверка старого snapshot не требует, чтобы
latest URL cache указывал на старый SHA. Status кратко читает current/summary/last diff;
для проверки всего содержимого используйте verify.

Exit code: 0 — успех, 1 — ошибка, 2 — неверные CLI arguments, 130 — Ctrl+C.
`status` без current возвращает null; `verify` без current завершается ошибкой.

## Сбои и lock

Сетевая неопределённость answer POST означает abandon instance; повторный POST
запрещён. Whole-run retry создаёт новый client/cookie jar и новый marathon с первой
позиции. Protocol/schema и validation errors прекращают sync без повторов. Ctrl+C
также не перезапускает попытку. Diagnostics остаются в runs, secrets не пишутся в log.

Два sync в один data root исключены через exclusive-create `state/sync.lock` с PID и
временем запуска. Чужой lock автоматически не удаляется, включая stale lock. После
аварии посмотрите PID в файле и убедитесь, что тот процесс завершён; лишь затем удалите
**конкретный** `data/state/sync.lock` вручную. Не удаляйте lock работающего sync.
Автоматический resume instance не реализован, cookie на диск не записывается.

## Разработка / совместимость

```powershell
.\.venv\Scripts\python.exe -m ruff check
.\.venv\Scripts\python.exe -m ruff format --check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy
```

Mypy strict проверяет весь production package. Pytest запрещает реальные sockets;
HTTP проходит через MockTransport. Исходные 35 tests сохранены и расширены.
Неизвестные дополнительные server JSON fields допускаются, отсутствие критических
полей/неизвестный question type — protocol error после записи raw.

Low-level importer сохранён для диагностики:

```powershell
.\.venv\Scripts\python.exe -m gims_open_data.importer --limit 10 --delay 1.0
```

Старые v0.1 runs остаются диагностическими материалами, автоматически current из них
не создаётся. Первый v1 sync создаёт baseline snapshot; типы fingerprints и schema
заданы явно для будущих миграций.

Результаты проверки v1.0: [docs/sync-v1-validation.md](docs/sync-v1-validation.md).

## Reference PDF и классификация

Локальные команды `python -m gims_open_data reference parse` и
`python -m gims_open_data reference crosswalk` создают отдельные reference/derived
артефакты, сохраняя current snapshot неизменным. Live остаётся источником текущего
текста, ответов, медиа и UUID; PDF добавляет опубликованные коды и заголовки.
Установка optional dependencies, команды и правила confidence:
[docs/reference-crosswalk.md](docs/reference-crosswalk.md).
Фактический формат: [docs/pdf-question-bank-format.md](docs/pdf-question-bank-format.md).
Полный локальный прогон и 20 примеров для review:
[docs/pdf-reference-investigation.md](docs/pdf-reference-investigation.md).
