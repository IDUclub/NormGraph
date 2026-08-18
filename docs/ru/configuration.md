# Конфигурация

Все настройки — в `src/common/config/app_config.py` (pydantic-settings). Любую можно переопределить
переменной окружения с префиксом **`NG_`** или через `.env`. Переменные окружения имеют приоритет над
`.env`. Значения по умолчанию рассчитаны на контур IDU, поэтому приложение запускается без конфигурации.

- `.env.example` — минимальная сетевая обвязка (адреса, которые обычно нужно менять).
- `.env.full.example` — все переменные, для справки.

## Neo4j

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_NEO4J_URI` | `bolt://localhost:7687` | Bolt URI |
| `NG_NEO4J_USER` | `neo4j` | пользователь |
| `NG_NEO4J_PASSWORD` | `normgraph` | пароль (маскируется в `/system/settings`) |
| `NG_NEO4J_DATABASE` | `neo4j` | база |
| `NG_RESTRICTION_VECTOR_INDEX` | `restriction_embedding` | имя векторного индекса |
| `NG_CLAUSE_VECTOR_INDEX` | `clause_embedding` | имя векторного индекса |
| `NG_ENTITY_VECTOR_INDEX` | `entity_embedding` | имя векторного индекса |
| `NG_KIND_VECTOR_INDEX` | `kind_embedding` | имя векторного индекса |

## IDU_DVD

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_DVD_BASE_URL` | `http://localhost:8100` | базовый URL IDU_DVD (прод публикует на 8100) |
| `NG_DVD_TIMEOUT` | `120.0` | таймаут HTTP (с) |
| `NG_DVD_SEARCH_FALLBACK` | `true` | при пустом результате графа — опереться на `/search` IDU_DVD |

## Провайдер LLM (извлечение ограничений)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_LLM_PROVIDER` | `openai_compatible` | `openai_compatible` или `ollama` |
| `NG_LLM_BASE_URL` | `http://localhost:11434/v1` | корень OpenAI-совместимого API (`…/v1`) |
| `NG_LLM_MODEL` | `qwen2.5:7b-instruct` | id чат-модели |
| `NG_LLM_API_KEY` | — | bearer-токен, если нужен endpoint'у (маскируется) |
| `NG_LLM_TEMPERATURE` | `0.0` | температура |
| `NG_LLM_MAX_TOKENS` | `4096` | максимум выходных токенов |
| `NG_LLM_TIMEOUT` | `600.0` | таймаут HTTP (с) |
| `NG_OLLAMA_BASE` | `http://localhost:11434` | корень нативного Ollama (когда `NG_LLM_PROVIDER=ollama`) |

По умолчанию — OpenAI-совместимый провайдер, поэтому любой из vLLM / LM Studio / llama.cpp / шима
`/v1` Ollama подключается указанием `NG_LLM_BASE_URL`. langextract работает через этот провайдер.

## Провайдер эмбеддингов (векторизатор)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_EMBEDDINGS_PROVIDER` | `openai_compatible` | `openai_compatible` (Giga) или `ollama` (напр. bge-m3) |
| `NG_EMBEDDINGS_URL` | `http://localhost:8001` | корень сервиса эмбеддингов (`POST /v1/embeddings`) |
| `NG_EMBEDDINGS_MODEL` | `ai-sage/Giga-Embeddings-instruct` | id модели |
| `NG_EMBEDDINGS_API_KEY` | — | bearer-токен, если нужен (маскируется) |
| `NG_EMBEDDINGS_QUERY_PROMPT` | Instruct-промпт | инструкция для запроса (Giga асимметрична) |
| `NG_EMBEDDINGS_TIMEOUT` | `600.0` | таймаут HTTP (с) |
| `NG_VECTOR_SIZE` | `2048` | **должен** совпадать с моделью (giga = 2048, bge-m3 = 1024) и векторными индексами |
| `NG_EMBED_BATCH` | `32` | размер батча эмбеддингов |

> Смена `NG_VECTOR_SIZE` требует пересоздания векторных индексов (удалить их или взять свежую базу
> Neo4j) — у векторного индекса Neo4j фиксированная размерность. Если настроенный индекс уже
> существует с другой размерностью, сервис завершит запуск с явной ошибкой; сохранённые эмбеддинги
> и индексы нужно пересобрать вместе.

## Конвейер извлечения

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_EXTRACTION_PASSES` | `1` | число последовательных проходов langextract по пункту (полнота vs стоимость) |
| `NG_ENTITY_MERGE_THRESHOLD` | `0.90` | косинус ≥ этого сливает сущность с существующей канонической |
| `NG_KIND_MATCH_THRESHOLD` | `0.88` | косинус ≥ этого сопоставляет вид; ниже → новый вид `pending` |
| `NG_EXTRACT_CONCURRENCY` | `8` | максимум пунктов, обрабатываемых LLM одновременно; запись в граф остаётся упорядоченной |

## Поиск / обход

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_SEARCH_LIMIT` | `10` | лимит результатов по умолчанию |
| `NG_MAX_TRAVERSAL_DEPTH` | `3` | ограничение глубины раскрытия окрестности графа |

## Kafka-синхронизация

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_KAFKA_BOOTSTRAP_SERVERS` | — (выключено) | брокер(ы); пусто/не задано = консюмер выключен |
| `NG_KAFKA_SCHEMA_REGISTRY_URL` | реестр контура | Avro Schema Registry |
| `NG_KAFKA_CLIENT_ID` | `normgraph` | client id |
| `NG_KAFKA_GROUP_ID` | `normgraph-sync` | группа консюмера (стабильная → offset'ы хранятся по группе) |
| `NG_KAFKA_TOPIC` | `document.events` | топик жизненного цикла IDU_DVD |
| `NG_KAFKA_AUTO_OFFSET_RESET` | `earliest` | политика offset на первом запуске (см. ниже) |
| `NG_RECONCILE_ON_STARTUP` | `true` | запускать catch-up reconcile при старте |

**Offset'ы и «только необработанные события».** При стабильном `NG_KAFKA_GROUP_ID` Kafka хранит
последний закоммиченный offset группы, поэтому при рестарте консюмер продолжает с него и обрабатывает
только ещё не обработанные события (otteroad коммитит после успешного хендлера — at-least-once).
`AUTO_OFFSET_RESET` важен только на самом первом старте (нет закоммиченного offset) или при истечении
offset'ов:

- `earliest` → один раз пройти весь backlog, затем только новые события (рекомендуется, если нужны
  ранее не обработанные события, включая те, что были до первого запуска консюмера);
- `latest` → пропустить backlog, только новые события с этого момента.

Guard идемпотентности (см. [pipeline](pipeline.md)) делает реплеи дешёвыми: неизменённый, уже
извлечённый документ пропускает переизвлечение.

## Логирование

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `NG_LOG_DIR` | `./logs` | каталог логов |
| `NG_LOG_FILE` | `app.log` | JSON-лог (отдаётся через `GET /system/logs`) |
| `NG_LOG_LEVEL` | `INFO` | уровень логирования |

## Пример `.env` (контур IDU)

```dotenv
NG_DVD_BASE_URL=http://10.32.11.17:8100
NG_LLM_BASE_URL=http://a.dgx:11434/v1
NG_LLM_MODEL=gpt-oss:20b
NG_EMBEDDINGS_URL=http://a.dgx:8010
NG_KAFKA_BOOTSTRAP_SERVERS=10.32.1.65:9092,10.32.1.65:9093,10.32.1.65:9094
NG_KAFKA_SCHEMA_REGISTRY_URL=http://10.32.1.65:8081
NG_KAFKA_AUTO_OFFSET_RESET=earliest
```
