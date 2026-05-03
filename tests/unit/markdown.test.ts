import { describe, expect, test } from "bun:test";
import { markdownToHtml, stripHtmlTags } from "../../src/common/markdown";

describe("markdownToHtml", () => {
	test("escapes HTML entities", () => {
		expect(markdownToHtml("a < b > c & d")).toBe("a &lt; b &gt; c &amp; d");
	});

	test("converts bold", () => {
		expect(markdownToHtml("hello **world**")).toBe("hello <b>world</b>");
	});

	test("converts italic", () => {
		expect(markdownToHtml("hello *world*")).toBe("hello <i>world</i>");
	});

	test("converts bold italic", () => {
		expect(markdownToHtml("***bold italic***")).toBe(
			"<b><i>bold italic</i></b>",
		);
	});

	test("converts inline code", () => {
		expect(markdownToHtml("use `async/await` here")).toBe(
			"use <code>async/await</code> here",
		);
	});

	test("escapes HTML inside inline code", () => {
		expect(markdownToHtml("run `<script>alert(1)</script>`")).toBe(
			"run <code>&lt;script&gt;alert(1)&lt;/script&gt;</code>",
		);
	});

	test("converts fenced code blocks", () => {
		const md = "```js\nconst x = 1;\n```";
		const html = markdownToHtml(md);
		expect(html).toContain('<pre><code class="language-js">');
		expect(html).toContain("const x = 1;");
		expect(html).toContain("</code></pre>");
	});

	test("converts code blocks without language", () => {
		const md = "```\nhello\n```";
		const html = markdownToHtml(md);
		expect(html).toContain("<pre><code>");
		expect(html).toContain("hello");
	});

	test("converts links", () => {
		expect(markdownToHtml("[click](https://example.com)")).toBe(
			'<a href="https://example.com">click</a>',
		);
	});

	test("converts strikethrough", () => {
		expect(markdownToHtml("~~deleted~~")).toBe("<s>deleted</s>");
	});

	test("does not format inside code blocks", () => {
		const md = "```\n**not bold** *not italic*\n```";
		const html = markdownToHtml(md);
		expect(html).not.toContain("<b>");
		expect(html).not.toContain("<i>");
		expect(html).toContain("**not bold**");
	});

	test("does not format inside inline code", () => {
		const md = "`**not bold**`";
		const html = markdownToHtml(md);
		expect(html).not.toContain("<b>");
		expect(html).toContain("**not bold**");
	});

	test("handles plain text without formatting", () => {
		expect(markdownToHtml("hello world")).toBe("hello world");
	});

	test("handles mixed content", () => {
		const md = "Hello **world**, use `code` and *italic*!";
		const html = markdownToHtml(md);
		expect(html).toContain("<b>world</b>");
		expect(html).toContain("<code>code</code>");
		expect(html).toContain("<i>italic</i>");
	});

	test("does not treat asterisks in words as formatting", () => {
		// file*name should not become file<i>name
		const result = markdownToHtml("file*name*end");
		// This is a tricky edge case — the regex should handle word boundaries
		expect(result).not.toContain("<i>name");
	});
});

describe("stripHtmlTags", () => {
	test("removes all tags", () => {
		expect(stripHtmlTags("<b>bold</b> and <i>italic</i>")).toBe(
			"bold and italic",
		);
	});

	test("handles nested tags", () => {
		expect(stripHtmlTags("<b><i>nested</i></b>")).toBe("nested");
	});

	test("passes plain text through", () => {
		expect(stripHtmlTags("no tags here")).toBe("no tags here");
	});
});
