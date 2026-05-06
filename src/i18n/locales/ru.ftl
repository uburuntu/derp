## General
welcome = Derp готов.
welcome-bonus = { $credits } бесплатных кредитов для старта!
welcome-features = <b>Попробуй</b>
    /search — ответы из интернета
    /imagine — изображения
    /edit — правка изображений
    /think — глубокий анализ
    /tts — голосовые сообщения
    /video — короткие видео
    /remind — напоминания

    <i>Пиши обычным текстом, упомяни меня в группе или ответь на моё сообщение.</i>
error-generic = Что-то пошло не так. Попробуй ещё раз.

## Commands
cmd-start-desc = Запустить бота
cmd-help-desc = Показать помощь и доступные команды
cmd-settings-desc = Открыть настройки
cmd-credits-desc = Проверить баланс кредитов
cmd-buy-desc = Купить кредиты или подписку
cmd-donate-desc = Поддержать Derp звёздами
cmd-memory-desc = Показать память чата
cmd-reminders-desc = Список активных напоминаний
cmd-info-desc = Детали генерации сообщения

## Credits
credits-title = Баланс
credits-balance = Личные кредиты: { $userCredits }
credits-chat-pool = Кредиты чата: { $chatCredits }
credits-subscription = План: { $tier } (продление { $expiry })
credits-subscription-expired = План: истёк
credits-added = { $credits } кредитов добавлено на твой баланс!
credits-added-chat = { $credits } общих кредитов добавлено в этот чат!
credits-subscribed = Подписка { $plan } оформлена! { $credits } кредитов начислено. Подписка продлевается ежемесячно.
credits-renewed = Подписка { $plan } продлена! { $credits } кредитов начислено.
credits-insufficient = Нужно { $cost } кредитов для { $tool }. Используй /buy для покупки.
chat-free-quota-reached = Бесплатный лимит чата на сегодня закончился. Используй /buy для стандартных ответов или попробуй завтра.
payment-sub-new = ✅ <b>Подписка { $plan } оформлена</b>

    { $credits } кредитов добавлено. Подписка продлевается ежемесячно.
payment-sub-renewed = ✅ <b>{ $plan } продлена</b>

    { $credits } кредитов добавлено.
payment-sub-new-debt = ✅ <b>Подписка { $plan } оформлена</b>

    { $debt } кредитов погасили долг после возврата.
    { $credits } кредитов добавлено. Подписка продлевается ежемесячно.
payment-sub-renewed-debt = ✅ <b>{ $plan } продлена</b>

    { $debt } кредитов погасили долг после возврата.
    { $credits } кредитов добавлено.
payment-pack-user = ✅ <b>Кредиты добавлены</b>

    { $credits } кредитов добавлено на твой баланс.
payment-pack-user-debt = ✅ <b>Долг погашен</b>

    { $debt } кредитов погасили личный долг после возврата.
    { $credits } кредитов добавлено на твой баланс.
payment-pack-chat = ✅ <b>Кредиты группы добавлены</b>

    { $credits } общих кредитов добавлено в этот чат.
payment-pack-chat-debt = ✅ <b>Долг группы погашен</b>

    { $debt } кредитов погасили долг группы после возврата.
    { $credits } общих кредитов добавлено.
refund-target-user = твоего личного баланса
refund-target-chat = общего пула этой группы
refund-processed = ↩️ <b>Возврат обработан</b>

    Возвращено { $recovered }/{ $original } кредитов с { $target }.
refund-processed-debt = ↩️ <b>Возврат обработан</b>

    Возвращено { $recovered }/{ $original } кредитов с { $target }.

    { $debt } кредитов уже были потрачены; долг нужно погасить перед платным использованием.

## Buy
buy-choose = 💰 <b>Пополнить кредиты</b>

    Подписки дают личные кредиты каждый месяц. Пакеты — разовое пополнение.
    Стандартные ответы в чате стоят 2 кредита; у платных инструментов своя цена.
    В группах общие пакеты добавляют кредиты, которыми может пользоваться весь чат.
buy-choose-personal = 💰 <b>Личные кредиты</b>

    Личные кредиты принадлежат тебе и работают в любом чате.
    Стандартные ответы стоят 2 кредита; у платных инструментов своя цена.
buy-choose-group = 💰 <b>Общий пул группы</b>

    Кредиты группы общие для этого чата; потратить их может любой участник.
    В этом чате Derp сначала тратит кредиты группы, потом личные кредиты.
buy-choose-subscriptions = 💰 <b>Ежемесячные подписки</b>

    Подписка начисляет личные кредиты сейчас и при каждом продлении.
    Они не общие для групп, а стандартный чат всё равно тратит кредиты.
buy-target-personal = Мои личные кредиты
buy-target-group = Общий пул этой группы
buy-target-subscriptions = Ежемесячные подписки
buy-plan-not-found = План не найден
buy-pack-not-found = Пакет не найден
buy-subscribe = Подписка { $plan }:
buy-pay-button = Оплатить { $stars }⭐/мес
buy-plan-button = Ежемесячно { $plan } — { $stars }⭐ → { $credits } кр. (выгода { $savings }){ $tag }
buy-pack-button = Разовый { $pack } — { $stars }⭐ → { $credits } кр.{ $bonus }
buy-group-pack-button = В чат { $pack } — { $stars }⭐ → { $credits } общих кр.{ $bonus }
buy-chat-groups-only = Эта команда работает в группах. Для личных кредитов используй /buy.
buy-invoice-error = Не удалось создать ссылку на оплату. Попробуй ещё раз.

## Donations
donate-choose = ⭐ <b>Поддержать Derp</b>

    Донат не покупает кредиты; он помогает оплачивать хостинг и разработку.
    Выбери сумму ниже или отправь <code>/donate 50</code>.
donate-invalid = Некорректная сумма доната
donate-thanks = ⭐ <b>Спасибо!</b>

    Получено звёзд: { $stars }.
donate-error = Не удалось обработать донат. Попробуй ещё раз.

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
tool-category-search = Поиск и источники
tool-category-reasoning = Анализ
tool-category-media = Медиа
tool-category-utility = Инструменты
tool-cost-free = бесплатно
tool-cost-free-daily = { $freeDaily } бесплатно на пользователя/чат/день
tool-cost-credits = { $credits } кр.
tool-cost-credits-with-quota = { $credits } кр., { $freeDaily } бесплатно на пользователя/чат/день
tool-disabled = { $tool } выключен в твоих настройках. Включить можно через /settings.
tool-confirm-source-chat = из общего пула группы
tool-confirm-source-user = с твоего личного баланса
tool-confirm-message = ⚠️ <b>Подтверди списание</b>

    <code>{ $tool }</code> спишет { $cost } кр. { $source }.
    Остаток после запуска: { $remaining }.

    <i>Запускай, когда результат стоит этой цены.</i>
tool-confirm-run = Запустить
tool-confirm-cancel = Отмена
tool-confirm-running-alert = Запускаю { $tool }...
tool-confirm-running = ⏳ <b>Выполняю</b>

    <code>{ $tool }</code> уже в работе.
tool-confirm-done = ✅ <b>Готово</b>

    <code>{ $tool }</code> завершён.
tool-confirm-failed = ⚠️ <b>Не удалось завершить запрос</b>

    Если инструмент не выполнился, кредиты не удержаны.
tool-confirm-cancelled = Отменено.
tool-confirm-expired = Подтверждение устарело. Отправь команду заново.
tool-confirm-invalid = Сохранённый запрос больше не подходит. Отправь команду заново.
tool-confirm-owner-only = Подтвердить или отменить может только тот, кто отправил запрос.

## Settings
settings-title = Настройки
settings-personality = <b>Стиль ответов:</b> { $personality }
settings-user-style = <b>Моя детализация:</b> { $style }
settings-disabled-tools = <b>Выключенные инструменты:</b> { $count }
settings-language = <b>Язык:</b> { $lang }
settings-memory-access = <b>Обновление памяти:</b> { $access }
settings-reminders-access = <b>Управление напоминаниями:</b> { $access }
settings-menu-personality = Стиль
settings-menu-language = Язык
settings-menu-user-style = Мой стиль
settings-menu-user-style-cycle = Детализация: { $style }
settings-menu-tools = Инструменты
settings-menu-permissions = Доступ
settings-menu-memory = Память
settings-menu-balance = Баланс
settings-close = Закрыть
settings-back = « Назад
settings-tools-none = нет
settings-tool-state-on = Вкл
settings-tool-state-off = Выкл
settings-tool-enabled = { $tool } включен
settings-tool-disabled = { $tool } выключен
settings-admin-only = Только администраторы чата могут менять настройки.
settings-personality-default = Обычный
settings-personality-professional = Деловой
settings-personality-casual = Неформальный
settings-personality-creative = Креативный
settings-personality-custom = Свой
settings-personality-custom-button = Свои инструкции
settings-personality-set = Стиль ответов: { $personality }
settings-custom-sub-required = Свои инструкции доступны по подписке. Используй /buy
settings-custom-current-none = (нет)
settings-custom-placeholder = Свои инструкции
settings-custom-prompt = Отправь инструкцию, которой Derp должен следовать в этом чате. До { $max } символов. Напиши /cancel, чтобы остановить настройку.
    Текущая: { $current }
settings-custom-too-long = Инструкция слишком длинная. Максимум: { $max } символов.
settings-custom-saved = Свои инструкции сохранены.
settings-custom-cancelled = Настройка своих инструкций отменена.
settings-user-style-concise = Кратко
settings-user-style-balanced = Сбалансировано
settings-user-style-detailed = Подробно
settings-user-style-set = Детализация: { $style }
settings-user-instructions-button = Мои инструкции
settings-user-instructions-placeholder = Личные инструкции
settings-user-instructions-prompt = Отправь личные инструкции, которые Derp должен помнить во всех чатах. До { $max } символов.
    Сейчас: { $current }
    Напиши /cancel, чтобы остановить настройку.
settings-user-instructions-saved = Личные инструкции сохранены.
settings-language-en = Английский
settings-language-ru = Русский
settings-language-auto = Авто
settings-lang-set = Язык установлен: { $lang }
settings-lang-auto = Язык: автоопределение из сообщений
settings-access-admins = только админы
settings-access-everyone = все участники
settings-menu-memory-access = Обновление памяти: { $access }
settings-menu-reminders-access = Напоминания: { $access }
settings-memory-access-set = Обновление памяти: { $access }
settings-reminders-access-set = Управление напоминаниями: { $access }
settings-memory-view-button = Показать память
settings-memory-clear-button = Очистить память
settings-memory-title = Память чата
settings-memory-none = Память чата пуста
settings-memory-cleared = Память очищена
settings-balance-subscription = План: { $tier }
settings-balance-info = Личные кредиты: { $userCredits }
    Кредиты чата: { $chatCredits }
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
inline-placeholder = Продолжай писать вопрос.
inline-wait = Сделай короткую паузу и повтори этот запрос.
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
help-footer = Для чата и поиска пиши обычным текстом. Для медиа, напоминаний, памяти и платных действий используй команды.
help-other = Другое

## Group onboarding
group-welcome = 👋 <b>Derp добавлен в этот чат.</b>

    Упомяните меня или ответьте на моё сообщение, когда нужна помощь.
    Админы могут открыть /settings для языка, памяти и доступа к напоминаниям.
