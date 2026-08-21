---
batch: idea
source: D:\Local\Git\transcriber\IDEA.md
created: 2026-08-21
status: approved
---

# Intake: Local meeting transcription desktop app (MVP)

## Source

`D:\Local\Git\transcriber\IDEA.md` is a single-author idea document (in Russian) written by the operator describing a personal, local-first tool for handling meeting recordings: a desktop UI plus a Python transcription backend, backed by an Obsidian-style "vault" folder of recordings. The document has four sections — `# Проблема` (problem), `# Ключевая функциональность` (key functionality, incl. the vault naming convention), `# Планы` (long-term plans), and `## MVP` (the scope to build now). Per operator notes, only the `## MVP` section (plus the vault-structure requirements from `# Ключевая функциональность`, which the MVP presupposes) is in scope; everything else under `# Планы` is deferred. The document is plain markdown with no images or embedded media, so no extraction was needed and `<batch-dir>/media/` was not created; the original file is the reading copy. Task text below is quoted verbatim in Russian, with an English gloss added underneath each quote (marked as gloss — the Russian wording is authoritative for the spec analyst). The repo is otherwise empty except for a gitignored read-only clone of the open-source project **vexa** at `D:\Local\Git\transcriber\vexa\`, which the document raises as a possible reuse source for the transcription service.

## Features

### [p] F1: Meeting vault layout and naming convention  (slug: meeting-vault-layout)

**Task text** (verbatim from the source):

> ## Папка с рекордингами, аля Vault в обсидиане
>
> C точки зрения пользователя:
> Я загружаю мит рекординг. Если он назван неправильно - он попадает в папку unsorted, с сотрировкой по дате добавления.
>
> Naming convention notation:
> ```
> <Project code> - <date> - <Title>.<ext>
> # Examples
> # ELS - 260812 - Security issue.mp4
> # GIS - 260724 - Client demo.mp4
> ```
>
> And in file system we can organize it like
>
> - root
>     - PROJ1
>         - folder with filename with trimmed first part of project, like 260812 - Security issue
>             - source.mp4
>             - summary.md
>             - transcript.json
>     - PROJ2
>     - ...
>
> Но на самом деле под каждый митинг нужно создавать отдельную папку, поскольку внутри мы будем потом рожать саммари, транскрипты и другие файлы.

> Additional in-scope context from `# Ключевая функциональность`:
> - 2 папки. 1 папка приложения, где хранятся скрипты, модели, все что нужно для работы но о чем пользователю знать не нужно.

*English gloss (not authoritative):* A recordings folder acting like an Obsidian **Vault**. The user drops in a meeting recording; if it is named incorrectly it lands in an `unsorted` folder, sorted by date added. Filename convention `<Project code> - <date> - <Title>.<ext>`. On disk: `root / <PROJECT CODE> / <date> - <Title> / {source.mp4, summary.md, transcript.json}` — i.e. the per-meeting folder name is the original filename with the project-code prefix trimmed. Every meeting gets its own folder because summaries, transcripts and other artifacts will be generated inside it later. There are two folders overall: the vault, and a separate application folder holding scripts, models and internals the user never needs to see.

**Attachments**: none.

---

### [p] F2: Python transcription microservice  (slug: transcription-service)

**Task text** (verbatim from the source):

> - Часть транскрибации должна быть как микросервис, при этом мы должны поддержать логгирование в sqlite и использовать litellm для унификации взаимодействия с LLM. Я локально буду использовать whisper large v3, но я хочу легко менять провайдеров и также вести учет временных затрат и цен на транскрипцию в случае с облачными провайдерами.
>     - И тут как раз вопрос. Я знаю про опенсорс решение с похожим назначением, но все же не совсем - vexa. Я склонировал репозиторий вексы сюда,
> можешь его посмотреть, может ли он быть полезен? Если мы можем какую-то часть вексы использовать - было бы круто, но если только маленький кусочек - можно просто
> вдохновиться и написать свое с нуля, и так и так норм.

> Related framing from `# Планы` (in-scope part only):
> - Я хочу сделать rust десктоп приложение которое будет работать как UI, и иметь интеграцию с python для непосредственной работы с транскрибацией. Но я хочу проверить для начала гипотезу и сделать MVP именно с частью транскрибации.

*English gloss (not authoritative):* The transcription part must be a microservice, with **sqlite logging** and **litellm** used to unify LLM/provider interaction. Locally the operator will run **whisper large v3**, but provider swapping must be easy, and time cost and monetary cost per transcription must be tracked for cloud providers. Open question raised by the operator: whether the open-source project **vexa** (cloned read-only at `D:\Local\Git\transcriber\vexa\`) can be partially reused for this service, or whether it should just serve as inspiration for a from-scratch implementation — both outcomes are acceptable to the operator. The desktop app is Rust/UI and integrates with Python for the actual transcription; the MVP exists primarily to validate the transcription hypothesis.

**Attachments**: none. **Research reference**: `D:\Local\Git\transcriber\vexa\` — read-only clone of the vexa open-source meeting-transcription project, to be evaluated for partial reuse during spec/plan for this feature (not evaluated at intake time, per operator instruction).

---

### [p] F3: Tauri 2 desktop app with drag-and-drop processing  (slug: tauri-desktop-app)

**Task text** (verbatim from the source):

> - Отдельно на расте - десктоп приложение таури 2 и раст. Используем реакт. на этапе MVP - возможность перетащить файл транскрипта в приложение и запуск его в обработку.

> Related framing from `# Ключевая функциональность`:
> - Десктопное приложение. Кроссплатформа, но начать можно с винды.

*English gloss (not authoritative):* A separate Rust-side desktop application built on **Tauri 2** + Rust, using **React** for the frontend. At the MVP stage the capability is: drag a recording file into the app and kick off its processing. The app is meant to be cross-platform eventually, but Windows first.

**Attachments**: none.

---

### [p] F4: Windows installer and build system  (slug: windows-installer-build)

**Task text** (verbatim from the source):

> - Как часть MVP - система сборки этого всего дела. Я хочу установщик на выходе под винду для MVP, который установит приложеньку, сможет скачать whisper3 в папку проекта, и я смогу выбрать
> корневую папку для митингов.

*English gloss (not authoritative):* Part of the MVP is the build system for the whole thing. The output must be a Windows installer that installs the app, is able to download whisper large v3 into the project/app folder, and lets the user choose the root folder for meetings.

**Attachments**: none.

## Unassigned content

- `# Проблема` (lines 1–4) — background motivation: the operator works across several projects, records meetings, currently dumps them into a `Meetings` folder on Windows, and needs a transcript (primarily) and a summary for each. This is framing context for the whole batch rather than a separate feature; it should be carried into each feature's spec as user context.
- Summary/`summary.md` generation — the problem statement says "Мне нужно по каждому делать транскрипт и саммари" and the vault layout lists `summary.md` inside each meeting folder, but the `## MVP` section only commits to the transcription part ("сделать MVP именно с частью транскрибации"). Treated as a filename/placeholder in F1's folder layout, not as an MVP capability. Needs an operator ruling at the split gate.
- Cross-platform support beyond Windows — stated as a direction ("Кроссплатформа, но начать можно с винды"), explicitly deferred by "начать можно с винды"; MVP targets Windows only (F4).

### Excluded by operator notes

Per operator instruction, everything under `# Планы` that is not part of `## MVP` is future work and gets no feature:

- "Классификация спикеров. Для проекта можно задать спикеров, и потом после нескольких ручных разметок будущие записи маркируются автоматически." — speaker classification/diarization; post-MVP.
- "Возможность записывать экран, и звук" — screen and audio recording capture; post-MVP.
- "Тикетов как action items со скриншотами" — action-item tickets with screenshots; post-MVP.
- "База знаний rag/LLM wiki" — RAG / LLM knowledge-base wiki; post-MVP.
- "Автовычленение топиков" (incl. the sub-note "Что такое топик - надо подумать. Требование/Задача/Эпик/Хотелка/Идея. Короче хочется все фразы группировать по топикам, а классифицировать топики можно позже как отдельная задача.") — automatic topic extraction and topic taxonomy; post-MVP, and explicitly still undefined by the author.

## Decisions log

- 2026-08-21 — Scope limited to the `## MVP` section; `# Планы` items deferred → per operator notes at intake.
- 2026-08-21 — Split approved as proposed: 4 features (F1 → F2 → F3 → F4) → operator at split gate.
- 2026-08-21 — `summary.md` is a reserved filename/placeholder in the vault layout only; no summary generation in MVP → operator at split gate.
- 2026-08-21 — F3 drag-and-drop target is the meeting **recording** file (mp4/audio), not a transcript file; "файл транскрипта" in IDEA.md was a misnomer → operator at split gate.
