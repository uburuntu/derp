"""Text sanitization utilities for Telegram messages.

Converts LLM Markdown output to HTML for safe sending via Telegram Bot API.
Handles edge cases like unmatched delimiters, nested formatting, and code blocks.
"""

from __future__ import annotations

import re
from html import escape as html_escape


def escape_html(text: str) -> str:
    """Escape HTML special characters.

    Args:
        text: Raw text to escape.

    Returns:
        Text with <, >, & escaped for HTML.
    """
    return html_escape(text, quote=False)


def _extract_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Extract fenced code blocks and replace with placeholders.

    This prevents markdown processing inside code blocks.

    Args:
        text: Input text with potential code blocks.

    Returns:
        Tuple of (text with placeholders, mapping of placeholder to code block HTML).
    """
    placeholders: dict[str, str] = {}
    counter = 0

    def replace_block(match: re.Match) -> str:
        nonlocal counter
        lang = match.group(1) or ""
        code = match.group(2)
        # Escape HTML inside code blocks
        escaped_code = escape_html(code.strip())
        placeholder = f"\x00CODEBLOCK{counter}\x00"
        counter += 1

        if lang:
            placeholders[placeholder] = (
                f'<pre><code class="language-{escape_html(lang)}">'
                f"{escaped_code}</code></pre>"
            )
        else:
            placeholders[placeholder] = f"<pre><code>{escaped_code}</code></pre>"
        return placeholder

    # Match ```lang\ncode\n``` or ```\ncode\n```
    pattern = r"```(\w*)\n(.*?)```"
    result = re.sub(pattern, replace_block, text, flags=re.DOTALL)
    return result, placeholders


def _extract_inline_code(text: str) -> tuple[str, dict[str, str]]:
    """Extract inline code and replace with placeholders.

    Args:
        text: Input text with potential inline code.

    Returns:
        Tuple of (text with placeholders, mapping of placeholder to inline code HTML).
    """
    placeholders: dict[str, str] = {}
    counter = 0

    def replace_code(match: re.Match) -> str:
        nonlocal counter
        code = match.group(1)
        escaped_code = escape_html(code)
        placeholder = f"\x00INLINECODE{counter}\x00"
        counter += 1
        placeholders[placeholder] = f"<code>{escaped_code}</code>"
        return placeholder

    # Match `code` but not inside already processed blocks
    # Use non-greedy match and avoid matching empty backticks
    pattern = r"`([^`\n]+)`"
    result = re.sub(pattern, replace_code, text)
    return result, placeholders


def _convert_links(text: str) -> str:
    """Convert markdown links to HTML.

    Args:
        text: Text with potential [text](url) links.

    Returns:
        Text with HTML anchor tags.
    """

    def replace_link(match: re.Match) -> str:
        link_text = match.group(1)
        url = match.group(2)
        # Escape the link text but not the URL (except for quotes)
        escaped_text = escape_html(link_text)
        # Basic URL validation - must start with http(s):// or tg://
        if not re.match(r"^(https?://|tg://)", url):
            # Not a valid URL, return as-is escaped
            return escape_html(match.group(0))
        # Escape quotes in URL for attribute safety
        safe_url = url.replace('"', "%22")
        return f'<a href="{safe_url}">{escaped_text}</a>'

    # Match [text](url) - text can contain anything except ]
    pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    return re.sub(pattern, replace_link, text)


def _convert_bold(text: str) -> str:
    """Convert markdown bold to HTML.

    Handles both **bold** and __bold__ syntax.
    """
    # **bold** - most common
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    # __bold__ - less common, but valid
    text = re.sub(r"__([^_]+)__", r"<b>\1</b>", text)
    return text


def _convert_italic(text: str) -> str:
    """Convert markdown italic to HTML.

    Handles *italic* syntax. Note: _italic_ is tricky because of
    words_with_underscores, so we're more conservative with it.
    """
    # *italic* - use word boundary awareness
    # Don't match if preceded by * (would be bold) or followed by * (would be bold)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)

    # _italic_ - match if preceded by whitespace/start/opening-punct and followed by
    # whitespace/end/closing-punct. This handles _word_, _word_! (_word_) etc.
    # while still avoiding snake_case_variables (no letter before underscore)
    text = re.sub(
        r"(?:^|(?<=[\s([{<]))_([^_]+)_(?:$|(?=[\s.,!?;:)}\]>]))", r"<i>\1</i>", text
    )
    return text


def _convert_strikethrough(text: str) -> str:
    """Convert markdown strikethrough to HTML."""
    return re.sub(r"~~([^~]+)~~", r"<s>\1</s>", text)


def _convert_spoiler(text: str) -> str:
    """Convert markdown spoiler to HTML (Telegram-specific)."""
    return re.sub(r"\|\|([^|]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)


def _convert_underline(text: str) -> str:
    """Convert markdown underline to HTML.

    Note: Standard markdown doesn't have underline, but some LLMs use it.
    We use a rare syntax to avoid conflicts.
    """
    # ++underline++ syntax (rare, used by some extended markdown)
    return re.sub(r"\+\+([^+]+)\+\+", r"<u>\1</u>", text)


def _restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    """Restore code block placeholders with their HTML."""
    for placeholder, html in placeholders.items():
        text = text.replace(placeholder, html)
    return text


def _escape_remaining_special_chars(text: str) -> str:
    """Escape any remaining HTML special characters that weren't part of formatting.

    This is called after markdown conversion, so we need to be careful
    not to escape the HTML tags we just created.
    """
    # Match valid HTML tags more precisely:
    # - Opening tags: <tagname ...>
    # - Closing tags: </tagname>
    # - Self-closing: <tagname ... />
    # Valid tag names start with letter, can contain letters, numbers, hyphens
    html_tag_pattern = r"(</?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^>]*)?>)"

    parts = re.split(html_tag_pattern, text)
    result = []
    for part in parts:
        # Check if this is a valid HTML tag we created
        if re.match(r"^</?[a-zA-Z][a-zA-Z0-9-]*(?:\s+[^>]*)?>$", part):
            # This is an HTML tag, keep as-is
            result.append(part)
        else:
            # This is content, escape special chars
            # Order matters: & first, then < and >
            escaped = part.replace("&", "&amp;")
            escaped = escaped.replace("<", "&lt;")
            escaped = escaped.replace(">", "&gt;")
            result.append(escaped)
    return "".join(result)


def markdown_to_html(text: str) -> str:
    """Convert markdown formatting to HTML for Telegram.

    Converts common markdown syntax to their HTML equivalents:
    - **bold** or __bold__ -> <b>bold</b>
    - *italic* or _italic_ -> <i>italic</i>
    - ~~strikethrough~~ -> <s>strikethrough</s>
    - `code` -> <code>code</code>
    - ```code block``` -> <pre><code>code block</code></pre>
    - [text](url) -> <a href="url">text</a>
    - ||spoiler|| -> <tg-spoiler>spoiler</tg-spoiler>
    - ++underline++ -> <u>underline</u>

    Args:
        text: Markdown-formatted text from LLM.

    Returns:
        HTML-formatted text safe for Telegram.
    """
    if not text:
        return ""

    # Step 1: Extract and protect code blocks (they shouldn't be processed)
    text, code_blocks = _extract_code_blocks(text)

    # Step 2: Extract and protect inline code
    text, inline_codes = _extract_inline_code(text)

    # Step 3: Convert markdown to HTML (order matters!)
    # Links first (before we mess with brackets)
    text = _convert_links(text)

    # Bold before italic (** before *)
    text = _convert_bold(text)
    text = _convert_italic(text)

    # Other formatting
    text = _convert_strikethrough(text)
    text = _convert_spoiler(text)
    text = _convert_underline(text)

    # Step 4: Escape any remaining special HTML characters
    text = _escape_remaining_special_chars(text)

    # Step 5: Restore code blocks and inline code
    text = _restore_placeholders(text, inline_codes)
    text = _restore_placeholders(text, code_blocks)

    return text


def sanitize_for_telegram(text: str, *, convert_markdown: bool = True) -> str:
    """Sanitize text for safe sending to Telegram.

    Args:
        text: Raw text (possibly with markdown formatting).
        convert_markdown: If True, convert markdown to HTML. If False, just escape.

    Returns:
        HTML-formatted text safe for Telegram's HTML parse mode.
    """
    if not text:
        return ""

    if convert_markdown:
        return markdown_to_html(text)
    else:
        return escape_html(text)


def strip_html_tags(text: str) -> str:
    """Remove all HTML tags from text.

    Useful for fallback to plain text when HTML parsing fails.

    Args:
        text: HTML-formatted text.

    Returns:
        Plain text with HTML tags removed.
    """
    # First remove tags
    text = re.sub(r"<[^>]+>", "", text)
    # Then replace common entities (order: &amp; last to avoid double-unescaping)
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&amp;", "&")
    return text
