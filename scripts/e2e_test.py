"""End-to-end test of the HTTP workflow, exactly as the browser performs it.

Usage (from the backend virtual environment, which has Pillow):
    python scripts/e2e_test.py --api http://127.0.0.1:5173            # local (via Vite proxy)
    python scripts/e2e_test.py --api https://xxxx.execute-api.ap-south-1.amazonaws.com --origin http://localhost:5173
    add --wait-expiry to also wait for a download link to expire and check it gets refreshed

Uses only the public API plus the presigned S3 URLs it returns. Never prints job tokens.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from PIL import Image

# INTERVIEW:
# This is an END-TO-END test: it uses only the public HTTP API and the presigned S3
# URLs, exactly like the browser does, so against the deployed API it exercises the
# real chain API Gateway -> Lambda -> DynamoDB -> S3 -> S3 trigger -> processor
# Lambda -> SNS. Against the local server it only proves the logic with mocked AWS.

# (name, passed?, detail) for every check, summarized at the end.
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    return ok


def http(method, url, *, body=None, headers=None, raw=False):
    data = None
    headers = dict(headers or {})
    if body is not None and not isinstance(body, bytes):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(body, bytes):
        data = body
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
            return resp.status, (content if raw else _json(content)), dict(resp.headers)
    except urllib.error.HTTPError as err:
        content = err.read()
        return err.code, (content if raw else _json(content)), dict(err.headers)


def _json(content):
    try:
        return json.loads(content) if content else None
    except ValueError:
        return content.decode(errors="replace")[:300]


def make_image(fmt, size, mode="RGB"):
    img = Image.new(mode, size, (220, 60, 40, 255)[: len(mode)])
    px = img.load()
    for x in range(0, size[0], 9):
        for y in range(size[1]):
            px[x, y] = (20, 120, 230, 120)[: len(mode)]
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


# Build a multipart/form-data body by hand (what a browser FormData upload sends):
# the presigned fields first, then the file as the LAST field, as S3 requires.
def multipart(fields: dict, file_bytes: bytes):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + file_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, api, origin):
        self.api = api.rstrip("/")
        self.origin = origin

    def headers(self, token=None):
        h = {"Origin": self.origin} if self.origin else {}
        if token:
            h["X-Job-Token"] = token
        return h

    def create(self, filename, content_type, size, operations):
        return http("POST", f"{self.api}/api/jobs", headers=self.headers(), body={
            "filename": filename, "contentType": content_type, "fileSize": size, "operations": operations})

    def upload(self, upload, data):
        body, ctype = multipart(upload["fields"], data)
        h = {"Content-Type": ctype}
        if self.origin:
            h["Origin"] = self.origin
        return http("POST", upload["url"], body=body, headers=h, raw=True)

    def complete(self, job_id, token):
        return http("POST", f"{self.api}/api/jobs/{job_id}/complete-upload", headers=self.headers(token))

    def status(self, job_id, token):
        return http("GET", f"{self.api}/api/jobs/{job_id}", headers=self.headers(token))

    def results(self, job_id, token):
        return http("GET", f"{self.api}/api/jobs/{job_id}/results", headers=self.headers(token))

    def poll(self, job_id, token, timeout=120):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            code, body, _ = self.status(job_id, token)
            if code == 200:
                if not seen or seen[-1] != body["status"]:
                    seen.append(body["status"])
                if body["status"] in ("COMPLETED", "FAILED"):
                    return body, seen
            time.sleep(1.5)
        return None, seen

    def run_job(self, data, filename, content_type, operations):
        code, job, _ = self.create(filename, content_type, len(data), operations)
        if code != 201:
            return None, f"create returned {code}: {job}"
        up_code, up_body, up_headers = self.upload(job["upload"], data)
        if up_code not in (200, 201, 204):
            return None, f"S3 upload returned {up_code}: {up_body[:200]!r}"
        c_code, c_body, _ = self.complete(job["jobId"], job["jobToken"])
        final, seen = self.poll(job["jobId"], job["jobToken"])
        return {"job": job, "upload_headers": up_headers, "complete": (c_code, c_body),
                "final": final, "seen": seen}, ""


def download(url, origin=None):
    return http("GET", url, raw=True, headers={"Origin": origin} if origin else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--origin", default="", help="Frontend origin to send, to verify CORS")
    parser.add_argument("--wait-expiry", type=int, default=0, metavar="SECONDS",
                        help="wait this long (> download TTL) and check refresh of expired links")
    args = parser.parse_args()
    c = Client(args.api, args.origin)

    # 1. Full JPEG job with all four operations
    jpeg = make_image("JPEG", (1600, 1000))
    ops = {"resize": {"maxWidth": 600, "maxHeight": 400, "keepAspectRatio": True}, "grayscale": {},
           "compress": {"quality": 40}, "pixelate": {"level": 16}}
    run, err = c.run_job(jpeg, "e2e photo.jpg", "image/jpeg", ops)
    if not check("JPEG job created and uploaded via presigned S3 POST", run is not None, err):
        return summary()
    job = run["job"]
    if args.origin:
        allow = {k.lower(): v for k, v in run["upload_headers"].items()}.get("access-control-allow-origin")
        check("S3 upload CORS allows the frontend origin", allow in (args.origin, "*"), f"got {allow}")
    check("complete-upload accepted", run["complete"][0] == 200, str(run["complete"]))
    final = run["final"]
    check("job reached COMPLETED", bool(final) and final["status"] == "COMPLETED",
          f"statuses seen: {run['seen']}; error: {final and final.get('errorMessage')}")
    if not final or final["status"] != "COMPLETED":
        return summary()
    variants = [o["variant"] for o in final["outputs"]]
    check("one output per operation + combined", variants == ["resize", "grayscale", "compress", "pixelate", "combined"],
          str(variants))

    code, res, _ = c.results(job["jobId"], job["jobToken"])
    check("results API returns presigned URLs", code == 200 and all("downloadUrl" in r for r in res["results"]))
    by_variant = {r["variant"]: r for r in res["results"]}
    expectations = {
        "resize": ((600, 375), None), "grayscale": ((1600, 1000), "L"), "compress": ((1600, 1000), None),
        "pixelate": ((1600, 1000), None), "combined": ((600, 375), "L"),
    }
    for variant, (size, mode) in expectations.items():
        r = by_variant[variant]
        d_code, d_body, d_headers = download(r["downloadUrl"])
        disp = {k.lower(): v for k, v in d_headers.items()}.get("content-disposition", "")
        ok = d_code == 200
        detail = f"HTTP {d_code}"
        if ok:
            img = Image.open(io.BytesIO(d_body))
            ok = img.format == "JPEG" and img.size == size and (mode is None or img.mode == mode) \
                and len(d_body) == r["sizeBytes"] and "attachment" in disp
            detail = f"{img.format} {img.size} {img.mode}, {len(d_body)} bytes, disposition={disp!r}"
        check(f"download '{variant}' is correct", ok, detail)
    p_code, _, _ = download(by_variant["pixelate"]["previewUrl"], args.origin or None)
    check("preview URL loads", p_code == 200)
    px = by_variant["pixelate"]["details"]["pixelate"]
    check("pixelation level & grid recorded", px == {"level": 16, "gridWidth": 16, "gridHeight": 10}, str(px))
    comp_size = by_variant["compress"]["sizeBytes"]
    check("JPEG quality 40 output is smaller than original", comp_size < len(jpeg), f"{comp_size} < {len(jpeg)}")

    # 2. Duplicate upload event: re-POST the same object with the still-valid form
    # (an idempotency check: a second S3 event must not change the completed job)
    before = c.status(job["jobId"], job["jobToken"])[1]
    dup_code, _, _ = c.upload(job["upload"], jpeg)
    c.complete(job["jobId"], job["jobToken"])
    time.sleep(8)
    after = c.status(job["jobId"], job["jobToken"])[1]
    check("duplicate S3 event does not change a completed job",
          dup_code in (200, 201, 204) and after["status"] == "COMPLETED"
          and after["outputs"] == before["outputs"] and after["completedAt"] == before["completedAt"])

    # 3. PNG with transparency, compression + pixelation at the extremes
    for level in (8, 128):
        png = make_image("PNG", (900, 300), mode="RGBA")
        run, err = c.run_job(png, "e2e.png", "image/png", {"compress": {}, "pixelate": {"level": level}})
        ok = run and run["final"] and run["final"]["status"] == "COMPLETED"
        detail = err
        if ok:
            out = {o["variant"]: o for o in run["final"]["outputs"]}
            grid = out["pixelate"]["details"]["pixelate"]
            ok = grid["gridWidth"] == level and grid["gridHeight"] == round(level / 3) \
                and out["compress"]["details"]["compress"]["method"] == "png-lossless"
            detail = f"grid {grid['gridWidth']}x{grid['gridHeight']}"
        check(f"PNG pixelation level {level} + lossless compression", bool(ok), detail)

    # 4. Validation and failure handling
    code, body, _ = c.create("a.jpg", "image/jpeg", 1000, {"pixelate": {"level": 12}})
    check("invalid pixelation level rejected (400)", code == 400, str(body))
    code, body, _ = c.create("a.jpg", "image/jpeg", 1000, {"compress": {"quality": 5}})
    check("invalid JPEG quality rejected (400)", code == 400)
    code, body, _ = c.create("anim.gif", "image/gif", 1000, {"grayscale": {}})
    check("unsupported file type rejected (400)", code == 400)
    code, body, _ = c.create("big.jpg", "image/jpeg", 10 * 1024 * 1024 + 1, {"grayscale": {}})
    check("file over 10 MB rejected (400)", code == 400)
    code, body, _ = c.create("a.jpg", "image/jpeg", 1000, {})
    check("no operation selected rejected (400)", code == 400)

    data = make_image("JPEG", (300, 300))
    code, j, _ = c.create("size.jpg", "image/jpeg", len(data), {"grayscale": {}})
    up_code, up_body, _ = c.upload(j["upload"], data + b"\x00" * 5000)
    if up_code == 400:
        check("upload bigger than declared is refused", True, "refused by S3 policy (content-length-range)")
    else:
        # Mock S3 (moto) does not enforce POST policies; complete-upload must catch it instead.
        cc, cb, _ = c.complete(j["jobId"], j["jobToken"])
        check("upload bigger than declared is refused", cc == 400,
              f"S3 accepted it (HTTP {up_code}, mock S3 does not enforce policies); "
              f"complete-upload returned {cc}: {cb}")

    corrupt = make_image("JPEG", (800, 600))[:600]
    run, err = c.run_job(corrupt, "broken.jpg", "image/jpeg", {"grayscale": {}})
    final = run and run["final"]
    check("corrupt image ends as FAILED with a clear message",
          bool(final) and final["status"] == "FAILED" and "corrupt" in (final.get("errorMessage") or ""),
          f"{final and final.get('errorMessage')}")

    fake = b"This is a text file pretending to be a JPEG" * 10
    run, err = c.run_job(fake, "fake.jpg", "image/jpeg", {"grayscale": {}})
    final = run and run["final"]
    check("non-image content ends as FAILED", bool(final) and final["status"] == "FAILED",
          f"{final and final.get('errorMessage')}")

    code, _, _ = c.status(job["jobId"], "x" * 43)
    check("wrong job token rejected (404)", code == 404)
    code, _, _ = c.status(job["jobId"], None)
    check("missing job token rejected (401)", code == 401)

    # 5. Expired download links can be refreshed without reprocessing
    # (real S3 refuses an expired presigned URL with 403; the mock does not, so this
    # step is only meaningful against AWS)
    if args.wait_expiry:
        old_url = by_variant["combined"]["downloadUrl"]
        print(f"... waiting {args.wait_expiry}s for download links to expire")
        time.sleep(args.wait_expiry)
        old_code, _, _ = download(old_url)
        code, res2, _ = c.results(job["jobId"], job["jobToken"])
        new_url = {r["variant"]: r for r in res2["results"]}["combined"]["downloadUrl"]
        new_code, new_body, _ = download(new_url)
        check("expired link is refused, refreshed link works",
              old_code == 403 and new_code == 200 and len(new_body) == by_variant["combined"]["sizeBytes"],
              f"old={old_code} new={new_code}")
    return summary()


def summary():
    passed = sum(ok for _, ok, _ in RESULTS)
    failed = len(RESULTS) - passed
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
