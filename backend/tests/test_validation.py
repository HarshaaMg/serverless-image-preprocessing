"""Tests for request/parameter validation and job tokens.

UNIT tests for imgproc/validation.py and imgproc/tokens.py. They make sure the API
rejects every invalid parameter (the frontend checks the same rules, but clients can
bypass the frontend) and that job tokens are random, hashed and verified correctly.
"""

import pytest

from imgproc import tokens
from imgproc.validation import ValidationError, is_valid_job_id, parse_create_job, parse_operations


def valid_body(**overrides):
    body = {
        "filename": "photo.jpg",
        "contentType": "image/jpeg",
        "fileSize": 12345,
        "operations": {"resize": {}, "pixelate": {"level": 16}},
    }
    body.update(overrides)
    return body


def test_defaults_are_filled_in():
    ops = parse_operations({"resize": {}, "compress": {}, "pixelate": {}, "grayscale": {}}, "JPEG")
    assert ops == {
        "resize": {"maxWidth": 600, "maxHeight": 400, "keepAspectRatio": True},
        "compress": {"quality": 75},
        "pixelate": {"level": 16},
        "grayscale": {},
    }


def test_disabled_operations_are_dropped():
    assert parse_operations({"grayscale": {}, "resize": False}, "PNG") == {"grayscale": {}}


@pytest.mark.parametrize("raw", [None, {}, {"resize": False}, [], "resize"])
def test_at_least_one_operation_required(raw):
    with pytest.raises(ValidationError, match="at least one"):
        parse_operations(raw, "JPEG")


@pytest.mark.parametrize("level", [8, 16, 64, 128])
def test_valid_pixel_levels(level):
    assert parse_operations({"pixelate": {"level": level}}, "PNG")["pixelate"]["level"] == level


# Out-of-range, not-a-multiple-of-8 and wrong-type pixelation levels are all rejected.
@pytest.mark.parametrize("level, message", [
    (0, "between"), (4, "between"), (136, "between"), (256, "between"),
    (12, "multiple of 8"), (100, "multiple of 8"),
    (16.5, "whole number"), ("16", "whole number"), (True, "whole number"), (None, "whole number"),
])
def test_invalid_pixel_levels(level, message):
    with pytest.raises(ValidationError, match=message):
        parse_operations({"pixelate": {"level": level}}, "PNG")


@pytest.mark.parametrize("quality", [9, 101, 0, -5])
def test_jpeg_quality_range(quality):
    with pytest.raises(ValidationError, match="between 10 and 100"):
        parse_operations({"compress": {"quality": quality}}, "JPEG")


def test_png_rejects_quality():
    with pytest.raises(ValidationError, match="only to JPEG"):
        parse_operations({"compress": {"quality": 50}}, "PNG")
    assert parse_operations({"compress": {}}, "PNG") == {"compress": {}}


@pytest.mark.parametrize("opts", [
    {"maxWidth": 0}, {"maxHeight": 5001}, {"maxWidth": "600"}, {"keepAspectRatio": "yes"}, {"width": 100},
])
def test_invalid_resize_options(opts):
    with pytest.raises(ValidationError):
        parse_operations({"resize": opts}, "JPEG")


def test_unknown_operation_rejected():
    with pytest.raises(ValidationError, match="Unknown operation"):
        parse_operations({"blur": {}}, "JPEG")


def test_create_job_valid():
    req = parse_create_job(valid_body())
    assert req["format"] == "JPEG" and req["fileSize"] == 12345


@pytest.mark.parametrize("overrides, message", [
    ({"filename": "doc.gif"}, "Unsupported"),
    ({"filename": "noext"}, "Unsupported"),
    ({"filename": ""}, "filename"),
    ({"filename": "a" * 300 + ".jpg"}, "filename"),
    ({"filename": "bad\x00name.jpg"}, "invalid characters"),
    ({"contentType": "image/gif"}, "contentType"),
    ({"filename": "photo.png"}, "does not match"),
    ({"fileSize": 0}, "between"),
    ({"fileSize": 10 * 1024 * 1024 + 1}, "between"),
    ({"fileSize": "100"}, "whole number"),
])
def test_create_job_invalid(overrides, message):
    with pytest.raises(ValidationError, match=message):
        parse_create_job(valid_body(**overrides))


def test_create_job_body_must_be_object():
    with pytest.raises(ValidationError):
        parse_create_job(["not", "an", "object"])


def test_job_id_format():
    assert is_valid_job_id("3f2b8c1e-9d4a-4b6f-8e2a-1c3d5e7f9a0b")
    assert not is_valid_job_id("../../etc/passwd")
    assert not is_valid_job_id(None)


# ------------------------------------------------------------------ tokens

def test_token_is_high_entropy_and_unique():
    a, b = tokens.new_token(), tokens.new_token()
    assert a != b and len(a) >= 43  # 32 random bytes, base64url


# Security test: only the matching token is accepted; empty, missing, overlong and
# random tokens are refused, and the stored value is a hash, not the token itself.
def test_token_verification():
    token = tokens.new_token()
    stored = tokens.hash_token(token)
    assert stored != token  # only the hash is stored
    assert tokens.verify_token(token, stored)
    assert not tokens.verify_token(tokens.new_token(), stored)
    assert not tokens.verify_token("", stored)
    assert not tokens.verify_token(None, stored)
    assert not tokens.verify_token("x" * 500, stored)
    assert not tokens.verify_token(token, None)
