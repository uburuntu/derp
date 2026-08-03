# Derp Privacy Policy / Политика конфиденциальности Дерпа

Version and effective date: 3 August 2026

Версия и дата вступления в силу: 3 августа 2026 г.

## English

### 1. Who operates Derp

Derp is an independently operated Telegram bot. This policy describes the
production bot and the data its operator controls. The only public contact and
privacy-control channel is the bot itself: open a private chat with Derp and use
`/privacy` or `/support`. There is no separate email support channel.

Telegram and the inference providers described below process data under their
own terms and privacy policies.

### 2. Data Derp processes

Derp processes only the data needed to provide conversation, media, billing,
privacy, and abuse-prevention features:

- Telegram user and chat identifiers, names, usernames, language, chat type,
  membership context, and message or topic identifiers.
- Message text, captions, dates, reply structure, and bounded attachment
  metadata. Conversation history stores Telegram file references, not source
  media bytes or signed download URLs. Source media is downloaded on demand
  when a request needs it.
- Chat settings, admin instructions, approved shared facts, retention choices,
  personal-spending consent, and free-model privacy consent.
- Operation and inference records such as the selected provider/model, token
  counts, costs, status, retries, and content-free request identities.
- User-visible run receipts that connect a delivered Telegram answer to its
  model display name, privacy mode, context counts, token usage when reported,
  and final credit state. They do not duplicate the prompt or answer.
- Stars purchase terms, hashed invoice capabilities, Telegram charge IDs,
  subscription state, wallet entries, charges, reversals, refunds, and debt.
  Derp does not receive card details; Telegram handles Stars payments.
- Legal acceptance records and support cases. A support case stores its opaque
  reference, category, state, requester relation, one bounded user message, an
  optional exact payment receipt, and the operator's final reason or refund
  state. Support messages are excluded from conversation history and inference.
- Generated image or audio files held temporarily when delivery or retry needs
  them.

Telegram payloads such as passport, authentication, contact, location, payment,
and web-app bodies are excluded from conversation history.

### 3. Why Derp uses this data

Derp uses the data to answer requests with recent context, hydrate requested
media, enforce chat and privacy choices, operate paid features, deliver or
retry results, reconcile provider charges and Stars payments, prevent duplicate
charges, diagnose failures, and protect the service from abuse.

### 4. AI providers and privacy modes

OpenRouter is Derp's primary inference platform. Paid requests use routes
configured to deny data collection and require zero data retention where the
selected route supports it. These are provider routing controls, not a promise
beyond the providers' own terms.

Free models are optional. OpenRouter and the selected free-model provider may
retain prompts and replies under their own policies. In private chat and inline
mode, they run only after the user explicitly accepts the current disclosure.
In a group, only a current administrator can enable them for that chat after
reviewing it. Derp then posts a visible public privacy notice; administrators
should also inform members who join later. Personal and chat choices are
separate. Either can be withdrawn in the relevant settings for future requests.

Google processes direct services such as text-to-speech. Telegram processes bot
messages, files, and Stars payments. Production Logfire telemetry receives
operational identifiers, timings, counts, model/cost facts, and sanitized error
locations, but not message, prompt, response, tool-argument, payment-payload, or
binary content.

Provider terms:

- [OpenRouter Privacy Policy](https://openrouter.ai/privacy)
- [OpenRouter Terms of Service](https://openrouter.ai/terms)
- [Google Privacy Policy](https://policies.google.com/privacy)
- [Telegram Privacy Policy](https://telegram.org/privacy)

These services may process data in other countries. Their policies govern
their storage, security, and legal bases.

### 5. Retention

- Conversation history follows the chat's 7, 30, or 90-day setting; the
  default is 30 days. Attachment references use the same schedule.
- A privacy-deleted message becomes an anonymous tombstone until its original
  retention deadline so reply chronology remains coherent.
- Approved shared facts, chat policy, and settings are kept separately until an
  authorized user removes or changes them.
- Generated artifacts are private and expire after at most 6 hours. They may be
  deleted earlier after confirmed delivery or terminal failure.
- A support case's user description and operator decision text are deleted 30
  days after the case closes. Its reference, category, status, requester
  relation, timestamps, selected payment relation, and accounting records remain.
- Billing, wallet, subscription, operation, run-receipt, consent, support-case
  metadata, and reconciliation records currently have no automated expiry. They
  are retained to keep purchases and refunds idempotent, resolve cases and
  disputes, prevent double charging, and satisfy legal or abuse-prevention needs.
- Telegram, OpenRouter, model providers, Google, and Logfire control their own
  retention. Derp cannot shorten it after data has been sent to them.

### 6. Your controls

Use `/privacy` in the relevant chat to inspect retention, delete saved messages
"from my memory," and open the complete policy inside Telegram. Reply to one of
your own messages with `/forget` to remove that saved message. Destructive
actions require a confirmation that names the scope and what remains in
Telegram. Chat admins can clear a chat or topic, forget approved shared facts,
and disable future ambient capture. Disabling ambient capture purges prior
ambient-only snapshots across the whole chat; explicit mentions and replies
remain until separately cleared.

Deletion removes application-managed content and media references. It cannot
remove Telegram messages, copies made by other people, unattributed text quoted
in another message, provider-controlled logs or caches, or accounting facts
that must remain for a purchase, refund, security, or legal record.

For a privacy or data request, open Derp privately and use `/support`, then
choose `Privacy or data` and send one bounded message. For a purchase, charge,
or refund issue, choose the relevant category and select the exact Derp payment
receipt when available; a refund requires one. The operator's answer, decline
reason, or refund state appears in the same stable case message. Telegram and
`@BotSupport` cannot resolve a purchase made from an independently operated bot;
the in-bot case is the operator's support channel.

### 7. Security and changes

Derp limits stored fields, uses private generated-artifact storage, hashes
capability tokens, and keeps production telemetry content-free. No system is
perfectly secure. Material policy changes receive a new date; consent to
changed free-model terms is requested again before that mode can run.

## Русский

### 1. Кто управляет Дерпом

Дерп - независимо управляемый Telegram-бот. Эта политика описывает боевого
бота и данные под контролем его оператора. Единственный публичный канал связи и
управления приватностью - сам бот: откройте личный чат с Дерпом и отправьте
`/privacy` или `/support`. Отдельной почты нет.

Telegram и указанные ниже провайдеры ИИ обрабатывают данные по собственным
условиям и политикам.

### 2. Какие данные обрабатывает Дерп

Дерп обрабатывает только данные для диалогов, медиа, оплаты, приватности и
защиты от злоупотреблений:

- идентификаторы пользователей и чатов Telegram, имена, username, язык, тип
  чата, контекст участия, идентификаторы сообщений и тем;
- текст, подписи, даты, связи ответов и ограниченные метаданные вложений. В
  истории хранятся ссылки Telegram на файлы, но не байты исходных медиа и не
  подписанные URL. Медиа скачиваются по запросу;
- настройки чата, инструкции админов, подтверждённые общие факты, сроки
  хранения, согласия на личные кредиты и бесплатные модели;
- записи операций и инференса: провайдер, модель, токены, стоимость, статус,
  повторы и идентификаторы без содержимого;
- справки `/info`, связывающие доставленный ответ Telegram с названием модели,
  режимом приватности, объёмом контекста, доступными данными о токенах и итоговым
  списанием кредитов. Запрос и ответ в справку не копируются;
- условия покупок Stars, хеши возможностей счёта, charge ID Telegram,
  подписки, движения кредитов, списания, возвраты и долг. Дерп не получает
  данные банковской карты: Stars обрабатывает Telegram;
- записи принятия юридических условий и обращения в поддержку. В обращении
  хранятся непрозрачный номер, категория, состояние, связь с автором, одно
  короткое сообщение пользователя, выбранный чек при наличии и итоговая причина
  оператора либо состояние возврата. Текст поддержки не попадает в историю
  диалога и инференс;
- созданные изображения и аудио, временно нужные для доставки или повтора.

Паспортные, авторизационные, контактные, географические, платёжные и web-app
payload Telegram не попадают в историю диалога.

### 3. Зачем нужны данные

Дерп использует данные, чтобы отвечать с недавним контекстом, загружать нужные
медиа, соблюдать настройки и согласия, выполнять платные функции, доставлять и
повторять результаты, сверять расходы провайдеров и платежи Stars, исключать
двойные списания, разбирать сбои и защищать сервис.

### 4. Провайдеры ИИ и режимы приватности

OpenRouter - основная платформа инференса. Платные запросы идут по маршрутам с
запретом сбора данных и требованием нулевого хранения, если выбранный маршрут
это поддерживает. Это настройка маршрута, а не обещание сверх условий
провайдера.

OpenRouter и провайдер бесплатной модели могут хранить запросы и ответы по своим
правилам. В личном чате и инлайн-режиме бесплатная модель запускается только
после явного принятия текущего предупреждения. В группе её может включить только
действующий администратор после такого же предупреждения. Дерп публикует об этом
заметное сообщение в группе; админам следует отдельно предупредить новых
участников. Личный выбор и настройка чата независимы. Оба решения можно
отозвать для будущих запросов.

Google может обрабатывать прямые функции, например синтез речи. Telegram
обрабатывает сообщения, файлы и платежи Stars. В боевой Logfire-телеметрии есть
технические идентификаторы, длительности, счётчики, модель, стоимость и
очищенные места ошибок, но нет сообщений, промптов, ответов, аргументов
инструментов, платёжных
payload и бинарных данных.

Политики провайдеров:

- [OpenRouter Privacy Policy](https://openrouter.ai/privacy)
- [OpenRouter Terms of Service](https://openrouter.ai/terms)
- [Google Privacy Policy](https://policies.google.com/privacy)
- [Telegram Privacy Policy](https://telegram.org/privacy)

Эти сервисы могут обрабатывать данные в других странах. Сроки, безопасность и
правовые основания определяют их политики.

### 5. Сроки хранения

- История хранится 7, 30 или 90 дней по настройке чата. По умолчанию - 30 дней.
  Ссылки на вложения живут столько же.
- Удалённое по запросу сообщение становится анонимной меткой до исходной даты
  удаления, чтобы не ломать цепочку ответов.
- Подтверждённые общие факты, политика и настройки чата хранятся отдельно, пока
  уполномоченный пользователь их не удалит или не изменит.
- Созданные файлы закрыты и хранятся не более 6 часов. После успешной доставки
  или окончательного сбоя они могут удалиться раньше.
- Сообщение пользователя и ответ оператора удаляются через 30 дней после
  закрытия обращения. Номер, категория, состояние, связь с пользователем,
  даты, выбранный платёж и учётные записи остаются.
- У записей оплаты, кредитов, подписок, операций, справок `/info`, согласий,
  метаданных поддержки и сверки сейчас нет автоматического срока удаления. Они
  нужны для идемпотентных покупок и возвратов, обращений и споров, защиты от
  двойных списаний, злоупотреблений и выполнения закона.
- Telegram, OpenRouter, провайдеры моделей, Google и Logfire сами определяют
  свои сроки. После отправки данных Дерп не может их сократить.

### 6. Ваши настройки

Отправьте `/privacy` в нужном чате, чтобы увидеть срок хранения, удалить
сохранённые сообщения из памяти Дерпа и открыть полную политику в Telegram.
Ответьте `/forget` на своё сообщение, чтобы удалить его сохранённую копию.
Перед удалением Дерп показывает область действия и напоминает, что останется в
Telegram. Админы чата могут очистить чат или тему, забыть общие факты и
отключить будущий фоновый контекст. При отключении удаляются фоновые снимки из
всего чата; явные упоминания и ответы остаются до отдельной очистки.

Удаление стирает содержимое и ссылки под управлением Дерпа. Оно не удаляет
сообщения Telegram, чужие копии, текст в сообщениях других людей, логи и кеши
провайдеров и учётные факты, нужные для оплаты, возврата, безопасности или
закона.

Для запроса о данных откройте личный чат с Дерпом, отправьте `/support`, выберите
`Приватность или данные` и отправьте одно короткое сообщение. Для оплаты или
возврата сначала выберите категорию и, если он есть, точный чек Дерпа; для
возврата чек обязателен. Ответ, причина отказа или состояние возврата появится
в том же сообщении обращения. Telegram и `@BotSupport` не решают вопросы
покупок у независимо управляемого бота; канал оператора - обращение внутри
Дерпа.

### 7. Безопасность и изменения

Дерп ограничивает набор полей, хранит созданные файлы в закрытом хранилище,
хеширует токены возможностей и не отправляет содержимое в боевую телеметрию.
Абсолютной защиты не существует. При существенных изменениях обновляется дата;
для новых условий бесплатных моделей Дерп снова запросит согласие.
