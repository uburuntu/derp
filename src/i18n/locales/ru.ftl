## General
welcome = Привет! Я Derp, твой AI-ассистент.
welcome-bonus = { $credits } бесплатных кредитов для старта!
welcome-features = <b>Что я умею</b>
    /search — Поиск в интернете
    /imagine — Генерация изображений
    /edit — Редактирование изображений
    /think — Глубокий анализ
    /tts — Озвучка текста
    /video — Генерация видео
    /remind — Напоминания

    <i>Упомяни меня или ответь на сообщение. /help — подробнее.</i>
error-generic = Что-то пошло не так. Попробуй ещё раз.

## Commands
cmd-start-desc = Запустить бота
cmd-help-desc = Показать помощь и доступные команды
cmd-settings-desc = Открыть настройки
cmd-credits-desc = Проверить баланс кредитов
cmd-buy-desc = Купить кредиты или подписку
cmd-memory-desc = Показать память чата
cmd-reminders-desc = Список активных напоминаний
cmd-info-desc = Детали генерации сообщения

## Credits
credits-balance = Твои кредиты: { $userCredits }
credits-chat-pool = Пул чата: { $chatCredits }
credits-subscription = Подписка: { $tier } (продление { $expiry })
credits-subscription-expired = Подписка: истекла
credits-added = { $credits } кредитов добавлено на твой баланс!
credits-added-chat = { $credits } кредитов добавлено в пул чата!
credits-subscribed = Подписка { $plan } оформлена! { $credits } кредитов начислено. Подписка продлевается ежемесячно.
credits-renewed = Подписка { $plan } продлена! { $credits } кредитов начислено.
credits-insufficient = Нужно { $cost } кредитов для { $tool }. Используй /buy для покупки.

## Buy
buy-choose = Выбери план или пакет кредитов:
buy-plan-not-found = План не найден
buy-pack-not-found = Пакет не найден
buy-subscribe = Подписка { $plan }:
buy-pay-button = Оплатить { $stars }⭐/мес

## Transfer
transfer-prompt = Перевод кредитов в пул этого чата.
    Твой баланс: { $balance }
    Минимум: 100
    Ответь числом для перевода:
transfer-min = Минимальный перевод: 100 кредитов
transfer-insufficient = Недостаточно кредитов. У тебя { $balance }.
transfer-success = Переведено { $amount } кредитов в пул чата.
transfer-failed = Перевод не удался: { $error }
transfer-groups-only = Переводы работают только в группах

## Tools
tool-web-search = Поиск в интернете
tool-imagine = Сгенерировать изображение по описанию
tool-edit-image = Отредактировать изображение по инструкции
tool-video = Сгенерировать короткое видео по описанию
tool-tts = Преобразовать текст в голосовое сообщение
tool-think = Глубокий анализ сложных задач
tool-remind = Создать, показать или отменить напоминания
tool-memory = Прочитать или обновить память чата
tool-get-member = Получить фото профиля участника

## Settings
settings-title = Настройки
settings-personality = Личность: { $personality }
settings-language = Язык: { $lang }
settings-memory-access = Доступ к памяти: { $access }
settings-reminders-access = Доступ к напоминаниям: { $access }
settings-close = Закрыть
settings-back = « Назад
settings-custom-sub-required = Свой промпт доступен по подписке. Используй /buy
settings-custom-prompt = Отправь свой системный промпт ответом на это сообщение. Макс. 2000 символов.
    Текущий: { $current }
settings-lang-set = Язык установлен: { $lang }
settings-lang-auto = Язык: автоопределение из сообщений
settings-balance-info = Твои кредиты: { $userCredits }
    Пул чата: { $chatCredits }
    { $subscription }

    Используй /buy для пополнения.

## Reminders
reminder-created = Напоминание создано: «{ $description }»
reminder-cancelled = Напоминание «{ $description }» отменено.
reminder-none = Нет активных напоминаний в этом чате.
reminder-limit = Достигнут лимит: { $limit } активных напоминаний на чат.
reminder-not-found = Напоминание не найдено
reminder-no-permission = Только создатель или админ может отменить это
reminder-cancel-button = Отменить

## Info
info-reply-required = Ответь на сообщение бота с /info чтобы увидеть детали генерации.
info-not-found = Сообщение не найдено в базе данных.
info-no-details = Нет данных о генерации для этого сообщения.
info-header = Информация о сообщении:

## Inline
inline-title = Спросить Derp
inline-placeholder = Думаю...
inline-error = Не удалось сгенерировать ответ. Попробуй ещё раз.

## Chat
chat-error = Не удалось сгенерировать ответ. Попробуй ещё раз.

## Memory
memory-none = Память чата пуста.
memory-updated = Память чата обновлена.
memory-cleared = Память чата очищена.
memory-admin-only = Только администраторы чата могут { $action } память.
memory-usage = Использование: /memory\_set <текст>

## Help
help-footer = Я могу делать всё это автоматически — просто опиши, что тебе нужно!

## Group onboarding
group-welcome = 👋 <b>Привет! Я Derp, ваш AI-ассистент.</b>

    Упомяните меня или ответьте на сообщение.
    Админы: /settings для настройки.
