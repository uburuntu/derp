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

## Buy
buy-choose = 💰 <b>Пополнить кредиты</b>

    Подписки дают личные кредиты каждый месяц. Пакеты — разовое пополнение.
    В группах общие пакеты добавляют кредиты, которыми может пользоваться весь чат.
buy-plan-not-found = План не найден
buy-pack-not-found = Пакет не найден
buy-subscribe = Подписка { $plan }:
buy-pay-button = Оплатить { $stars }⭐/мес
buy-plan-button = Ежемесячно { $plan } — { $stars }⭐ → { $credits } кр. (выгода { $savings }){ $tag }
buy-pack-button = Разовый { $pack } — { $stars }⭐ → { $credits } кр.{ $bonus }
buy-group-pack-button = В чат { $pack } — { $stars }⭐ → { $credits } общих кр.{ $bonus }
buy-transfer-button = Перенести из личного баланса (мин. 100)
buy-chat-groups-only = Эта команда работает в группах. Для личных кредитов используй /buy.

## Transfer
transfer-prompt = Перенеси личные кредиты в общий баланс этой группы.
    Твой баланс: { $balance }
    Минимум: 100

    Ответь суммой для переноса.
transfer-min = Минимальный перевод: 100 кредитов
transfer-insufficient = Недостаточно кредитов. У тебя { $balance }.
transfer-success = { $amount } кредитов перенесено в общий баланс этой группы.
transfer-failed = Перевод не удался: { $error }
transfer-groups-only = Переводы работают только в группах
transfer-already-processed = Этот перевод уже обработан.

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

## Settings
settings-title = Настройки
settings-personality = <b>Стиль ответов:</b> { $personality }
settings-language = <b>Язык:</b> { $lang }
settings-memory-access = <b>Обновление памяти:</b> { $access }
settings-reminders-access = <b>Управление напоминаниями:</b> { $access }
settings-menu-personality = Стиль
settings-menu-language = Язык
settings-menu-permissions = Доступ
settings-menu-memory = Память
settings-menu-balance = Баланс
settings-close = Закрыть
settings-back = « Назад
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
help-footer = Для чата и поиска пиши обычным текстом. Для медиа, напоминаний, памяти и платных действий используй команды.
help-other = Другое

## Group onboarding
group-welcome = 👋 <b>Derp добавлен в этот чат.</b>

    Упомяните меня или ответьте на моё сообщение, когда нужна помощь.
    Админы могут открыть /settings для языка, памяти и доступа к напоминаниям.
