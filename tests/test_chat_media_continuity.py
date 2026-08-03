"""Current media remains associated with its normalized attachment reference."""

import json

from aiogram.types import (
    PaidMediaInfo,
    PaidMediaPhoto,
    PaidMediaVideo,
    PhotoSize,
    Video,
)
from pydantic_ai import BinaryContent

from derp.handlers.chat import _current_user_prompt, _current_user_turn


def test_partial_hydration_marks_the_matching_attachment_available(
    make_message,
) -> None:
    message = make_message(
        text="compare these",
        paid_media=PaidMediaInfo(
            star_count=1,
            paid_media=[
                PaidMediaPhoto(
                    photo=[
                        PhotoSize(
                            file_id="photo-file",
                            file_unique_id="photo-unique",
                            width=800,
                            height=600,
                            file_size=100,
                        )
                    ]
                ),
                PaidMediaVideo(
                    video=Video(
                        file_id="video-file",
                        file_unique_id="video-unique",
                        width=1280,
                        height=720,
                        duration=2,
                        mime_type="video/mp4",
                        file_size=200,
                    )
                ),
            ],
        ),
    )
    turn = _current_user_turn(message)
    video = BinaryContent(data=b"video", media_type="video/mp4")

    prompt = _current_user_prompt(message, {turn.attachments[1]: video})

    rendered = json.loads(prompt[0])
    assert [item["status"] for item in rendered["attachments"]] == [
        "missing",
        "available",
    ]
    assert prompt[1:] == [video]
