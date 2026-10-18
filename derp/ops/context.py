"""What the model sees, as one self-contained HTML page (the owners' /context).

Renders a ``Runtime.preview``: the chat completion request exactly as it would go to
OpenRouter — routing and settings, every tool with its description and schema, and
every message (persona, transcript, replayed runs, the situation and the current
message, pictures inline). Everything shown is escaped: chat text is untrusted, and
the page runs no scripts.
"""

import contextlib
import json
from datetime import datetime
from html import escape
from typing import Any, cast

from derp.agent.runtime import Preview
from derp.history.render import estimate_tokens

type Json = dict[str, Any]

NOTE = (
    "<p class=meta>The request a message here would send now, captured before it left: no model was called, "
    "nothing was charged. Estimates are rough (bytes ÷ 4).</p>"
)

STYLE = """
:root { color-scheme: light dark; --line: #8884; --soft: #8881; }
body { font: 14px/1.45 system-ui, sans-serif; max-width: 72rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .2rem 1rem; }
dt { opacity: .7; } dd { margin: 0; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--soft); padding: .6rem; border-radius: 6px;
      font: 12.5px/1.4 ui-monospace, monospace; margin: .4rem 0; }
details { border: 1px solid var(--line); border-radius: 8px; padding: .3rem .7rem; margin: .4rem 0; }
summary { cursor: pointer; } summary small, .meta { opacity: .6; }
.message { border-left: 4px solid var(--line); padding: .2rem .8rem; margin: .8rem 0; }
.system { border-color: #a6f; } .user { border-color: #3a7; } .assistant { border-color: #38f; }
.tool { border-color: #e90; } .role { font-weight: 600; text-transform: uppercase; font-size: 12px; }
img { max-width: 24rem; max-height: 24rem; border-radius: 6px; display: block; margin: .4rem 0; }
"""


def _tokens(value: Any) -> int:
    return estimate_tokens(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def _json(value: Any) -> str:
    return f"<pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"


def _clipped(value: Any) -> Any:
    """``value`` with inline files (data URLs, base64) cut short, for the raw view."""
    if isinstance(value, dict):
        return {k: _clipped(v) for k, v in cast(Json, value).items()}
    if isinstance(value, list):
        return [_clipped(v) for v in cast(list[Any], value)]
    if isinstance(value, str) and len(value) > 300 and (value.startswith("data:") or " " not in value[:300]):
        return f"{value[:60]}… ({len(value):,} characters)"
    return value


def _part(part: Json) -> str:
    kind = part.get("type")
    if kind == "text":
        return f"<pre>{escape(str(part.get('text', '')))}</pre>"
    if kind == "image_url":
        url = str(cast(Json, part.get("image_url") or {}).get("url", ""))
        if url.startswith("data:image/"):
            return f'<img alt="image" src="{escape(url, quote=True)}">'
        return f"<p class=meta>image {escape(url[:200])}</p>"
    return f"<p class=meta>{escape(str(kind))} part</p>" + _json(_clipped(part))


def _content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return f"<pre>{escape(content)}</pre>"
    return "".join(_part(cast(Json, p)) for p in cast(list[Any], content))


def _message(index: int, message: Json) -> str:
    role = str(message.get("role", "?"))
    head = f"<span class=role>{escape(role)}</span> <span class=meta>#{index} · ≈{_tokens(message):,} tokens"
    if call_id := message.get("tool_call_id"):
        head += f" · answers {escape(str(call_id))}"
    parts = [f'<div class="message {escape(role)}">{head}</span>', _content(message.get("content"))]
    for call in cast(list[Json], message.get("tool_calls") or []):
        function = cast(Json, call.get("function") or {})
        arguments: Any = function.get("arguments", "")
        with contextlib.suppress(ValueError):
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        parts.append(
            f"<p>→ <code>{escape(str(function.get('name')))}</code> <span class=meta>{escape(str(call.get('id', '')))}</span></p>"
        )
        parts.append(_json(arguments))
    if reasoning := message.get("reasoning"):
        parts.append(f"<details><summary>reasoning</summary><pre>{escape(str(reasoning))}</pre></details>")
    return "".join(parts) + "</div>"


def _tool(tool: Json) -> str:
    if tool.get("type") != "function":  # a server tool: OpenRouter runs it
        return f"<details><summary><code>{escape(str(tool.get('type')))}</code> <small>server tool</small></summary>{_json(tool)}</details>"
    function = cast(Json, tool.get("function") or {})
    description = str(function.get("description") or "")
    first = description.strip().splitlines()[0] if description.strip() else ""
    return (
        f"<details><summary><code>{escape(str(function.get('name')))}</code> <small>{escape(first)} · "
        f"≈{_tokens(tool):,} tokens</small></summary><pre>{escape(description)}</pre>"
        f"{_json(function.get('parameters', {}))}</details>"
    )


def render(preview: Preview, *, chat: str, asker: str, when: datetime) -> str:
    body = preview.body
    messages = cast(list[Json], body.get("messages") or [])
    tools = cast(list[Json], body.get("tools") or [])
    settings = {k: v for k, v in body.items() if k not in {"messages", "tools"}}
    fallbacks = ", ".join(preview.pin.fallbacks) or "none"
    facts = {
        "Chat": chat,
        "Asked by": asker,
        "At": when.strftime("%Y-%m-%d %H:%M %Z"),
        "Paid by": preview.funding.value,
        "Model": f"{preview.pin.model} (fallbacks: {fallbacks})",
        "Size": f"≈{_tokens(body):,} tokens in all: window ≈{preview.window_tokens:,}, persona and tools "
        f"≈{preview.prefix_tokens:,}; {len(messages)} messages, {len(tools)} tools",
    }
    rows = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts.items())
    return "\n".join(
        [
            "<!doctype html><html><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Derp context · {escape(chat)}</title><style>{STYLE}</style></head><body>",
            "<h1>What the model would see</h1>",
            NOTE,
            f"<dl>{rows}</dl>",
            f"<h2>Request settings</h2>{_json(settings)}",
            f"<h2>Tools ({len(tools)})</h2>",
            *(_tool(tool) for tool in tools),
            f"<h2>Messages ({len(messages)})</h2>",
            *(_message(index, message) for index, message in enumerate(messages)),
            f"<h2>Raw request</h2><details><summary>JSON (inline files cut short)</summary>{_json(_clipped(body))}</details>",
            "</body></html>",
        ]
    )
