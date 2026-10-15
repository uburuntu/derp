from sqlalchemy import select

from derp.history.models import Chat, ChatMember, MemberEvent
from tests.e2e.harness import Conversation, Person, Room

GROUP = Room(-1002, title="Club")
OWNER = Person(301, "Olga")
NEWBIE = Person(302, "Nick")


def member_update(
    chat: Room, actor: Person, user: dict[str, object], old: str, new: str
) -> dict[str, object]:
    return {
        "chat": chat.payload(),
        "from": actor.payload(),
        "date": 0,
        "old_chat_member": {"status": old, "user": user},
        "new_chat_member": {"status": new, "user": user},
    }


async def test_derp_added_refreshes_profile_and_members_are_tracked(conversation: Conversation) -> None:
    conversation.meet(OWNER, GROUP, status="creator")
    conversation.api.register_user(NEWBIE.payload())
    bot_user = conversation.api.bot_user
    added = await conversation.push("my_chat_member", member_update(GROUP, OWNER, bot_user, "left", "member"))
    await conversation.settle(added)
    await conversation.api.wait_call("getChatMemberCount")
    joined = await conversation.push(
        "chat_member", member_update(GROUP, OWNER, NEWBIE.payload(), "left", "member")
    )
    await conversation.settle(joined)

    async with conversation.db.session() as session:
        chat = await session.get(Chat, GROUP.id)
        assert chat is not None and chat.bot_status == "member"
        assert [(a["id"], a["status"]) for a in chat.profile["admins"]] == [(OWNER.id, "creator")]
        owner = await session.get(ChatMember, (GROUP.id, OWNER.id))
        assert owner is not None and owner.status == "creator"
        events = list(await session.scalars(select(MemberEvent.kind).where(MemberEvent.user_id == NEWBIE.id)))
        assert events == ["join"]
