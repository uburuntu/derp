## General
welcome = Hey! I'm Derp, your AI assistant.
welcome-bonus = { $credits } free credits to get started!
welcome-features = <b>What I can do</b>
    /search — Search the web
    /imagine — Generate images
    /edit — Edit images
    /think — Deep reasoning
    /tts — Text to speech
    /video — Generate videos
    /remind — Set reminders

    <i>Mention me or reply to my messages. Use /help for more.</i>
error-generic = Something went wrong. Please try again.

## Commands
cmd-start-desc = Start the bot
cmd-help-desc = Show help and available commands
cmd-settings-desc = Open settings menu
cmd-credits-desc = Check credit balance
cmd-buy-desc = Buy credits or subscribe
cmd-memory-desc = View chat memory
cmd-reminders-desc = List active reminders
cmd-info-desc = Show message generation details

## Credits
credits-balance = Your credits: { $userCredits }
credits-chat-pool = Chat pool: { $chatCredits }
credits-subscription = Subscription: { $tier } (renews { $expiry })
credits-subscription-expired = Subscription: expired
credits-added = { $credits } credits added to your balance!
credits-added-chat = { $credits } credits added to this chat's pool!
credits-subscribed = Subscribed to { $plan }! { $credits } credits added. Your subscription renews monthly.
credits-renewed = { $plan } subscription renewed! { $credits } credits added.
credits-insufficient = Need { $cost } credits for { $tool }. Use /buy to get credits.

## Buy
buy-choose = Choose a plan or credit pack:
buy-plan-not-found = Plan not found
buy-pack-not-found = Pack not found
buy-subscribe = Subscribe to { $plan }:
buy-pay-button = Pay { $stars }⭐/month

## Transfer
transfer-prompt = Transfer credits to this chat's pool.
    Your balance: { $balance }
    Minimum: 100
    Reply with the amount to transfer:
transfer-min = Minimum transfer: 100 credits
transfer-insufficient = Insufficient credits. You have { $balance }.
transfer-success = Transferred { $amount } credits to this chat's pool.
transfer-failed = Transfer failed: { $error }
transfer-groups-only = Transfers only work in groups

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

## Settings
settings-title = Settings
settings-personality = Personality: { $personality }
settings-language = Language: { $lang }
settings-memory-access = Memory access: { $access }
settings-reminders-access = Reminders access: { $access }
settings-close = Close
settings-back = « Back
settings-custom-sub-required = Custom prompts require a subscription. Use /buy
settings-custom-prompt = Send your custom system prompt as a reply to this message. Max 2000 characters.
    Current: { $current }
settings-lang-set = Language set to { $lang }
settings-lang-auto = Language: auto-detect from messages
settings-balance-info = Your credits: { $userCredits }
    Chat pool: { $chatCredits }
    { $subscription }

    Use /buy to get more.

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
inline-placeholder = Thinking...
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
help-footer = I can do all of this automatically — just describe what you need!

## Group onboarding
group-welcome = 👋 <b>Hey everyone! I'm Derp, your AI assistant.</b>

    Mention me or reply to my messages to chat.
    Admins: /settings to configure.
