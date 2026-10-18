import asyncio
import base64
import hashlib
import hmac
import io
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

from derp.config import MediaLimits, Settings
from derp.tools import background, captions, effects, workers
from derp.tools.lookups import Lookups
from derp.tools.workers import EffectError, WorkerPool

POOL = WorkerPool()

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def picture(width: int = 400, height: int = 300, color: str = "teal") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def ffmpeg(*args: str) -> bytes:
    command = ["ffmpeg", "-v", "error", *args, "pipe:1"]
    return subprocess.run(command, check=True, capture_output=True).stdout  # noqa: S603 - fixed arguments


def clip(seconds: int = 2, *, sound: bool = True) -> bytes:
    audio = ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac", "-shortest"] if sound else []
    return ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}", *audio,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4",
    )  # fmt: skip


def size_of(jpeg: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(jpeg)).size


@pytest.mark.parametrize(
    ("style", "expected"),
    [("lobster", (400, 300)), ("demotivator", None), ("panel", None)],
)
def test_captions_keep_the_picture_and_frame_it(
    style: captions.Style, expected: tuple[int, int] | None
) -> None:
    result = captions.caption_image(picture(), "caption", style)
    width, height = size_of(result)
    if expected:
        assert (width, height) == expected
    else:
        assert width >= 400 and height > 300  # a frame or a panel adds room for the text


def test_long_and_multiline_text_still_fits() -> None:
    text = "a" * 250 + "\n" + "слово " * 20
    styles: tuple[captions.Style, ...] = ("demotivator", "lobster", "panel")
    for style in styles:
        frame = captions.caption_frame((320, 240), text, style)
        assert frame.overlay.width <= 320 + 200 and frame.overlay.height < 2000


def test_bad_inputs_are_refused_safely() -> None:
    with pytest.raises(EffectError, match="empty"):
        captions.caption_image(picture(), "  \n ", "panel")
    with pytest.raises(EffectError, match="not a readable image"):
        captions.caption_image(b"not an image", "x", "panel")
    with pytest.raises(EffectError, match="16 MP"):
        workers.check_dimensions(9000, 10)
    assert captions.media_size(100, 50) == (320, 160)  # small inputs are scaled up, sides even


def test_mirror_flips_left_to_right() -> None:
    source = Image.new("RGB", (320, 240), "white")
    source.paste((255, 0, 0), (0, 0, 10, 240))
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    flipped = Image.open(io.BytesIO(captions.mirror(buffer.getvalue()))).convert("RGB")
    red, _, _ = flipped.getpixel((flipped.width - 3, 120))  # type: ignore[misc]
    assert red > 200


def test_stickers_are_512_px_webp_within_the_size_cap() -> None:
    noise = Image.effect_noise((1600, 3000), 100).convert("RGB")  # hard to compress
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    data = captions.sticker(buffer.getvalue())
    sticker = Image.open(io.BytesIO(data))
    assert sticker.format == "WEBP" and sticker.size == (273, 512) and len(data) <= captions.STICKER_BYTES
    small = Image.open(io.BytesIO(captions.sticker(picture(64, 32))))
    assert small.size == (512, 256)


async def test_workers_are_killed_at_their_deadline_and_output_cap() -> None:
    with pytest.raises(EffectError, match="took too long"):
        await workers.run([sys.executable, "-c", "import time; time.sleep(5)"], seconds=0.3, max_output=1000)
    with pytest.raises(EffectError, match="output too large"):
        await workers.run([sys.executable, "-c", "print('x' * 100000)"], seconds=10, max_output=1000)
    with pytest.raises(EffectError, match="failed"):
        await workers.run([sys.executable, "-c", "raise SystemExit(3)"], seconds=10, max_output=1000)
    assert await workers.run([sys.executable, "-c", "print('ok')"], seconds=10, max_output=1000) == b"ok\n"


async def test_a_full_pool_turns_callers_away() -> None:
    pool = WorkerPool(MediaLimits(workers=1, queue=1))
    release = asyncio.Event()

    async def hold() -> None:
        async with pool.slot():
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(2)]  # one running, one queued
    await asyncio.sleep(0)
    with pytest.raises(workers.BusyError):
        async with pool.slot():
            pass
    release.set()
    await asyncio.gather(*holders)
    async with pool.slot():
        pass


@needs_ffmpeg
async def test_video_memes_keep_time_and_sound(tmp_path: Path) -> None:
    meme = tmp_path / "meme.mp4"
    await asyncio.to_thread(
        meme.write_bytes, await effects.meme_video(POOL, clip(), "caption", "demotivator")
    )
    info = await effects.probe(POOL, meme)
    assert info.audio and abs(info.seconds - 2) < 0.5
    assert info.width > 320 and info.height > 240 and info.width % 2 == info.height % 2 == 0


@needs_ffmpeg
async def test_reversing_and_its_limits() -> None:
    assert (await effects.reverse(POOL, clip()))[4:8] == b"ftyp"
    voice = ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus", "-f", "ogg")
    assert (await effects.reverse(POOL, voice, voice=True))[:4] == b"OggS"
    with pytest.raises(EffectError, match="too long"):
        await effects.reverse(POOL, clip(effects.MAX_REVERSE_SECONDS + 2, sound=False))
    with pytest.raises(EffectError):
        await effects.reverse(POOL, picture())
    with pytest.raises(EffectError, match="20 MB"):
        await effects.reverse(POOL, b"\x00" * (POOL.limits.max_input_bytes + 1))


@needs_ffmpeg
async def test_frames_and_samples_for_recognition() -> None:
    assert Image.open(io.BytesIO(await effects.still(POOL, clip()))).format == "JPEG"
    assert (await effects.audio_sample(POOL, clip()))[:4] == b"RIFF"
    with pytest.raises(effects.SilentError):
        await effects.audio_sample(POOL, clip(sound=False))


@needs_ffmpeg
async def test_speech_is_the_sound_of_any_clip_or_recording_as_ogg() -> None:
    memo = ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-f", "mp3")
    assert (await effects.speech(POOL, clip()))[:4] == (await effects.speech(POOL, memo))[:4] == b"OggS"
    with pytest.raises(effects.SilentError):
        await effects.speech(POOL, clip(sound=False))
    with pytest.raises(EffectError):
        await effects.speech(POOL, b"not a video")


def test_background_model_availability(tmp_path: Path) -> None:
    assert not background.available(tmp_path / "missing.onnx")
    fake = tmp_path / "fake.onnx"
    fake.write_bytes(b"x" * background.MODEL_BYTES)
    assert background.available(fake)
    with pytest.raises(ValueError, match="checksum"):
        background.verified(fake)


@pytest.mark.skipif(
    not background.available(Settings().background_model), reason="background model not fetched"
)
async def test_background_removal_makes_a_transparent_png() -> None:
    source = Image.new("RGB", (320, 240), "white")
    source.paste((200, 30, 30), (110, 70, 210, 170))
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    cutout = Image.open(
        io.BytesIO(await effects.remove_background(POOL, buffer.getvalue(), Settings().background_model))
    )
    assert cutout.mode == "RGBA" and cutout.size == (320, 240)


async def test_song_recognition_signs_requests_and_reads_results(settings: Settings) -> None:
    keyed = settings.model_copy(
        update={
            "acrcloud_host": "acr.example",
            "acrcloud_access_key": "KEY",
            "acrcloud_access_secret": "SECRET",
        }
    )
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        music = {
            "title": "Song", "artists": [{"name": "A"}, {"name": "B"}], "album": {"name": "LP"},
            "external_metadata": {"spotify": {"track": {"id": "sp1"}}, "youtube": {"vid": "yt1"}},
        }  # fmt: skip
        return httpx.Response(200, json={"status": {"code": 0}, "metadata": {"music": [music]}})

    tools = Lookups(keyed, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    text = await tools.identify_song(b"RIFFsample")
    assert text == (
        "A, B — Song\nAlbum: LP\nhttps://open.spotify.com/track/sp1\nhttps://www.youtube.com/watch?v=yt1\n(ACRCloud)"
    )
    [request] = seen
    body = request.content.decode(errors="replace")
    stamp = body.split('name="timestamp"\r\n\r\n')[1].split("\r\n")[0]
    expected = base64.b64encode(
        hmac.new(b"SECRET", f"POST\n/v1/identify\nKEY\naudio\n1\n{stamp}".encode(), hashlib.sha1).digest()
    ).decode()
    assert request.url.host == "acr.example" and expected in body and "SECRET" not in body

    nothing = Lookups(
        keyed,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": {"code": 1001}}))
        ),
    )
    assert await nothing.identify_song(b"x") == "No song recognized in that audio."


async def test_anime_search_reports_the_best_matches(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.trace.moe" and request.content == b"JPEG"
        hit = {
            "anilist": {"id": 7, "title": {"romaji": "Yuru Camp"}, "isAdult": False},
            "from": 5,
            "similarity": 0.9,
        }
        return httpx.Response(200, json={"result": [hit]})

    tools = Lookups(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert (await tools.identify_anime(b"JPEG")).startswith(
        "Yuru Camp at 0:05, similarity 90% https://anilist.co/anime/7"
    )

    weak = {"result": [{"anilist": {"id": 1, "title": {"romaji": "Zoids"}}, "similarity": 0.4}]}
    guessing = Lookups(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=weak))),
    )
    assert "No convincing match" in await guessing.identify_anime(b"JPEG")


async def test_the_pool_follows_the_configured_media_limits() -> None:
    with pytest.raises(EffectError, match="too large"):
        await effects.mirror(WorkerPool(MediaLimits(max_input_bytes=100)), picture())
    with pytest.raises(EffectError, match=r"0\.01 MP"):
        await effects.mirror(WorkerPool(MediaLimits(max_pixels=10_000)), picture())
    assert len(await effects.mirror(POOL, picture())) > 0
