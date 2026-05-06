## General
welcome = Derp is ready.
welcome-bonus = { $credits } free credits to get started!
welcome-features = <b>Try</b>
    /search — web answers
    /imagine — images
    /edit — image edits
    /think — deeper reasoning
    /tts — voice messages
    /video — short videos
    /remind — reminders

    <i>Ask normally, mention me in a group, or reply to one of my messages.</i>
error-generic = Something went wrong. Please try again.

## Commands
cmd-start-desc = Start the bot
cmd-help-desc = Show help and available commands
cmd-settings-desc = Open settings menu
cmd-credits-desc = Check credit balance
cmd-buy-desc = Buy credits or subscribe
cmd-donate-desc = Support Derp with Stars
cmd-memory-desc = View chat memory
cmd-reminders-desc = List active reminders
cmd-info-desc = Show message generation details

## Credits
credits-title = Balance
credits-balance = Personal credits: { $userCredits }
credits-chat-pool = Group credits: { $chatCredits }
credits-subscription = Plan: { $tier } (renews { $expiry })
credits-subscription-expired = Plan: expired
credits-added = { $credits } credits added to your balance!
credits-added-chat = { $credits } shared credits added to this chat!
credits-subscribed = Subscribed to { $plan }! { $credits } credits added. Your subscription renews monthly.
credits-renewed = { $plan } subscription renewed! { $credits } credits added.
credits-insufficient = Need { $cost } credits for { $tool }. Use /buy to get credits.
credits-refund-debt-title = Refund debt
credits-personal-debt = Personal debt: { $credits } credits
credits-group-debt = Group debt: { $credits } credits
credits-debt-hint = New payments settle debt first; paid usage is blocked until it is settled.
credits-spend-order-group = Spend order here: group pool first, then personal credits.
credits-personal-private-hint = Personal balance details are shown only in private chat. Use /credits in DM.
chat-free-quota-reached = Free chat limit reached for today. Use /buy for standard replies, or try again tomorrow.
payment-sub-new = ✅ <b>Subscribed to { $plan }</b>

    { $credits } credits added. Subscription renews monthly.
payment-sub-renewed = ✅ <b>{ $plan } renewed</b>

    { $credits } credits added.
payment-sub-new-debt = ✅ <b>Subscribed to { $plan }</b>

    { $debt } credits settled refund debt.
    { $credits } credits added. Subscription renews monthly.
payment-sub-renewed-debt = ✅ <b>{ $plan } renewed</b>

    { $debt } credits settled refund debt.
    { $credits } credits added.
payment-pack-user = ✅ <b>Credits added</b>

    { $credits } credits added to your balance.
payment-pack-user-debt = ✅ <b>Credits settled</b>

    { $debt } credits settled personal refund debt.
    { $credits } credits added to your balance.
payment-pack-chat = ✅ <b>Group credits added</b>

    { $credits } shared credits added to this chat.
payment-pack-chat-debt = ✅ <b>Group credits settled</b>

    { $debt } credits settled group refund debt.
    { $credits } shared credits added.
refund-target-user = your personal balance
refund-target-chat = this group's shared pool
refund-processed = ↩️ <b>Refund processed</b>

    Recovered { $recovered }/{ $original } credits from { $target }.
refund-processed-debt = ↩️ <b>Refund processed</b>

    Recovered { $recovered }/{ $original } credits from { $target }.

    { $debt } credits were already used and must be settled before paid usage continues.
refund-review-needed = ⚠️ <b>Refund needs review</b>

    Telegram reported a refund, but local reconciliation failed.
    Charge: <code>{ $chargeId }</code>
payment-settlement-failed = ⚠️ <b>Payment received</b>

    I could not update the credit balance automatically.
    Charge: <code>{ $chargeId }</code>

## Buy
buy-choose = 💰 <b>Add credits</b>

    Subscriptions add monthly personal credits. Packs are one-time.
    Standard chat replies spend 2 credits; paid tools show their own cost.
    In groups, group packs add shared credits everyone can use here.
buy-choose-personal = 💰 <b>Personal credits</b>

    Personal credits are yours and work in any chat.
    Standard chat replies spend 2 credits; paid tools show their own cost.
buy-choose-group = 💰 <b>Group shared pool</b>

    Group credits are shared in this chat; anyone here can spend them.
    Derp spends group credits before personal credits here.
buy-choose-subscriptions = 💰 <b>Monthly subscriptions</b>

    Subscriptions add personal credits now and on each renewal.
    They are not shared with groups, and standard chat still spends credits.
buy-target-personal = My personal credits
buy-target-group = This group's shared pool
buy-target-subscriptions = Monthly subscriptions
buy-private-required = 💬 <b>Open Derp privately</b>

    Personal credits and subscriptions are paid in a private chat.
    Tap Derp's profile, press Start, then use /buy there.
buy-private-sent = I sent the payment link privately.
buy-private-open-bot = Open Derp privately and press Start first, then try again.
buy-plan-not-found = Plan not found
buy-pack-not-found = Pack not found
buy-subscribe = Subscribe to { $plan }:
buy-pay-button = Pay { $stars }⭐/month
buy-plan-button = Monthly { $plan } — { $stars }⭐ → { $credits } credits ({ $savings } off){ $tag }
buy-pack-button = One-time { $pack } — { $stars }⭐ → { $credits } credits{ $bonus }
buy-group-pack-button = Group { $pack } — { $stars }⭐ → { $credits } shared credits{ $bonus }
buy-invoice-sub-title = { $plan } Subscription
buy-invoice-sub-description = { $credits } credits/month ({ $savings } savings)
buy-invoice-sub-label = { $plan } Subscription
buy-invoice-pack-title = { $pack } Credit Pack
buy-invoice-pack-description = { $credits } credits
buy-invoice-pack-label = { $pack } Pack
buy-invoice-group-pack-title = { $pack } Group Credit Pack
buy-invoice-group-pack-description = { $credits } credits for this chat
buy-invoice-group-pack-label = { $pack } Group Pack
buy-chat-groups-only = This command is for groups. Use /buy for personal credits.
buy-invoice-error = Could not create the payment link. Please try again.
payment-error-unknown-payload = Unknown invoice payload
payment-error-currency = Unsupported payment currency
payment-error-product = Unknown plan or pack
payment-error-amount = Invoice amount mismatch
payment-validation-error = Payment rejected: { $reason }

## Donations
donate-choose = ⭐ <b>Support Derp</b>

    Donations do not buy credits; they help cover hosting and development.
    Choose an amount below, or send <code>/donate 50</code>.
donate-invalid = Invalid donation amount
donate-thanks = ⭐ <b>Thank you!</b>

    { $stars } Stars received.
donate-error = Donation could not be processed. Please try again.

## Tools
tool-web-search = Search the web for current information
tool-imagine = Generate an image from a text description
tool-edit-image = Edit an image based on text instructions
tool-video = Generate a short video from a text description
tool-tts = Convert text to a voice message
tool-think = Deep reasoning for complex problems
tool-remind = Create, list, or cancel reminders
tool-memory = Read or update chat memory
tool-get-member = Get a chat member's profile photo
tool-category-search = Search & Research
tool-category-reasoning = Reasoning
tool-category-media = Media
tool-category-utility = Utilities
tool-cost-free = free
tool-cost-free-daily = { $freeDaily } free per user/chat/day
tool-cost-credits = { $credits } cr
tool-cost-credits-with-quota = { $credits } cr, { $freeDaily } free per user/chat/day
tool-disabled = { $tool } is disabled in your settings. Use /settings to enable it.
tool-confirm-source-chat = from this group's shared pool
tool-confirm-source-user = from your personal balance
tool-confirm-message = ⚠️ <b>Confirm spend</b>

    <code>{ $tool }</code> will use { $cost } credits { $source }.
    Remaining after this: { $remaining }.

    <i>Run it when the result is worth the cost.</i>
tool-confirm-message-private = ⚠️ <b>Confirm spend</b>

    <code>{ $tool }</code> will use { $cost } credits { $source }.

    <i>Run it when the result is worth the cost.</i>
tool-confirm-run = Run
tool-confirm-cancel = Cancel
tool-confirm-running-alert = Running { $tool }...
tool-confirm-running = ⏳ <b>Running</b>

    <code>{ $tool }</code> is in progress.
tool-confirm-done = ✅ <b>Done</b>

    <code>{ $tool }</code> finished.
tool-confirm-failed = ⚠️ <b>Could not finish this request</b>

    No credits were kept if the tool did not complete.
tool-confirm-billable-failed = ⚠️ <b>Result needs review</b>

    Provider work completed, but delivery failed. Credits were kept and admins were notified.
tool-confirm-cancelled = Cancelled.
tool-confirm-expired = This confirmation expired. Send the command again.
tool-confirm-invalid = This saved request is no longer valid. Send the command again.
tool-confirm-owner-only = Only the person who requested this can confirm or cancel it.

## Settings
settings-title = Settings
settings-personality = <b>Response style:</b> { $personality }
settings-user-style = <b>My default detail:</b> { $style }
settings-disabled-tools = <b>Disabled tools:</b> { $count }
settings-language = <b>Language:</b> { $lang }
settings-memory-access = <b>Memory updates:</b> { $access }
settings-reminders-access = <b>Reminder controls:</b> { $access }
settings-menu-personality = Style
settings-menu-language = Language
settings-menu-user-style = My style
settings-menu-user-style-cycle = Detail: { $style }
settings-menu-tools = Tool toggles
settings-menu-permissions = Permissions
settings-menu-memory = Memory
settings-menu-balance = Balance
settings-close = Close
settings-back = « Back
settings-tools-none = none
settings-tool-state-on = On
settings-tool-state-off = Off
settings-tool-enabled = { $tool } enabled
settings-tool-disabled = { $tool } disabled
settings-admin-only = Only chat admins can change settings.
settings-personality-default = Default
settings-personality-professional = Professional
settings-personality-casual = Casual
settings-personality-creative = Creative
settings-personality-custom = Custom
settings-personality-custom-button = Custom instructions
settings-personality-set = Response style set to { $personality }
settings-custom-sub-required = Custom instructions require a subscription. Use /buy
settings-custom-current-none = (none)
settings-custom-placeholder = Custom instructions
settings-custom-prompt = Send the instructions Derp should follow in this chat. Max { $max } characters. Reply /cancel to stop.
    Current: { $current }
settings-custom-too-long = Custom instructions are too long. Max { $max } characters.
settings-custom-saved = Custom instructions saved.
settings-custom-cancelled = Custom instruction setup cancelled.
settings-user-style-concise = Concise
settings-user-style-balanced = Balanced
settings-user-style-detailed = Detailed
settings-user-style-set = Default detail set to { $style }
settings-user-instructions-button = My instructions
settings-user-instructions-placeholder = Personal instructions
settings-user-instructions-prompt = Send personal instructions Derp should remember across chats. Max { $max } characters.
    Current: { $current }
    Reply /cancel to stop.
settings-user-instructions-saved = Personal instructions saved.
settings-language-en = English
settings-language-ru = Russian
settings-language-auto = Auto-detect
settings-lang-set = Language set to { $lang }
settings-lang-auto = Language: auto-detect from messages
settings-access-admins = admins only
settings-access-everyone = everyone
settings-menu-memory-access = Memory updates: { $access }
settings-menu-reminders-access = Reminder controls: { $access }
settings-memory-access-set = Memory updates: { $access }
settings-reminders-access-set = Reminder controls: { $access }
settings-memory-view-button = View memory
settings-memory-clear-button = Clear memory
settings-memory-title = Chat memory
settings-memory-none = No memory stored
settings-memory-cleared = Memory cleared
settings-balance-subscription = Plan: { $tier }
settings-balance-info = Personal credits: { $userCredits }
    Group credits: { $chatCredits }
    { $subscription }

    Use /buy to add credits.

## Reminders
reminder-created = Reminder created: "{ $description }"
reminder-cancelled = Reminder "{ $description }" cancelled.
reminder-none = No active reminders in this chat.
reminder-limit = Maximum { $limit } active reminders per chat reached.
reminder-not-found = Reminder not found
reminder-no-permission = Only the creator or admin can cancel this
reminder-cancel-button = Cancel

## Info
info-reply-required = Reply to a bot message with /info to see generation details.
info-not-found = Message not found in database.
info-no-details = No generation details available for this message.
info-header = Message Info:

## Inline
inline-title = Ask Derp
inline-placeholder = Keep typing your question.
inline-wait = Pause for a moment, then try this query again.
inline-error = Sorry, I couldn't generate a response. Try again.

## Chat
chat-error = Sorry, I had trouble generating a response. Please try again.

## Memory
memory-none = No memory stored for this chat.
memory-updated = Chat memory updated.
memory-cleared = Chat memory cleared.
memory-admin-only = Only chat admins can { $action } memory.
memory-usage = Usage: /memory\_set <text>

## Help
help-footer = Ask normally for chat and search. Use slash commands for media, reminders, memory, and paid actions.
help-other = Other

## Group onboarding
group-welcome = 👋 <b>Derp joined this chat.</b>

    Mention me or reply to my messages when you want help.
    Admins can use /settings for language, memory, and reminder permissions.
