"""Tests for image validation and the four operations (no AWS involved).

These are UNIT tests: each one calls a single function from imgproc/image_ops.py with
an image generated in memory and checks the result. Nothing is mocked because this
module has no external dependencies.
"""

import io
import struct
import zlib

import pytest
from PIL import Image

from conftest import make_image, open_bytes
from imgproc import image_ops
from imgproc.image_ops import (
    ImageValidationError,
    fit_dimensions,
    load_image,
    pixel_grid,
    process,
)

RESIZE = {"maxWidth": 600, "maxHeight": 400, "keepAspectRatio": True}


# ------------------------------------------------------------------ resize

# INTERVIEW:
# @pytest.mark.parametrize runs the same test once per row of inputs - an easy way to
# cover many cases (normal, wide, tall, already-small) without copy-pasting tests.
@pytest.mark.parametrize(
    "size, expected",
    [
        ((1200, 800), (600, 400)),   # same ratio as the box
        ((2000, 500), (600, 150)),   # wide panorama: width-limited
        ((500, 2000), (100, 400)),   # tall: height-limited
        ((300, 200), (300, 200)),    # already small: never enlarged
        ((4000, 3000), (533, 400)),
    ],
)
def test_fit_dimensions_preserves_aspect_ratio(size, expected):
    assert fit_dimensions(*size, 600, 400) == expected


def test_resize_output_fits_bounds_and_keeps_ratio():
    loaded = load_image(make_image("JPEG", (1600, 900)))
    out = process(loaded, {"resize": RESIZE})
    img = open_bytes(out.data)
    assert img.size == (out.width, out.height) == (600, 338)
    assert abs(img.width / img.height - 1600 / 900) < 0.01
    assert out.details["resize"]["outputWidth"] == 600


def test_resize_without_aspect_ratio_uses_exact_size():
    loaded = load_image(make_image("PNG", (1000, 1000)))
    out = process(loaded, {"resize": {"maxWidth": 300, "maxHeight": 100, "keepAspectRatio": False}})
    assert (out.width, out.height) == (300, 100)


def test_resize_tiny_extreme_ratio_never_zero():
    loaded = load_image(make_image("PNG", (5000, 2), pattern=False))
    out = process(loaded, {"resize": RESIZE})
    assert out.width == 600 and out.height >= 1


# ------------------------------------------------------------------ grayscale

def test_grayscale_jpeg_is_single_channel():
    out = process(load_image(make_image("JPEG")), {"grayscale": {}})
    img = open_bytes(out.data)
    assert img.mode == "L" and img.format == "JPEG"


# Transparent pixels must stay transparent after grayscale (LA mode keeps alpha).
def test_grayscale_png_keeps_transparency():
    data = make_image("PNG", (100, 80), mode="RGBA", color=(255, 0, 0, 0), pattern=False)
    out = process(load_image(data), {"grayscale": {}})
    img = open_bytes(out.data)
    assert img.mode == "LA"
    assert img.getchannel("A").getextrema() == (0, 0)  # still fully transparent
    assert out.details["grayscale"]["alphaPreserved"] is True


def test_palette_png_with_transparency_is_handled():
    img = Image.new("P", (50, 50), 0)
    img.putpalette([255, 0, 0] + [0, 255, 0] * 255)
    buf = io.BytesIO()
    img.save(buf, "PNG", transparency=0)
    loaded = load_image(buf.getvalue())
    assert loaded.image.mode == "RGBA"
    out = process(loaded, {"grayscale": {}, "pixelate": {"level": 8}})
    assert open_bytes(out.data).mode == "LA"


# ------------------------------------------------------------------ compression

def test_jpeg_quality_reduces_size():
    loaded = load_image(make_image("JPEG", (800, 600)))
    low = process(loaded, {"compress": {"quality": 10}})
    high = process(loaded, {"compress": {"quality": 100}})
    assert len(low.data) < len(high.data)
    assert low.details["compress"] == {"method": "jpeg-quality", "quality": 10}


# "Lossless" is checked literally: every pixel before and after must be identical.
def test_png_compression_is_lossless():
    data = make_image("PNG", (300, 200))
    loaded = load_image(data)
    out = process(loaded, {"compress": {}})
    assert out.details["compress"] == {"method": "png-lossless"}
    original = open_bytes(data).convert("RGB")
    result = open_bytes(out.data).convert("RGB")
    assert original.tobytes() == result.tobytes()  # pixel-identical


def test_png_optimization_drops_opaque_alpha_losslessly():
    data = make_image("PNG", (200, 200), mode="RGBA", color=(1, 2, 3, 255))
    out = process(load_image(data), {"compress": {}})
    img = open_bytes(out.data)
    assert img.mode == "RGB"
    assert img.tobytes() == open_bytes(data).convert("RGB").tobytes()


# ------------------------------------------------------------------ pixelation

# Pixelation at the minimum, default and maximum levels: proportions are preserved
# and the image really is made of uniform blocks.
@pytest.mark.parametrize("level", [8, 16, 128])
def test_pixelation_levels(level):
    loaded = load_image(make_image("PNG", (1024, 512)))
    out = process(loaded, {"pixelate": {"level": level}})
    img = open_bytes(out.data)
    assert img.size == (1024, 512)  # proportions preserved, not squared
    assert out.details["pixelate"] == {"level": level, "gridWidth": level, "gridHeight": level // 2}
    # The image is made of uniform blocks: every block of width 1024/level is one colour.
    block = 1024 // level
    row = [img.getpixel((x, 0)) for x in range(block)]
    assert len(set(row)) == 1


def test_lower_level_is_more_pixelated():
    loaded = load_image(make_image("PNG", (512, 512)))
    coarse = open_bytes(process(loaded, {"pixelate": {"level": 8}}).data)
    fine = open_bytes(process(loaded, {"pixelate": {"level": 128}}).data)
    assert len(coarse.getcolors(1 << 24)) <= len(fine.getcolors(1 << 24))


@pytest.mark.parametrize(
    "size, level, grid",
    [
        ((1000, 500), 16, (16, 8)),
        ((500, 1000), 16, (8, 16)),
        ((10, 6), 16, (10, 6)),     # smaller than the grid: clamped, no upscaling
        ((300, 1), 8, (8, 1)),      # extreme ratio never yields a zero-sized grid
        ((7, 7), 128, (7, 7)),
    ],
)
def test_pixel_grid_small_and_non_square(size, level, grid):
    assert pixel_grid(*size, level) == grid


def test_pixelate_tiny_image_works():
    out = process(load_image(make_image("JPEG", (5, 3))), {"pixelate": {"level": 16}})
    assert (out.width, out.height) == (5, 3)


# ------------------------------------------------------------------ combined

# Even when operations are listed in reverse, they run in the documented order.
def test_combined_operations_apply_in_fixed_order():
    loaded = load_image(make_image("JPEG", (1200, 900)))
    ops = {
        "pixelate": {"level": 16},
        "compress": {"quality": 50},
        "grayscale": {},
        "resize": RESIZE,
    }
    out = process(loaded, ops)
    assert out.details["appliedOrder"] == ["resize", "grayscale", "compress", "pixelate"]
    img = open_bytes(out.data)
    assert img.size == (533, 400)  # resize ran first
    assert img.mode == "L"
    # pixelation grid is computed on the resized image
    assert out.details["pixelate"]["gridWidth"] == 16
    assert out.details["pixelate"]["gridHeight"] == 12


def test_only_selected_operations_are_applied():
    loaded = load_image(make_image("PNG", (640, 480)))
    out = process(loaded, {"grayscale": {}})
    assert out.details["appliedOrder"] == ["grayscale"]
    assert (out.width, out.height) == (640, 480)
    assert "resize" not in out.details and "pixelate" not in out.details


def test_output_keeps_original_format():
    assert open_bytes(process(load_image(make_image("PNG")), {"grayscale": {}}).data).format == "PNG"
    assert open_bytes(process(load_image(make_image("JPEG")), {"grayscale": {}}).data).format == "JPEG"


# ------------------------------------------------------------------ invalid input
# Security-relevant tests: wrong types, corrupted files, oversized files and
# decompression bombs must all be rejected with a clear ImageValidationError.

def test_empty_file_rejected():
    with pytest.raises(ImageValidationError, match="empty"):
        load_image(b"")


def test_unsupported_format_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "GIF")
    with pytest.raises(ImageValidationError, match="Unsupported"):
        load_image(buf.getvalue())


def test_text_file_renamed_as_image_rejected():
    with pytest.raises(ImageValidationError, match="Unsupported"):
        load_image(b"hello, I am not an image")


def test_corrupted_jpeg_rejected():
    data = make_image("JPEG", (400, 300))
    with pytest.raises(ImageValidationError, match="corrupted"):
        load_image(data[: len(data) // 3])


def test_corrupted_png_rejected():
    data = bytearray(make_image("PNG", (100, 100)))
    data[40:60] = b"\x00" * 20  # break a chunk + its CRC
    with pytest.raises(ImageValidationError):
        load_image(bytes(data))


def test_oversized_file_rejected():
    data = b"\xff\xd8\xff" + b"\x00" * image_ops.MAX_FILE_BYTES
    with pytest.raises(ImageValidationError, match="10 MB"):
        load_image(data)


# Builds a tiny, valid-looking PNG whose header claims huge dimensions, imitating a
# decompression bomb without actually needing gigabytes of memory in the test.
def _png_header_only(width, height) -> bytes:
    """A tiny PNG that *claims* huge dimensions (decompression-bomb style)."""
    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


def test_decompression_bomb_dimensions_rejected():
    with pytest.raises(ImageValidationError, match="too large|too many pixels"):
        load_image(_png_header_only(50_000, 50_000))


def test_too_many_pixels_rejected():
    with pytest.raises(ImageValidationError, match="too many pixels"):
        load_image(_png_header_only(9000, 9000))  # 81 MP > 40 MP limit


def test_exif_orientation_is_applied():
    img = Image.new("RGB", (40, 20), (255, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90 degrees
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    loaded = load_image(buf.getvalue())
    assert (loaded.width, loaded.height) == (20, 40)
