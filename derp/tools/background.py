"""Background removal with the U²-NetP model (Apache-2.0, 4.6 MB) on CPU.

The weights are fetched once, explicitly, at image build time
(``python -m derp.tools.background fetch``) and checksum-verified on every
use; a user request never downloads them, and the tool hides itself while
they are missing. Inference runs as ``python -m derp.tools.background SRC
MODEL`` in a separate process with CPU and memory limits and one thread, and
writes the PNG to stdout.
"""

import hashlib
import importlib.util
import io
import os
import sys
import urllib.request
from pathlib import Path

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
MODEL_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
MODEL_BYTES = 4_574_861
MAX_SIDE = 2048
SIDE = 320  # the model's input resolution


def available(model: Path) -> bool:
    return (
        model.is_file()
        and model.stat().st_size == MODEL_BYTES
        and importlib.util.find_spec("onnxruntime") is not None
    )


def verified(model: Path) -> bytes:
    data = model.read_bytes()
    if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("background model checksum mismatch")
    return data


def fetch(model: Path) -> None:
    """Build-time download; inference never calls this."""
    model.parent.mkdir(parents=True, exist_ok=True)
    partial = model.with_suffix(".download")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
            partial.write_bytes(response.read(MODEL_BYTES + 1))
        verified(partial)
        partial.replace(model)
    finally:
        partial.unlink(missing_ok=True)


def _limit() -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))


def render(source: Path, model: Path) -> bytes:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageChops

    from derp.tools.captions import open_image

    image = open_image(source.read_bytes())
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    session = ort.InferenceSession(verified(model), sess_options=options, providers=["CPUExecutionProvider"])
    pixels = np.asarray(image.convert("RGB").resize((SIDE, SIDE), Image.Resampling.LANCZOS), dtype=np.float32)
    pixels /= max(float(pixels.max()), 1e-6)
    pixels -= np.array([0.485, 0.456, 0.406], dtype=np.float32)
    pixels /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
    output = np.asarray(session.run(None, {session.get_inputs()[0].name: pixels.transpose(2, 0, 1)[None]})[0])
    if output.shape != (1, 1, SIDE, SIDE) or not np.isfinite(output).all():
        raise ValueError("invalid mask")
    mask = output[0, 0]
    low, high = float(mask.min()), float(mask.max())
    if high - low <= 1e-6:
        raise ValueError("empty mask")
    alpha = Image.fromarray(((mask - low) * (255 / (high - low))).clip(0, 255).astype(np.uint8))
    image.putalpha(
        ImageChops.multiply(image.getchannel("A"), alpha.resize(image.size, Image.Resampling.LANCZOS))
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    if sys.argv[1:2] == ["fetch"] and len(sys.argv) == 3:
        fetch(Path(sys.argv[2]))
    elif len(sys.argv) == 3:
        _limit()
        sys.stdout.buffer.write(render(Path(sys.argv[1]), Path(sys.argv[2])))
    else:
        raise SystemExit("usage: python -m derp.tools.background (fetch MODEL | SOURCE MODEL)")
