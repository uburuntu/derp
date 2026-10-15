from derp.telegram.media import sniff_image


def test_sniff_image_recognizes_common_formats_only() -> None:
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image(b"\x1aE\xdf\xa3webm") is None  # video stickers are not images
    assert sniff_image(b"") is None
