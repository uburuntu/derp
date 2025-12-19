"""Tests for derp/common/sanitize.py - Markdown to HTML conversion."""

from derp.common.sanitize import (
    escape_html,
    markdown_to_html,
    sanitize_for_telegram,
    strip_html_tags,
)


class TestEscapeHtml:
    """Tests for HTML escaping."""

    def test_escapes_angle_brackets(self):
        assert escape_html("a < b > c") == "a &lt; b &gt; c"

    def test_escapes_ampersand(self):
        assert escape_html("foo & bar") == "foo &amp; bar"

    def test_preserves_quotes(self):
        # We don't escape quotes for text content
        assert escape_html('say "hello"') == 'say "hello"'

    def test_empty_string(self):
        assert escape_html("") == ""

    def test_no_special_chars(self):
        assert escape_html("hello world") == "hello world"


class TestMarkdownToHtml:
    """Tests for markdown to HTML conversion."""

    # --- Bold ---
    def test_bold_double_asterisks(self):
        assert markdown_to_html("hello **world**") == "hello <b>world</b>"

    def test_bold_double_underscores(self):
        assert markdown_to_html("hello __world__") == "hello <b>world</b>"

    # --- Italic ---
    def test_italic_single_asterisks(self):
        assert markdown_to_html("hello *world*") == "hello <i>world</i>"

    def test_italic_underscores_with_spaces(self):
        # Only match underscores surrounded by whitespace to avoid snake_case
        assert markdown_to_html("hello _world_ there") == "hello <i>world</i> there"

    def test_no_italic_in_snake_case(self):
        # Don't match underscores in middle of words
        result = markdown_to_html("foo_bar_baz")
        assert result == "foo_bar_baz"

    # --- Code ---
    def test_inline_code(self):
        assert markdown_to_html("use `print()` here") == "use <code>print()</code> here"

    def test_code_block(self):
        result = markdown_to_html("```\nprint('hello')\n```")
        assert "<pre><code>" in result
        assert "print(&#x27;hello&#x27;)" in result or "print('hello')" in result
        assert "</code></pre>" in result

    def test_code_block_with_language(self):
        result = markdown_to_html("```python\nprint('hello')\n```")
        assert 'class="language-python"' in result
        assert "<pre><code" in result

    def test_code_block_escapes_html_inside(self):
        result = markdown_to_html("```\n<script>alert('xss')</script>\n```")
        assert "&lt;script&gt;" in result
        assert "<script>" not in result

    # --- Strikethrough ---
    def test_strikethrough(self):
        assert markdown_to_html("~~deleted~~") == "<s>deleted</s>"

    # --- Spoiler ---
    def test_spoiler(self):
        assert markdown_to_html("||secret||") == "<tg-spoiler>secret</tg-spoiler>"

    # --- Links ---
    def test_link_http(self):
        result = markdown_to_html("click [here](https://example.com)")
        assert '<a href="https://example.com">here</a>' in result

    def test_link_https(self):
        result = markdown_to_html("[Google](https://google.com)")
        assert '<a href="https://google.com">Google</a>' in result

    def test_link_telegram(self):
        result = markdown_to_html("mention [user](tg://user?id=123)")
        assert '<a href="tg://user?id=123">user</a>' in result

    def test_invalid_link_protocol_escaped(self):
        # Invalid protocols should be escaped, not converted to links
        result = markdown_to_html("[click](javascript:alert())")
        assert "<a " not in result
        assert "javascript" in result

    # --- Escaping remaining chars ---
    def test_escapes_angle_brackets_in_text(self):
        result = markdown_to_html("1 < 2 and 3 > 2")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_preserves_emoji(self):
        result = markdown_to_html("Hello 👋 World 🌍")
        assert "👋" in result
        assert "🌍" in result

    def test_preserves_unicode(self):
        result = markdown_to_html("Привет мир 你好世界")
        assert "Привет мир" in result
        assert "你好世界" in result

    # --- Mixed formatting ---
    def test_bold_and_italic(self):
        result = markdown_to_html("**bold** and *italic*")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result

    def test_complex_message(self):
        text = (
            "Hello **user**, check out [this link](https://example.com) and `run code`."
        )
        result = markdown_to_html(text)
        assert "<b>user</b>" in result
        assert '<a href="https://example.com">this link</a>' in result
        assert "<code>run code</code>" in result

    # --- Edge cases ---
    def test_empty_string(self):
        assert markdown_to_html("") == ""

    def test_none_handled(self):
        # The function should handle empty strings but text is typed as str
        assert markdown_to_html("") == ""

    def test_unmatched_asterisks_preserved(self):
        # Single asterisk without pair should be preserved
        result = markdown_to_html("hello * world")
        # Should not create broken italic
        assert result == "hello * world"

    def test_code_prevents_inner_markdown(self):
        # Markdown inside code blocks should not be processed
        result = markdown_to_html("```\n**not bold**\n```")
        assert "<b>" not in result
        assert "**not bold**" in result or "*not bold*" in result


class TestSanitizeForTelegram:
    """Tests for the main sanitization function."""

    def test_converts_markdown_by_default(self):
        result = sanitize_for_telegram("**bold** text")
        assert "<b>bold</b>" in result

    def test_escape_only_mode(self):
        result = sanitize_for_telegram("**bold** <script>", convert_markdown=False)
        assert "**bold**" in result
        assert "&lt;script&gt;" in result

    def test_empty_string(self):
        assert sanitize_for_telegram("") == ""


class TestStripHtmlTags:
    """Tests for HTML tag stripping (fallback to plain text)."""

    def test_removes_simple_tags(self):
        assert strip_html_tags("<b>bold</b>") == "bold"

    def test_removes_multiple_tags(self):
        result = strip_html_tags("<b>bold</b> and <i>italic</i>")
        assert result == "bold and italic"

    def test_restores_entities(self):
        result = strip_html_tags("&lt;hello&gt;")
        assert result == "<hello>"

    def test_handles_nested_tags(self):
        result = strip_html_tags("<b><i>nested</i></b>")
        assert result == "nested"

    def test_plain_text_unchanged(self):
        assert strip_html_tags("hello world") == "hello world"


class TestLlmOutputExamples:
    """Test real-world LLM output patterns."""

    def test_llm_code_example(self):
        llm_output = """Here's how to do it:

```python
def hello():
    print("Hello, world!")
```

That's it!"""
        result = markdown_to_html(llm_output)
        assert "<pre><code" in result
        assert "def hello():" in result

    def test_llm_formatted_list(self):
        llm_output = "**Features:**\n- *Fast* processing\n- **Accurate** results"
        result = markdown_to_html(llm_output)
        assert "<b>Features:</b>" in result
        assert "<i>Fast</i>" in result
        assert "<b>Accurate</b>" in result

    def test_llm_with_special_chars(self):
        llm_output = "The expression 3 < 5 && 5 > 3 is true. Use `&&` for AND."
        result = markdown_to_html(llm_output)
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<code>&amp;&amp;</code>" in result or "<code>&&</code>" in result

    def test_telegram_error_case(self):
        # This is the type of output that was causing the original error
        llm_output = "**Tool:** `generate_image`\nCost: 5 credits | Free daily: 0"
        result = markdown_to_html(llm_output)
        # Should not raise any errors and should be valid HTML
        assert "<b>Tool:</b>" in result
        assert "<code>generate_image</code>" in result
