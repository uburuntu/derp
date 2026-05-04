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

## Buy
buy-choose = 💰 <b>Add credits</b>

    Subscriptions add monthly personal credits. Packs are one-time.
    In groups, group packs add shared credits everyone can use here.
buy-plan-not-found = Plan not found
buy-pack-not-found = Pack not found
buy-subscribe = Subscribe to { $plan }:
buy-pay-button = Pay { $stars }⭐/month
buy-plan-button = Monthly { $plan } — { $stars }⭐ → { $credits } credits ({ $savings } off){ $tag }
buy-pack-button = One-time { $pack } — { $stars }⭐ → { $credits } credits{ $bonus }
buy-group-pack-button = Group { $pack } — { $stars }⭐ → { $credits } shared credits{ $bonus }
buy-chat-groups-only = This command is for groups. Use /buy for personal credits.
buy-invoice-error = Could not create the payment link. Please try again.

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
