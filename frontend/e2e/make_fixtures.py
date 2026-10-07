"""Generate test images for the browser tests into frontend/e2e/.fixtures/."""

import io
import sys
from pathlib import Path

from PIL import Image

out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / ".fixtures")
out.mkdir(parents=True, exist_ok=True)


def striped(mode, size, bg, fg):
    img = Image.new(mode, size, bg)
    px = img.load()
    for x in range(0, size[0], 9):
        for y in range(size[1]):
            px[x, y] = fg
    for y in range(0, size[1], 13):
        for x in range(size[0]):
            px[x, y] = fg
    return img


# A valid 1600x1000 JPEG with stripes (gives compression and pixelation detail to work on).
buf = io.BytesIO()
striped("RGB", (1600, 1000), (220, 60, 40), (20, 120, 230)).save(buf, "JPEG", quality=95)
jpeg = buf.getvalue()
(out / "photo.jpg").write_bytes(jpeg)

striped("RGBA", (900, 300), (0, 0, 255, 128), (255, 200, 0, 255)).save(out / "logo.png")

# Invalid inputs: a text file named .jpg, a file over 10 MB, and an unsupported GIF.
(out / "notimage.jpg").write_bytes(b"This is a text file pretending to be a JPEG.\n" * 20)
(out / "big.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 100))
Image.new("RGB", (50, 50), (0, 200, 0)).save(out / "anim.gif")

# Real JPEG header but cut off: may decode partially in a browser, Pillow refuses it.
(out / "truncated.jpg").write_bytes(jpeg[: len(jpeg) * 6 // 10])

print(f"fixtures written to {out}")
