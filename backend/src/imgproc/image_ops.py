"""Image validation and processing with Pillow.

Operations are always applied in this fixed order (only the selected ones):
    1. resize  2. grayscale  3. compress  4. pixelate

This module knows nothing about AWS. processor.py downloads the bytes from S3, calls
load_image() and process() here, and uploads the returned bytes to the output bucket.
Keeping it AWS-free makes it easy to unit test (tests/test_image_ops.py).
"""

from __future__ import annotations

# io.BytesIO lets Pillow read/write images in memory, so we never touch the disk
# (Lambda only has a small /tmp folder, and memory is faster anyway).
import io
import warnings
from dataclasses import dataclass, field

# INTERVIEW:
# Pillow (imported as PIL) is the standard Python imaging library. It contains
# compiled C code, so the Lambda package must contain the *Linux* build of Pillow,
# not the Windows one - see scripts/build_lambda.ps1.
from PIL import Image, ImageOps, UnidentifiedImageError

from .limits import MAX_FILE_BYTES  # noqa: F401 - re-exported for callers
MAX_DIMENSION = 10_000  # max width or height in pixels
MAX_PIXELS = 40_000_000  # 40 megapixels (decompression-bomb guard)

# INTERVIEW:
# A "decompression bomb" is a tiny file (e.g. a few KB PNG) that claims enormous
# dimensions and expands to gigabytes of memory when decoded, crashing the server.
# We cap width, height and total pixels BEFORE decoding pixel data.
# Backstop for Pillow's own decompression-bomb detection.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# The fixed, documented order in which selected operations are applied.
OPERATION_ORDER = ("resize", "grayscale", "compress", "pixelate")

# Output keeps the input format: file extension and Content-Type for each format.
FORMAT_INFO = {
    "JPEG": {"ext": "jpg", "content_type": "image/jpeg"},
    "PNG": {"ext": "png", "content_type": "image/png"},
}

DEFAULT_JPEG_QUALITY = 92  # used when the user did not select compression
DEFAULT_COMPRESS_QUALITY = 75


# A dedicated exception type for "bad input". processor.py treats it as a permanent
# failure (job FAILED, no retry), unlike AWS errors which are retried.
class ImageValidationError(ValueError):
    """The uploaded file is not an acceptable image. Retrying will not help."""


# The decoded original image plus facts about it (shown to the user as "Original").
@dataclass
class LoadedImage:
    image: Image.Image
    format: str  # "JPEG" or "PNG"
    width: int
    height: int
    size_bytes: int


# One processed result: the encoded bytes and metadata saved in DynamoDB.
@dataclass
class ProcessedImage:
    data: bytes
    format: str
    width: int
    height: int
    details: dict = field(default_factory=dict)


# INTERVIEW:
# Never trust the file name or the browser's MIME type - both are controlled by the
# client. "Magic bytes" are the first bytes of the file and reveal the real format:
# JPEG starts with FF D8 FF, PNG with 89 50 4E 47 0D 0A 1A 0A.
def detect_format(data: bytes) -> str | None:
    """Identify the real file type from its magic bytes, ignoring the filename."""
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    return None


# Reject images that are too wide, too tall or have too many pixels in total.
def check_dimensions(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise ImageValidationError("The image has invalid dimensions.")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ImageValidationError(
            f"The image is too large ({width}x{height}). "
            f"Maximum width and height is {MAX_DIMENSION} pixels."
        )
    if width * height > MAX_PIXELS:
        raise ImageValidationError(
            f"The image has too many pixels ({width * height:,}). "
            f"Maximum is {MAX_PIXELS:,} pixels."
        )


def load_image(data: bytes) -> LoadedImage:
    """Validate raw bytes and decode them into a normalized Pillow image."""
    # Step 1: cheap checks on the raw bytes (empty / too big).
    if not data:
        raise ImageValidationError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ImageValidationError("The file is larger than the 10 MB limit.")

    # Step 2: real file type from the content, not the name.
    fmt = detect_format(data)
    if fmt is None:
        raise ImageValidationError(
            "Unsupported file type. Supported formats: JPG, JPEG, PNG."
        )

    # Step 3: let Pillow inspect and decode the file. Pillow's decompression-bomb
    # *warning* is turned into an *error* so such files are rejected, not processed.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            # First pass: read only the header to check dimensions cheaply,
            # then verify() the file structure without decoding pixels.
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != fmt:
                    raise ImageValidationError("The file content is not a valid image.")
                check_dimensions(*probe.size)
                probe.verify()
            # Second pass: fully decode (verify() leaves the image unusable).
            # load() forces Pillow to read every pixel, which catches truncated files.
            image = Image.open(io.BytesIO(data))
            image.load()
        except ImageValidationError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ImageValidationError("The image has too many pixels to process safely.")
        # Any decoding problem becomes one friendly message for the user.
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
            raise ImageValidationError("The image file is corrupted or cannot be read.")

    # Phone photos often store "rotate 90°" in EXIF metadata instead of rotating the
    # pixels. exif_transpose applies that rotation so outputs are the right way up.
    image = ImageOps.exif_transpose(image)
    image = normalize_mode(image, fmt)
    return LoadedImage(
        image=image,
        format=fmt,
        width=image.width,
        height=image.height,
        size_bytes=len(data),
    )


# Images come in many pixel "modes" (palette, 16-bit, 1-bit...). Converting them to a
# small set of standard modes means every operation below works the same way.
# RGB = colour, L = grayscale, the "A" versions (RGBA, LA) also have transparency.
def normalize_mode(image: Image.Image, fmt: str) -> Image.Image:
    """Convert unusual pixel modes into RGB/RGBA/L/LA so every operation works."""
    # JPEG never has transparency, so RGB or L is enough (CMYK JPEGs become RGB).
    if fmt == "JPEG":
        return image if image.mode in ("RGB", "L") else image.convert("RGB")

    # PNG: keep transparency wherever the file has it.
    has_transparency = "transparency" in image.info
    if image.mode in ("RGBA", "LA"):
        return image
    if image.mode in ("RGB", "L") and not has_transparency:
        return image
    if image.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N"):
        # 16-bit grayscale PNG -> 8-bit grayscale
        return image.convert("I").point(lambda v: v / 256).convert("L")
    if image.mode == "1":
        return image.convert("L")
    # Palette images with a transparent colour become RGBA so the transparency survives.
    if image.mode == "PA" or has_transparency:
        return image.convert("RGBA")
    return image.convert("RGB")


# ---------------------------------------------------------------- operations


# INTERVIEW:
# Aspect-ratio-preserving resize: use ONE scale factor for both sides, the smaller
# of (maxW / w) and (maxH / h), so the image fits inside the box without stretching.
# Capping the factor at 1.0 means small images are never enlarged (which would blur).
# Example: 1600x1000 into 600x400 -> scale = min(0.375, 0.4) = 0.375 -> 600x375.
def fit_dimensions(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    """Largest size that fits inside the bounds, keeping aspect ratio, never enlarging."""
    scale = min(max_width / width, max_height / height, 1.0)
    # max(1, ...) prevents a 0-pixel side for extreme shapes like 5000x2.
    new_w = min(max_width, max(1, round(width * scale)))
    new_h = min(max_height, max(1, round(height * scale)))
    return new_w, new_h


# Operation 1: resize. keep_aspect_ratio=False resizes to the exact box (may stretch).
def resize(image: Image.Image, max_width: int, max_height: int, keep_aspect_ratio: bool = True) -> Image.Image:
    if keep_aspect_ratio:
        size = fit_dimensions(image.width, image.height, max_width, max_height)
    else:
        size = (max_width, max_height)
    if size == image.size:
        return image.copy()
    # LANCZOS is a high-quality downscaling filter (sharper than bilinear).
    return image.resize(size, Image.Resampling.LANCZOS)


# Operation 2: grayscale.
def grayscale(image: Image.Image) -> Image.Image:
    # LA keeps the alpha channel so transparent PNGs stay transparent.
    if image.mode in ("RGBA", "LA"):
        return image.convert("LA")
    return image.convert("L")


# Operation 3: compression.
# INTERVIEW:
# JPEG is "lossy": a lower quality value throws away detail to make the file smaller.
# PNG is "lossless": it can only be re-packed more efficiently, so pixels never change
# and the file may not get much smaller. That is why the UI hides the quality slider
# for PNG and why we never promise a size reduction.
def compress(image: Image.Image, fmt: str, quality: int) -> Image.Image:
    """JPEG: apply lossy compression now so later steps see its effect.

    PNG: pixels are unchanged; the lossless optimization happens when encoding.
    """
    if fmt != "JPEG":
        return image
    # Encode to JPEG in memory and decode again, so the compression artefacts are
    # "baked in" before pixelation runs (keeps the documented operation order true).
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality, optimize=True)
    buf.seek(0)
    compressed = Image.open(buf)
    compressed.load()
    return compressed


# INTERVIEW:
# The pixelation level is the number of blocks along the LONGER side; the shorter
# side gets proportionally fewer blocks, so the image is never squashed into a square.
# Example: 1000x500 at level 16 -> a 16x8 grid. Small images are clamped so we never
# ask for more blocks than pixels.
def pixel_grid(width: int, height: int, level: int) -> tuple[int, int]:
    """Grid size for pixelation: `level` cells along the longer side."""
    if width >= height:
        grid_w = min(level, width)
        grid_h = max(1, round(grid_w * height / width))
    else:
        grid_h = min(level, height)
        grid_w = max(1, round(grid_h * width / height))
    return grid_w, grid_h


# Operation 4: pixelation.
def pixelate(image: Image.Image, level: int) -> Image.Image:
    grid = pixel_grid(image.width, image.height, level)
    # Shrink to the tiny grid (BOX averages the colours inside each block)...
    small = image.resize(grid, Image.Resampling.BOX)
    # ...then blow it back up with NEAREST, which copies each pixel into a solid
    # square instead of smoothing - that is what creates the blocky look.
    return small.resize(image.size, Image.Resampling.NEAREST)


# ---------------------------------------------------------------- encoding


# Turn the final Pillow image into file bytes in the ORIGINAL format.
def encode(image: Image.Image, fmt: str, compress_options: dict | None) -> bytes:
    """Save the image in its original format. Metadata (EXIF etc.) is not copied.

    compress_options is None when compression was not selected; {} means PNG lossless.
    """
    buf = io.BytesIO()
    if fmt == "JPEG":
        # Note the `is not None` check: an empty dict {} is "falsy" in Python, and
        # treating it as "not selected" was a real bug caught by the tests.
        if compress_options is not None:
            quality = compress_options.get("quality") or DEFAULT_COMPRESS_QUALITY
        else:
            quality = DEFAULT_JPEG_QUALITY
        image.save(buf, "JPEG", quality=quality, optimize=True)
    else:
        if compress_options is not None:
            # Lossless: drop a fully opaque alpha channel, then max zlib effort.
            if image.mode in ("RGBA", "LA") and image.getchannel("A").getextrema() == (255, 255):
                image = image.convert("RGB" if image.mode == "RGBA" else "L")
            image.save(buf, "PNG", optimize=True)
        else:
            image.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


# The main entry point used by processor.py for every result image.
def process(loaded: LoadedImage, operations: dict) -> ProcessedImage:
    """Apply the selected operations in OPERATION_ORDER and encode the result.

    `operations` maps operation name -> options, e.g.
        {"resize": {"maxWidth": 600, "maxHeight": 400, "keepAspectRatio": True},
         "pixelate": {"level": 16}}
    Operations missing from the dict are skipped.
    """
    image = loaded.image
    fmt = loaded.format
    # `details` is stored in DynamoDB and shown in the UI ("grid 16x10", "quality 40"...).
    details: dict = {"appliedOrder": [op for op in OPERATION_ORDER if op in operations]}

    # Each `if` below runs only when that operation was selected, and the order of the
    # `if` blocks IS the fixed processing order: resize -> grayscale -> compress -> pixelate.
    if "resize" in operations:
        opts = operations["resize"]
        image = resize(image, opts["maxWidth"], opts["maxHeight"], opts["keepAspectRatio"])
        details["resize"] = {
            "maxWidth": opts["maxWidth"],
            "maxHeight": opts["maxHeight"],
            "keepAspectRatio": opts["keepAspectRatio"],
            "outputWidth": image.width,
            "outputHeight": image.height,
        }
    if "grayscale" in operations:
        image = grayscale(image)
        details["grayscale"] = {"alphaPreserved": image.mode == "LA"}
    if "compress" in operations:
        quality = operations["compress"].get("quality") or DEFAULT_COMPRESS_QUALITY
        image = compress(image, fmt, quality)
        details["compress"] = (
            {"method": "jpeg-quality", "quality": quality}
            if fmt == "JPEG"
            else {"method": "png-lossless"}
        )
    if "pixelate" in operations:
        level = operations["pixelate"]["level"]
        # The grid is computed on the CURRENT image, i.e. after any resize.
        grid_w, grid_h = pixel_grid(image.width, image.height, level)
        image = pixelate(image, level)
        details["pixelate"] = {"level": level, "gridWidth": grid_w, "gridHeight": grid_h}

    data = encode(image, fmt, operations.get("compress"))
    return ProcessedImage(data=data, format=fmt, width=image.width, height=image.height, details=details)
