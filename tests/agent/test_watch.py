import io
import zlib

from PIL import Image

from derp.agent.watch import Look, label, medium, pdf_pages
from derp.history.models import MessageMedia


def row(kind: str, **fields: object) -> MessageMedia:
    return MessageMedia(
        chat_id=1, message_id=7, idx=0, type=kind, file_id="f", file_unique_id="u-f", bot_id=1, **fields
    )


def test_clips_audio_and_pdfs_go_to_the_media_pin_and_nothing_else() -> None:
    assert medium(row("video")) == medium(row("animation")) == medium(row("video_note")) == "clip"
    assert medium(row("sticker", mime_type="video/webm")) == "clip"
    assert medium(row("audio", mime_type="audio/mpeg")) == medium(row("voice")) == "audio"
    assert medium(row("document", mime_type="application/pdf")) == "pdf"
    assert medium(row("sticker", mime_type="application/x-tgsticker")) is None
    assert medium(row("document", mime_type="application/zip")) is None
    assert medium(row("photo")) is None


def test_lines_name_the_file_and_say_why_it_was_not_opened() -> None:
    clip = Look(row("video", duration=95), "clip", why="longer than the 1:00 this answer can take")
    assert clip.line() == "#7 (video 1:35): not watched — longer than the 1:00 this answer can take."
    paper = Look(row("document", file_name="a.pdf"), "pdf", notes="A thesis.\n(p. 2) Results", pages=3)
    assert paper.line() == "#7 (file a.pdf, 3 pages, read):\nA thesis.\n(p. 2) Results"
    assert label(row("voice", duration=7)) == "voice 0:07"


def test_pdf_pages_are_counted_in_the_file_and_in_compressed_object_streams() -> None:
    pages = [Image.new("RGB", (60, 80), "white") for _ in range(3)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    assert pdf_pages(buffer.getvalue()) == 3
    objects = zlib.compress(b"3 0 4 20 << /Type /Page /Parent 2 0 R >> << /Type /Page /Parent 2 0 R >>")
    stream = (
        f"5 0 obj << /Type /ObjStm /N 2 /First 8 /Filter /FlateDecode /Length {len(objects)} >>\nstream\n"
    )
    packed = (
        b"%PDF-1.5\n2 0 obj << /Type /Pages /Count 2 >> endobj\n"
        + stream.encode()
        + objects
        + b"\nendstream endobj\n"
    )
    assert pdf_pages(packed) == 2  # the /Pages node itself isn't a page
    assert pdf_pages(b"%PDF-1.7\nnothing readable") is None
