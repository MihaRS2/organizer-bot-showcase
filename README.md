# Pactum Workspace

**Облачная legal-AI платформа для Pactum Group Ltd**

Pactum Workspace — рабочая среда для контрактного менеджмента и договорной работы юристов, в которой утверждённый договорный стандарт используется для подготовки проектов документов, структурированной аналитики и доказуемых договорных результатов. Платформа размещена на территории Республики Казахстан, использует обезличенные обращения к внешним LLM-сервисам (OpenAI) через серверный PII-фильтр на базе Microsoft Presidio с кастомными правилами распознавания PII Республики Казахстан.

## Состав платформы

Pactum Workspace построен на стеке открытых компонентов с надстройкой кастомизированных модулей:

| Слой | Компонент | Природа |
|------|-----------|---------|
| Chat UI | LibreChat (portal.pactum.kz) | OSS (MIT) |
| Backend / Workflow | Dify (studio.pactum.kz) | OSS (Apache 2.0-based) |
| TLS / Proxy | Caddy | OSS (Apache 2.0) |
| Реляционная БД + вектора | PostgreSQL + pgvector | OSS |
| Граф (GraphRAG) | Neo4j Community | OSS (GPL v3) |
| Полнотекст | Meilisearch | OSS (MIT) |
| Reranker | Infinity | OSS (Apache 2.0) |
| Объектное хранилище | MinIO | OSS (AGPLv3) |
| PII-движок | Microsoft Presidio + spaCy | OSS (MIT) |
| **PII-обёртка с правилами РК/СНГ** | **`pii-proxy/`** | Background IP Исполнителя |
| **Синхронизация Google Drive** | **`gdrive-sync/`** | Background IP + Foreground маппинг |
| **Мост LibreChat ↔ Dify** | **`dify-adapter/`** | Background IP Исполнителя |
| **Генератор документов** | **`doc-generator/`** | Background IP каркас + Foreground шаблоны |
| LLM-провайдер | OpenAI (GPT-5 mini, GPT-5.2) | External |

Полный список с лицензиями — см. [Приложение 3 к Договору](#правовой-режим).

## Структура репозитория

```
pactum-workspace/
├── README.md                ← этот файл
├── ARCHITECTURE.md          ← техническая архитектура
├── CHANGELOG.md             ← реестр изменений (Keep a Changelog)
├── LICENSE                  ← правовой режим (Proprietary, см. Договор)
├── .gitignore               ← исключение секретов
│
├── pii-proxy/               ← серверный PII-фильтр на базе Presidio
│   ├── proxy.py             (Background)
│   ├── stream_rewriter.py   (Background)
│   ├── audit.py             (Background)
│   ├── anonymizer_v2.py     (Background) — Presidio wrapper
│   ├── kz_recognizers.py    (Background) — 5 KZ-recognizers
│   ├── stoplist.py          (Background) — 50+ юр.терминов
│   ├── lemmatizer.py        (Background) — pymorphy3
│   ├── pactum_config.yaml   (Foreground) — конфигурация под Заказчика
│   ├── tests/               (Foreground) — тесты под Pactum-кейсы
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
│
├── gdrive-sync/             ← синхронизация Google Drive → Dify KB
│   ├── sync_core.py         (Background)
│   ├── converters.py        (Background)
│   ├── pactum_mapper.py     (Foreground) — маппинг папок Заказчика
│   ├── Dockerfile
│   └── requirements.txt
│
├── dify-adapter/            ← мост LibreChat ↔ Dify (Background)
│   ├── adapter.py
│   ├── sse_translator.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── doc-generator/           ← генератор DOCX/XLSX/PDF (docs.pactum.kz)
│   ├── app.py               (Background)
│   ├── render.py            (Background)
│   ├── converters.py        (Background)
│   ├── templates/           (Foreground) — шаблоны Pactum
│   ├── Dockerfile
│   └── requirements.txt
│
├── backup/                  ← резервное копирование с GPG-шифрованием
│   ├── backup.sh            (Background)
│   ├── restore.sh           (Background)
│   ├── install_mc.sh        (Background)
│   ├── notify.sh            (Background)
│   ├── retention.sh         (Foreground) — политика 7d+4w+12m
│   └── .env.example
│
├── workflows/               ← Dify workflow-графы
│   └── jurai-universal-graph.json   (Foreground)
│
├── librechat-config/        ← конфигурация LibreChat
│   └── librechat.yaml.example       (Foreground)
│
└── infrastructure/          ← инфраструктурные манифесты
    ├── docker-compose.override.yml  (Foreground)
    ├── caddy/Caddyfile              (Foreground)
    └── .env.example                 (Background — шаблон)
```

## Быстрый старт (только для администраторов с доступом к инфраструктуре)

> **Внимание:** этот раздел описывает только структуру развёртывания. Реальные `.env` файлы с секретами, ключами OpenAI и паролями БД в репозитории не размещаются и передаются администратору отдельно через защищённый канал.

### Предварительные требования

- Linux-хост (Ubuntu 22.04+ рекомендуется) с минимум 8 vCPU, 32GB RAM, 200GB SSD
- Docker 24+ и Docker Compose v2
- Доступ к доменам `portal.pactum.kz`, `studio.pactum.kz`, `docs.pactum.kz`
- API-ключи: OpenAI, Google Drive Service Account
- GPG-ключи для шифрования бэкапов

### Развёртывание

1. Склонируйте репозиторий и склонируйте отдельно базовые стеки LibreChat и Dify в соседние папки (`../LibreChat`, `../dify`)
2. Скопируйте все `.env.example` в `.env` и заполните секретами
3. Запустите базовые стеки: `cd ../LibreChat && docker compose up -d` и `cd ../dify && docker compose up -d`
4. Запустите кастомные сервисы: `docker compose -f infrastructure/docker-compose.override.yml up -d`
5. Настройте Caddy на хосте согласно `infrastructure/caddy/Caddyfile`
6. Импортируйте workflow в Dify Studio: `workflows/jurai-universal-graph.json`
7. Запустите первичную синхронизацию gdrive-sync, дождитесь индексации

Подробная инструкция — см. `ARCHITECTURE.md` и (после Этапа 3) `DEPLOYMENT.md`.

## Правовой режим

Pactum Workspace разрабатывается по **Договору на выполнение работ по развёртыванию, конфигурированию и кастомизации инфраструктурного программного стека от 20 марта 2026 года** (редакция v5 от 18 мая 2026 года), заключённому между:

- **Заказчик:** Частная компания «Pactum Group Ltd» в лице Генерального директора Султанбаева Азамата
- **Исполнитель:** Рамишвили Михаил Лаврентьевич

Разграничение прав интеллектуальной собственности на компоненты Программного продукта определяется **Приложением 3 к Договору** (редакция v2 от 18 мая 2026 года):

- **Foreground IP** — компоненты, созданные специально по Договору. Исключительные имущественные права принадлежат Заказчику в полном объёме (п.9.2 Договора).
- **Background IP** — компоненты, разработанные Исполнителем до Договора или в его общей профессиональной деятельности. Принадлежат Исполнителю; Заказчик получает на них безотзывную, бессрочную, безвозмездную лицензию, эксклюзивную в применении к юридическим услугам Pactum Group Ltd в Республике Казахстан (п.9.10.1 Договора).
- **Open Source компоненты** — сторонние компоненты с открытыми лицензиями (MIT, Apache 2.0, GPL и др.), регулируются условиями применимых лицензий.

В каждом модуле природа компонентов помечена в комментариях заголовка файла. Полный реестр — в Приложении 3.

## Признаки программного продукта (для целей Astana Hub, налоговых органов, аудиторов)

В соответствии с п. 10.4.4 Договора и разделом 6 Приложения 3:

- (а) Структурированный исходный код по модулям ✓
- (б) Пользовательская и техническая документация ✓ (README.md, ARCHITECTURE.md)
- (в) Реестр изменений (CHANGELOG.md в формате Keep a Changelog) ✓
- (г) Лицензионные условия (LICENSE) ✓
- (д) Семантическое версионирование с тегами релизов (v0.1.0, v0.2.0, …) ✓
- (е) Контейнеризация (Dockerfile, docker-compose) ✓
- (ж) Автоматизированные тесты (`pii-proxy/tests/`) ✓
- (з) Шаблоны конфигурации с исключением секретов (.env.example, .gitignore) ✓
- (и) Формальное разграничение IP (Приложение 3) ✓

## Контакты

- **Заказчик / Владелец продукта:** Pactum Group Ltd
- **Исполнитель / Разработчик:** Рамишвили М. Л.
- **Авторы идеи первого релиза:** Султанбаев Азамат, Мустафина Гулира, Кайрат Сержанов

## Конфиденциальность

Репозиторий приватный. Содержимое является конфиденциальной информацией Сторон Договора и не подлежит распространению, копированию или использованию третьими лицами без письменного согласия Заказчика. Раздел 11 Договора. Срок действия обязательств по конфиденциальности — 10 лет.
