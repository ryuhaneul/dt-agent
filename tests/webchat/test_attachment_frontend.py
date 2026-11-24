"""Incremental browser hashes are checked against NIST vectors and hashlib."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ductor_bot.i18n.loader import _load_toml
from ductor_bot.webchat.web.app import CSP, STARFLOW_CSP

STATIC = Path(__file__).resolve().parents[2] / "ductor_bot/webchat/web/static"
NODE_HASH = r"""
import fs from 'node:fs';
const {source,path,stride}=JSON.parse(fs.readFileSync(0,'utf8'));
const {Sha256}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(source)).toString('base64'));
const fd=fs.openSync(path,'r'), chunk=Buffer.alloc(stride), hash=new Sha256();
let count;
while((count=fs.readSync(fd,chunk,0,chunk.length,null)))hash.update(chunk.subarray(0,count));
fs.closeSync(fd);const first=hash.hex();if(hash.hex()!==first)throw Error('unstable_digest');
console.log(first);
"""


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
        (
            b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
        ),
        (b"a" * 1_000_000, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"),
    ],
)
@pytest.mark.parametrize("stride", [1, 55, 64, 65, 1024 * 1024])
def test_sha256_nist_vectors(tmp_path, payload, expected, stride):
    assert hashlib.sha256(payload).hexdigest() == expected
    assert run_hash(tmp_path, payload, stride) == expected


def run_hash(tmp_path, payload, stride):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser hash checks")
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)
    result = subprocess.run(
        [node, "--input-type=module", "-e", NODE_HASH],
        input=json.dumps(
            {
                "source": str(STATIC / "js/sha256.js"),
                "path": str(path),
                "stride": stride,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    "stride", [7, 56, 63, 64, 65, 1024 * 1024 - 1, 1024 * 1024 + 1, 8 * 1024 * 1024]
)
def test_sha256_binary_chunk_boundaries(tmp_path, stride):
    repeats = 32769 if stride == 8 * 1024 * 1024 else 4097
    payload = bytes(range(256)) * repeats + b"\x00\xffboundary"
    assert run_hash(tmp_path, payload, stride) == hashlib.sha256(payload).hexdigest()


def image_size_fixtures(directory):
    rng = random.Random(23)
    capture = Image.new("RGB", (1600, 900), "#f5f7f9")
    draw = ImageDraw.Draw(capture)
    draw.rectangle((0, 0, 1600, 65), fill="#083344")
    for top in range(92, 862, 56):
        draw.rounded_rectangle((165, top, 1460, top + 47), 8, fill="#ffffff", outline="#dde9eb")
        for _ in range(65):
            left = rng.randrange(190, 1380)
            y = top + rng.choice((10, 23, 36))
            draw.line((left, y, left + rng.randrange(4, 31), y),
                      fill=rng.choice(("#083344", "#475569", "#0e7490")))
    capture.save(directory / "capture.png", optimize=True)

    size = (1600, 1200)
    noise = random.Random(42).randbytes(size[0] * size[1])
    pixels = noise.translate(bytes(96 + value // 4 for value in range(256)))
    Image.frombytes("L", size, pixels).convert("RGB").save(directory / "low-quality.jpg", quality=40)


def test_attachment_browser_contract(tmp_path):
    module = os.environ.get("DTW_PLAYWRIGHT_MODULE")
    if not module:
        pytest.skip("Set DTW_PLAYWRIGHT_MODULE for optional real browser attachment checks")
    image_size_fixtures(tmp_path)
    image = Image.effect_noise((2400, 1600), 25).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((220, 240, 2100, 1350), radius=100, fill="#0b5f58")
    draw.rectangle((400, 420, 1800, 510), fill="#5ebfb4")
    draw.rectangle((400, 670, 1550, 730), fill="#f4f4f2")
    draw.rectangle((400, 850, 1750, 910), fill="#f4f4f2")
    image.save(tmp_path / "sample.png")
    image.save(tmp_path / "sample.jpg", quality=94)
    for name, size in (("scale-large.png", (3840, 2160)), ("scale-small.png", (500, 200))):
        Image.new("RGB", size, "#0b5f58").save(tmp_path / name)
    exif = Image.Exif()
    exif[274] = 6  # Rotate clockwise when displayed.
    Image.new("RGB", (400, 200), "#0b5f58").save(tmp_path / "rotated.jpg", exif=exif)
    small = Image.new("RGB", (48, 36), "blue")
    small.save(tmp_path / "sample.gif")
    small.save(tmp_path / "sample.bmp")
    (tmp_path / "notes.txt").write_text("clipboard file\n", encoding="utf8")
    configuration = {
        "static": str(STATIC),
        "directory": str(tmp_path),
        "shots": os.environ.get("DTW_ATTACH_SHOTS", str(tmp_path / "shots")),
        "csp": CSP,
        "scenario": os.environ.get("DTW_ATTACH_SCENARIO", "4a"),
        "scaleShots": os.environ.get("DTW_SCALE_SHOTS", str(tmp_path / "shots")),
        "strings": _load_toml(STATIC.parents[2] / "i18n/ko/web.toml"),
    }
    config = tmp_path / "browser.json"
    config.write_text(json.dumps(configuration, ensure_ascii=False))
    result = subprocess.run(
        [shutil.which("node"), "tests/webchat/attachment_browser.mjs", str(config)],
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(evidence["screens"]) == 6
    assert evidence["emptyPage"] == {"entries": 100, "attachmentRequests": 0}
    assert [item["mode"] for item in evidence["uploads"]] == [
        "original",
        "resize",
        "compress",
        "resize-compress",
    ]
    for result in evidence["uploads"]:
        stored = tmp_path / result["filename"]
        assert stored.stat().st_size == result["size"]
        with Image.open(stored) as actual:
            assert (
                actual.format
                == {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[result["mime"]]
            )
            expected = (
                (1200, 800) if result["mode"] in {"resize", "resize-compress"} else (2400, 1600)
            )
            assert actual.size == expected
        if result["mode"] == "original":
            assert stored.read_bytes() == (tmp_path / "sample.png").read_bytes()
    print(json.dumps(evidence, ensure_ascii=False))


def test_message_image_browser_geometry(tmp_path):
    if not os.environ.get("DTW_PLAYWRIGHT_MODULE"):
        pytest.skip("Set DTW_PLAYWRIGHT_MODULE for message image browser checks")
    sizes = {"wide": (1600, 600), "square": (800, 800), "portrait": (600, 1600), "small": (120, 80)}
    for name, (width, height) in sizes.items():
        image = Image.new("RGB", (width, height), "#123f4b")
        draw = ImageDraw.Draw(image)
        inset = max(3, min(width, height) // 16)
        draw.rectangle((inset, inset, width - inset, height - inset), outline="#70d4c7", width=inset)
        radius = min(width, height) // 5
        x, y = width // 2, height // 2
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="#f4f4f2")
        image.thumbnail((512, 512))
        image.save(tmp_path / f"{name}.png")

    config = tmp_path / "message-image.json"
    config.write_text(
        json.dumps(
            {
                "static": str(STATIC),
                "directory": str(tmp_path),
                "shots": os.environ.get("DTW_IMAGE_SHOTS", str(tmp_path / "shots")),
                "csp": CSP,
                "starflowCsp": STARFLOW_CSP,
                "strings": _load_toml(STATIC.parents[2] / "i18n/ko/web.toml"),
                "sizes": sizes,
            }
        )
    )
    result = subprocess.run(
        [shutil.which("node"), "tests/webchat/message_image_browser.mjs", str(config)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
