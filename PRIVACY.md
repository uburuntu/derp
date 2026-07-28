# Derp Privacy Policy / Политика конфиденциальности Дерпа

Version and effective date: 28 July 2026

Версия и дата вступления в силу: 28 июля 2026 г.

## English

### 1. Who operates Derp

Derp is an independently operated Telegram bot. This policy describes the
production bot and the data its operator controls. The only public contact and
privacy-control channel is the bot itself: open a private chat with Derp and use
`/privacy`. There is no separate email support channel.

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
- Stars purchase terms, hashed invoice capabilities, Telegram charge IDs,
  subscription state, wallet entries, charges, reversals, refunds, and debt.
  Derp does not receive card details; Telegram handles Stars payments.
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

OpenRouter is Derp's primary inference platform. Paid requests and all group
requests use routes configured to deny data collection and require zero data
retention where the selected route supports it. These are provider routing
controls, not a promise beyond the providers' own terms.

Free models are optional. They run only after explicit consent in a private
chat or inline mode. OpenRouter and the selected free-model provider may retain
prompts and replies under their own policies. Free non-ZDR models never run for
groups. Consent can be withdrawn from `Model privacy` in Derp's private
settings; withdrawal affects future requests.

Google may process requests for direct services such as text-to-speech and when
the operator activates the documented provider rollback. Telegram processes
bot messages, files, and Stars payments. Production Logfire telemetry receives
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
- Billing, wallet, subscription, operation, consent, and reconciliation records
  currently have no automated expiry. They are retained to keep purchases and
  refunds idempotent, resolve disputes, prevent double charging, and satisfy
  legal or abuse-prevention needs.
- Telegram, OpenRouter, model providers, Google, and Logfire control their own
  retention. Derp cannot shorten it after data has been sent to them.

### 6. Your controls

Use `/privacy` in the relevant chat to inspect retention, delete your saved
messages there, and open this policy. Reply to one of your own messages with
`/forget` to remove that saved message. Chat admins can clear a chat or topic,
forget approved shared facts, and disable future ambient capture. Disabling
ambient capture also purges prior ambient-only snapshots; explicit interactions
remain until separately cleared.

Deletion removes application-managed content and media references. It cannot
remove Telegram messages, copies made by other people, unattributed text quoted
in another message, provider-controlled logs or caches, or accounting facts
that must remain for a purchase, refund, security, or legal record.

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
`/privacy`. Отдельной почты поддержки нет.

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
- условия покупок Stars, хеши возможностей счёта, charge ID Telegram,
  подписки, движения кредитов, списания, возвраты и долг. Дерп не получает
  данные банковской карты: Stars обрабатывает Telegram;
- созданные изображения и аудио, временно нужные для доставки или повтора.

Паспортные, авторизационные, контактные, географические, платёжные и web-app
payload Telegram не попадают в историю диалога.

### 3. Зачем нужны данные

Дерп использует данные, чтобы отвечать с недавним контекстом, загружать нужные
медиа, соблюдать настройки и согласия, выполнять платные функции, доставлять и
повторять результаты, сверять расходы провайдеров и платежи Stars, исключать
двойные списания, разбирать сбои и защищать сервис.

### 4. Провайдеры ИИ и режимы приватности

OpenRouter - основная платформа инференса. Платные запросы и запросы из групп
идут по маршрутам с запретом сбора данных и требованием нулевого хранения, если
выбранный маршрут это поддерживает. Это настройки маршрута, а не обещание
сверх условий провайдера.

Бесплатные модели включаются только после явного согласия в личном чате или
inline-режиме. OpenRouter и провайдер бесплатной модели могут хранить запросы и
ответы по своим правилам. В группах бесплатные non-ZDR модели не запускаются.
Согласие можно отозвать в разделе `Приватность моделей` личных настроек Дерпа;
это действует на будущие запросы.

Google может обрабатывать прямые функции, например синтез речи, и запросы при
включённом оператором резервном маршруте. Telegram обрабатывает сообщения,
файлы и платежи Stars. В боевой Logfire-телеметрии есть технические
идентификаторы, длительности, счётчики, модель, стоимость и очищенные места
ошибок, но нет сообщений, промптов, ответов, аргументов инструментов, платёжных
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
- У записей оплаты, кошельков, подписок, операций, согласий и сверки сейчас нет
  автоматического срока удаления. Они нужны для идемпотентных покупок и
  возвратов, споров, защиты от двойных списаний, злоупотреблений и выполнения
  закона.
- Telegram, OpenRouter, провайдеры моделей, Google и Logfire сами определяют
  свои сроки. После отправки данных Дерп не может их сократить.

### 6. Ваши настройки

Отправьте `/privacy` в нужном чате, чтобы увидеть срок хранения, удалить свои
сохранённые сообщения и открыть эту политику. Ответьте `/forget` на своё
сообщение, чтобы удалить его сохранённую копию. Админы чата могут очистить чат
или тему, забыть общие факты и отключить будущий фоновый контекст. При
отключении удаляются прошлые фоновые снимки; явные обращения остаются до
отдельной очистки.

Удаление стирает содержимое и ссылки под управлением Дерпа. Оно не удаляет
сообщения Telegram, чужие копии, текст в сообщениях других людей, логи и кеши
провайдеров и учётные факты, нужные для оплаты, возврата, безопасности или
закона.

### 7. Безопасность и изменения

Дерп ограничивает набор полей, хранит созданные файлы в закрытом хранилище,
хеширует токены возможностей и не отправляет содержимое в боевую телеметрию.
Абсолютной защиты не существует. При существенных изменениях обновляется дата;
для новых условий бесплатных моделей Дерп снова запросит согласие.
