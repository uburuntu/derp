"""Enforce the effects an admin confirmed for an automatic turn, including when Code Mode calls a tool."""

import json
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import to_jsonable_python

from derp.agent.approvals import ask
from derp.agent.deps import RunDeps
from derp.agent.outputs import Refused, deliver
from derp.memory.models import ReplyRule
from derp.memory.notes import is_admin

# An unknown or newly introduced tool requires an exact-action approval by default.
READ_ONLY = frozenset(
    {
        "about_derp",
        "read_source",
        "search_source",
        "search_history",
        "read_messages",
        "chat_query",
        "chat_info",
        "find_notes",
        "inspect_media",
        "digest",
        "lookup",
        "weather",
        "market",
        "identify",
        "run_program",
        "chat_accounting",
        "list_jobs",
    }
)


@dataclass
class RuleTools(WrapperToolset[RunDeps]):
    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunDeps], tool: ToolsetTool[RunDeps]
    ) -> Any:
        deps = ctx.deps
        rules = deps.reply_rules
        capability = "market_chart" if name == "market" and tool_args.get("chart") else name
        if capability in READ_ONLY:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        # Automatic runs cannot speak for their message author or silently change a private setting.
        if (
            name == "wallet_status"
            or (name == "purchase_button" and not tool_args.get("for_chat"))
            or (
                name == "change_setting"
                and tool_args.get("setting") in {"my_coins_in_groups", "name_me_when_i_chip_in"}
            )
            or (name == "remember" and tool_args.get("about") == "me")
        ):
            return "Personal settings, purchases and notes require the person's own request."
        args: dict[str, Any] = to_jsonable_python(tool_args)
        rule = rules[0]
        granted = False
        fixed = False
        for candidate in rules:
            for grant in candidate.grants:
                if grant["tool"] != capability:
                    continue
                expected = grant.get("arguments")
                if expected is not None:
                    bound = {
                        k: deps.trigger_message_id if v == "$message" else v for k, v in expected.items()
                    }
                    try:
                        # The dispatcher supplies default values (e.g. repeat="once"). Compare the
                        # effective calls so an omitted default never widens or invalidates a grant.
                        bound = to_jsonable_python(tool.args_validator.validate_python(bound))
                    except ValidationError:
                        continue
                    if bound != args:
                        continue
                rule, granted, fixed = candidate, True, expected is not None
                break
            if granted:
                break
        meta: dict[str, Any] = ctx.tool_call_metadata or {}
        exact = ctx.tool_call_approved and meta.get("rule_tool") == name and meta.get("args") == args
        priced = name == "generate_image" and ctx.tool_call_approved and "aspect_ratio" in meta
        if exact or priced:
            origin = next((r for r in rules if r.id == meta.get("reply_rule_id")), None)
            if origin is None:
                return "The rule that authorized this action is no longer active."
            rule = origin
        async with deps.db.session() as session:
            current = await session.get(ReplyRule, rule.id)
            if current is None or current.chat_id != deps.chat.id or current.state != "active":
                return "The authorizing rule was paused or removed."
        if granted or exact or priced:
            admin = deps.rule_approver if exact or priced else rule.admin_id
            if admin is None or not await is_admin(deps.bot, deps.chat, admin):
                return "The authorizing admin no longer has permission here."
            # Use an independent authority context for simultaneous tool calls. Output and edit state
            # belongs to the original run; propagate scalar updates alongside its shared lists and sets.
            effective = replace(
                deps,
                rule_approver=admin,
                authorizing_rule_id=rule.id,
                user_id=admin if fixed or (exact and name != "generate_image") else deps.user_id,
            )
            ctx = replace(ctx, deps=effective)
            if exact:  # the ordinary image price confirmation still applies
                ctx = replace(ctx, tool_call_approved=False, tool_call_metadata=None)
            riders, zone = effective.riders, effective.tz
            try:
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            finally:
                deps.riders += effective.riders - riders
                if effective.tz != zone:
                    deps.tz = effective.tz
                if effective.edited_target is not None:
                    deps.edited_target = effective.edited_target
        display = f"{name} " + json.dumps(args, ensure_ascii=False)
        if len(display) > 2200:
            return "This action exceeds what can be shown for approval. Ask the rule admin to request it directly."
        if not await is_admin(deps.bot, deps.chat, rule.admin_id):
            return "The rule's admin cannot authorize this action now."
        try:
            await deliver(
                deps,
                "other",
                "rule action confirmation",
                lambda: ask(
                    deps.db,
                    deps.bot,
                    deps.settings,
                    deps.target,
                    kind="rule_action",
                    price=0,
                    args={"rule_tool": name, "args": args, "reply_rule_id": rule.id},
                    user_id=rule.admin_id,
                    run_id=deps.run_id,
                    call=ctx.tool_call_id or "",
                    locale=deps.locale,
                    display=display,
                ),
                history=False,
            )
        except Refused as refusal:
            return str(refusal)
        raise ApprovalRequired
