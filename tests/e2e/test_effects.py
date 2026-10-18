import io
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from sqlalchemy import select

from derp.agent.models import AgentRun, RunOutput
from derp.history.models import MessageMedia
from derp.money.models import RunFee
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call

LIV = Person(1801, "Liv")
PUB = Room(-1018, title="Pub")
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def jpeg(color: str = "teal") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@cache
def clip(kind: str) -> bytes:
    """A tiny synthetic video (with a tone) or voice note made by ffmpeg."""
    if kind == "voice":
        args = ["-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:a", "libopus", "-f", "ogg"]
    else:
        args = [
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4",
        ]  # fmt: skip
    command = ["ffmpeg", "-v", "error", *args, "pipe:1"]
    return subprocess.run(command, check=True, capture_output=True).stdout  # noqa: S603 - fixed arguments


def results_of(model: ScriptedModel, tool: str) -> list[str]:
    """The tool's results in the latest request that has any (an image note may follow the run)."""
    for request in reversed(model.requests):
        found = [
            str(p.content)
            for m in request
            for p in m.parts
            if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name == tool
        ]
        if found:
            return found
    return []


async def ambient(conversation: Conversation, file_id: str, data: bytes, **content: Any) -> int:
    """A media message nobody asked Derp about (stored, not answered)."""
    conversation.api.register_file(file_id, data)
    sent = await conversation.send(LIV, PUB, **content)
    await conversation.settle(sent)
    return sent.message_id


def photo(file_id: str, data: bytes) -> list[dict[str, object]]:
    return [
        {
            "file_id": file_id,
            "file_unique_id": f"u-{file_id}",
            "width": 400,
            "height": 300,
            "file_size": len(data),
        }
    ]


def video(file_id: str, data: bytes) -> dict[str, object]:
    return {
        "file_id": file_id, "file_unique_id": f"u-{file_id}", "width": 320, "height": 240, "duration": 2,
        "file_size": len(data),
    }  # fmt: skip


async def test_an_image_meme_is_free_and_goes_out_as_a_photo(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    picture = await ambient(conversation, "pic", jpeg(), photo=photo("pic", jpeg()))
    model.reply(
        tool_call("make_meme", text="когда тесты зелёные", style="demotivator", message_id=picture),
        ".",
    )
    asked = await conversation.send(LIV, PUB, "derp make a demotivator of that")
    sent = await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)
    [attachment] = sent.attachments
    assert Image.open(io.BytesIO(attachment.data)).format == "JPEG"
    assert "Sent it as #" in results_of(model, "make_meme")[0]
    async with conversation.db.session() as session:
        assert await session.scalar(select(RunOutput.kind)) == "image"
        assert await session.scalar(select(RunFee.id)) is None


async def test_a_profile_photo_meme(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(LIV, PUB)
    conversation.api.register_file(f"profile-{LIV.id}", jpeg("orange"))
    model.reply(
        tool_call("make_meme", text="me", style="lobster", profile_photo=True),
        ".",
    )
    asked = await conversation.send(LIV, PUB, "derp meme my avatar")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)


async def test_a_chat_photo_meme(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(LIV, PUB)
    conversation.api.register_file(f"chatphoto-{PUB.id}", jpeg("navy"))
    model.reply(tool_call("make_meme", text="us", chat_photo=True), ".")
    asked = await conversation.send(LIV, PUB, "derp meme our chat picture")
    await conversation.expect_reply(asked, method="sendPhoto")
    await conversation.settle(asked)


async def test_a_missing_chat_photo_is_explained_to_the_model(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    model.reply(tool_call("make_meme", text="us", chat_photo=True), "no picture here")
    asked = await conversation.send(LIV, PUB, "derp meme our chat picture")
    await conversation.settle(asked)
    assert any("No chat photo" in result for result in results_of(model, "make_meme"))
    assert conversation.api.calls_of("sendPhoto", after=asked.after) == []


@needs_ffmpeg
async def test_a_video_meme_costs_the_flat_fee_on_paid_runs(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", LIV.id)).id
        await credit(session, wallet_id, "stars:liv", 650, "purchase")
    data = clip("video")
    moving = await ambient(conversation, "vid", data, video=video("vid", data))
    model.reply(
        tool_call("make_meme", text="this is fine", message_id=moving),
        ".",
    )
    asked = await conversation.send(LIV, PUB, "derp meme the video")
    sent = await conversation.expect_reply(asked, method="sendVideo", timeout=30)
    await conversation.settle(asked, timeout=30)
    assert sent.attachments[0].data[4:8] == b"ftyp"
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.charged_credits == conversation.settings.effect_credits
        assert await balance(session, wallet_id) == 650 - conversation.settings.effect_credits


async def test_sponsored_heavy_effects_stop_at_the_daily_count(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    async with conversation.db.transaction() as session:
        for _ in range(conversation.settings.sponsored_effects_per_day):
            session.add(
                RunFee(run_id=0, chat_id=PUB.id, user_id=LIV.id, item="meme", funding="sponsored", credits=0)
            )
    moving = await ambient(conversation, "vid2", b"\x00" * 64, video=video("vid2", b"\x00" * 64))
    model.reply(
        tool_call("make_meme", text="again", message_id=moving),
        "out of them",
    )
    asked = await conversation.send(LIV, PUB, "derp one more video meme")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "used up" in results_of(model, "make_meme")[0]
    assert conversation.api.calls_of("sendVideo", after=asked.after) == []


@needs_ffmpeg
async def test_a_replied_voice_note_is_reversed(conversation: Conversation, model: ScriptedModel) -> None:
    conversation.meet(LIV, PUB)
    data = clip("voice")
    voice = {"file_id": "vn", "file_unique_id": "u-vn", "duration": 2, "file_size": len(data)}
    spoken = await ambient(conversation, "vn", data, voice=voice)
    model.reply(tool_call("reverse_media"), ".")
    asked = await conversation.send(LIV, PUB, "derp play this backwards", reply_to=spoken)
    sent = await conversation.expect_reply(asked, method="sendVoice", timeout=30)
    await conversation.settle(asked, timeout=30)
    assert sent.attachments[0].data[:4] == b"OggS"


@needs_ffmpeg
async def test_song_and_anime_recognition(
    conversation: Conversation, model: ScriptedModel, web: dict[str, Any]
) -> None:
    conversation.settings = conversation.settings.model_copy(
        update={"acrcloud_host": "acr.example", "acrcloud_access_key": "k", "acrcloud_access_secret": "s"}
    )
    await conversation.restart()
    web["/v1/identify"] = {
        "status": {"code": 0},
        "metadata": {"music": [{"title": "Never Gonna Give You Up", "artists": [{"name": "Rick Astley"}]}]},
    }
    web["/search"] = {
        "result": [
            {"anilist": {"id": 1, "title": {"english": "Cowboy Bebop"}}, "episode": 5, "from": 83.2,
             "similarity": 0.95},
        ]
    }  # fmt: skip
    conversation.meet(LIV, PUB)
    data = clip("video")
    moving = await ambient(conversation, "vid3", data, video=video("vid3", data))
    model.reply(
        tool_call("identify_song", message_id=moving),
        tool_call("identify_anime", message_id=moving),
        "It's Rick.",
    )
    asked = await conversation.send(LIV, PUB, "derp what song and what anime is this?")
    await conversation.expect_reply(asked, timeout=30)
    await conversation.settle(asked, timeout=30)
    assert results_of(model, "identify_song") == ["Rick Astley — Never Gonna Give You Up\n(ACRCloud)"]
    [anime] = results_of(model, "identify_anime")
    assert anime.startswith("Cowboy Bebop, episode 5 at 1:23, similarity 95% https://anilist.co/anime/1")


async def test_keyed_and_model_backed_effects_hide_when_unavailable(
    conversation: Conversation, model: ScriptedModel, tmp_path: Path
) -> None:
    conversation.settings = conversation.settings.model_copy(
        update={"background_model": tmp_path / "missing.onnx"}
    )
    await conversation.restart()
    conversation.meet(LIV, PUB)
    model.reply("none")
    asked = await conversation.send(LIV, PUB, "derp cut out the background")
    await conversation.settle(asked)
    found = {tool.name for tool in model.infos[-1].function_tools}
    assert "make_meme" in found and "remove_background" not in found and "identify_song" not in found


VASYA = Person(1802, "Vasya", username="vasya_k")
VASILISA = Person(1803, "Vasilisa")


async def regulars(conversation: Conversation, *people: Person) -> None:
    for someone in people:
        conversation.meet(someone, PUB)
        said = await conversation.send(someone, PUB, "hi all")
        await conversation.settle(said)


async def test_a_member_named_by_the_asker_is_the_source_in_one_call(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    await regulars(conversation, VASYA, VASILISA)
    conversation.api.register_file(f"profile-{VASYA.id}", jpeg("red"))
    model.reply(
        tool_call("make_meme", text="кто", person="vas"),
        tool_call("make_meme", text="кто", person="Petya"),
        tool_call("make_meme", text="король", person="@vasya_k"),
        tool_call("reverse_media", person="Vasya"),
        ".",
    )
    asked = await conversation.send(LIV, PUB, "derp meme @vasya_k's avatar and mirror it")
    await conversation.settle(asked)
    assert len(conversation.api.calls_of("sendPhoto", after=asked.after)) == 2
    ambiguous, missing, sent = results_of(model, "make_meme")
    assert sent.startswith("Sent it as #")
    assert "Vasya (@vasya_k)" in ambiguous and "Vasilisa" in ambiguous
    assert "No one called 'Petya' here" in missing
    assert results_of(model, "reverse_media")[0].startswith("Sent it as #")


async def test_a_cutout_becomes_a_transparent_sticker(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    buffer = io.BytesIO()
    Image.new("RGBA", (300, 200), (255, 0, 0, 0)).save(buffer, format="PNG")
    cutout = buffer.getvalue()
    document = {
        "file_id": "cut",
        "file_unique_id": "u-cut",
        "file_name": "cutout.png",
        "file_size": len(cutout),
    }
    posted = await ambient(conversation, "cut", cutout, document=document)
    model.reply(tool_call("make_sticker", message_id=posted), ".")
    asked = await conversation.send(LIV, PUB, "derp make that a sticker")
    sent = await conversation.expect_reply(asked, method="sendSticker")
    await conversation.settle(asked)
    sticker = Image.open(io.BytesIO(sent.attachments[0].data))
    assert sticker.format == "WEBP" and sticker.size == (512, 341) and sticker.mode == "RGBA"
    assert results_of(model, "make_sticker")[0].startswith("Sent it as #")
    async with conversation.db.session() as session:
        assert await session.scalar(select(RunOutput.kind)) == "sticker"


@needs_ffmpeg
async def test_clips_too_long_to_watch_are_seen_through_a_frame_and_say_why(
    conversation: Conversation, model: ScriptedModel
) -> None:
    conversation.meet(LIV, PUB)
    data = clip("video")
    gif = await ambient(conversation, "gif", data, animation=video("gif", data) | {"duration": 200})
    note = {"file_id": "vnote", "file_unique_id": "u-vnote", "length": 240, "duration": 90}
    round_note = await ambient(conversation, "vnote", data, video_note=note)
    model.reply(
        tool_call("inspect_media", message_id=gif),
        "a test pattern",
        tool_call("inspect_media", message_id=round_note),
        "a test pattern again",
        "colour bars, twice",
    )
    asked = await conversation.send(LIV, PUB, "derp what's on the gif and the circle?")
    await conversation.expect_reply(asked, timeout=30)
    await conversation.settle(asked, timeout=30)
    labels = [
        str(part.content)
        for message in model.requests[-1]
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "inspect_media"
    ]
    assert "still frame of the gif" in labels[0] and "still frame of the video note" in labels[1]
    assert "(gif 3:20): not watched — longer than the 1:00 this answer can take." in labels[0]
    assert (
        "(video note 1:30): not watched — longer than the 1:00 this answer can take (paid answers take up to 3:00)."
        in labels[1]
    )
    async with conversation.db.session() as session:
        notes = set(await session.scalars(select(MessageMedia.description)))
    assert {"a test pattern", "a test pattern again"} <= notes
