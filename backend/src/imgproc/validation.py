"""Validation of API request bodies and processing parameters.

Used by job_api.py when a job is created (POST /api/jobs). The normalized operations
dict returned here is stored in DynamoDB and later read by processor.py, so the
processor can trust its shape. The frontend (frontend/src/options.js) checks the same
rules, but only for user experience - this module is the real gatekeeper.
"""

from __future__ import annotations

import re
# DynamoDB returns numbers as Decimal, so validation must accept Decimal too.
from decimal import Decimal

from .limits import MAX_FILE_BYTES

# Allowed ranges for every user-controlled number (documented in the README).
RESIZE_MIN, RESIZE_MAX = 1, 5000
QUALITY_MIN, QUALITY_MAX = 10, 100
PIXEL_MIN, PIXEL_MAX, PIXEL_STEP = 8, 128, 8

# Which file extensions map to which content types, and which content types we accept.
EXTENSION_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
CONTENT_TYPE_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}

# Job IDs are UUID version 4 strings. Checking the exact format before using an ID
# blocks junk input (for example "../../etc/passwd") from reaching DynamoDB or S3 keys.
JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


# job_api.py catches this exception and turns it into an HTTP 400 response.
class ValidationError(ValueError):
    """A request parameter is invalid (maps to HTTP 400)."""


# Strict whole-number check with a range. JSON numbers arrive as int or float,
# DynamoDB numbers as Decimal; "16" (a string) or 16.5 are rejected.
def _int(value, name: str, low: int, high: int) -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be a whole number.")
    if isinstance(value, Decimal):  # values read back from DynamoDB
        if value != value.to_integral_value():
            raise ValidationError(f"{name} must be a whole number.")
        value = int(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError(f"{name} must be a whole number.")
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(f"{name} must be a whole number.")
    if not low <= value <= high:
        raise ValidationError(f"{name} must be between {low} and {high}.")
    return value


# Each operation's options must be a JSON object; `true`/missing means "use defaults".
def _options(raw, name: str) -> dict:
    if raw is None or raw is True:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError(f"Options for '{name}' must be an object.")
    return raw


# Reject misspelled or unexpected option names instead of silently ignoring them.
def _reject_unknown(opts: dict, allowed: set, name: str) -> None:
    unknown = set(opts) - allowed
    if unknown:
        raise ValidationError(f"Unknown option(s) for '{name}': {', '.join(sorted(unknown))}.")


# INTERVIEW:
# Input validation on the server is mandatory even though the frontend validates too:
# anyone can call the API directly (curl, Postman) and skip the browser entirely.
# This is an "allow-list" approach: only known operations and options are accepted.
def parse_operations(raw, image_format: str) -> dict:
    """Return a normalized operations dict with defaults filled in.

    Only operations present in `raw` are enabled. Example input:
        {"resize": {"maxWidth": 800}, "grayscale": {}, "pixelate": {"level": 32}}
    """
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("Select at least one processing operation.")
    unknown = set(raw) - {"resize", "grayscale", "compress", "pixelate"}
    if unknown:
        raise ValidationError(f"Unknown operation(s): {', '.join(sorted(unknown))}.")

    ops: dict = {}
    for name, value in raw.items():
        # "resize": false means the user did not select it.
        if value is False:
            continue
        opts = _options(value, name)
        # Resize: defaults 600x400, aspect ratio kept unless explicitly turned off.
        if name == "resize":
            _reject_unknown(opts, {"maxWidth", "maxHeight", "keepAspectRatio"}, name)
            keep = opts.get("keepAspectRatio", True)
            if not isinstance(keep, bool):
                raise ValidationError("keepAspectRatio must be true or false.")
            ops["resize"] = {
                "maxWidth": _int(opts.get("maxWidth", 600), "maxWidth", RESIZE_MIN, RESIZE_MAX),
                "maxHeight": _int(opts.get("maxHeight", 400), "maxHeight", RESIZE_MIN, RESIZE_MAX),
                "keepAspectRatio": keep,
            }
        # Grayscale has no options.
        elif name == "grayscale":
            _reject_unknown(opts, set(), name)
            ops["grayscale"] = {}
        # Compression: JPEG needs a quality (10-100, default 75); PNG is lossless and
        # must NOT receive a quality value.
        elif name == "compress":
            _reject_unknown(opts, {"quality"}, name)
            if image_format == "JPEG":
                ops["compress"] = {
                    "quality": _int(opts.get("quality", 75), "quality", QUALITY_MIN, QUALITY_MAX)
                }
            else:
                if "quality" in opts:
                    raise ValidationError(
                        "quality applies only to JPEG images; PNG uses lossless optimization."
                    )
                ops["compress"] = {}
        # Pixelation: level 8-128 in steps of 8 (default 16).
        elif name == "pixelate":
            _reject_unknown(opts, {"level"}, name)
            level = _int(opts.get("level", 16), "level", PIXEL_MIN, PIXEL_MAX)
            if level % PIXEL_STEP:
                raise ValidationError(f"level must be a multiple of {PIXEL_STEP}.")
            ops["pixelate"] = {"level": level}

    if not ops:
        raise ValidationError("Select at least one processing operation.")
    return ops


# Validate the whole POST /api/jobs body: file name, declared type, declared size
# and the operations. The real file content is checked later by the processor,
# because at this point the file has not been uploaded yet.
def parse_create_job(body) -> dict:
    """Validate the POST /api/jobs body."""
    if not isinstance(body, dict):
        raise ValidationError("Request body must be a JSON object.")

    # File name: required, sensible length, no control characters.
    filename = body.get("filename")
    if not isinstance(filename, str) or not 1 <= len(filename) <= 255:
        raise ValidationError("filename is required (1-255 characters).")
    if any(ord(c) < 32 for c in filename):
        raise ValidationError("filename contains invalid characters.")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTENSION_TYPES:
        raise ValidationError("Unsupported file type. Supported formats: JPG, JPEG, PNG.")

    # Declared content type must be supported AND agree with the extension.
    content_type = body.get("contentType")
    if content_type not in CONTENT_TYPE_FORMAT:
        raise ValidationError("contentType must be image/jpeg or image/png.")
    if EXTENSION_TYPES[ext] != content_type:
        raise ValidationError("The file extension does not match the file type.")

    # Declared size: 1 byte to 10 MB. S3 later enforces this exact size on upload.
    file_size = _int(body.get("fileSize"), "fileSize", 1, MAX_FILE_BYTES)
    image_format = CONTENT_TYPE_FORMAT[content_type]
    operations = parse_operations(body.get("operations"), image_format)

    return {
        "filename": filename,
        "contentType": content_type,
        "format": image_format,
        "fileSize": file_size,
        "operations": operations,
    }


def is_valid_job_id(job_id) -> bool:
    return isinstance(job_id, str) and bool(JOB_ID_RE.match(job_id))
