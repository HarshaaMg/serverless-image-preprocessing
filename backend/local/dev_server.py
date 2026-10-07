"""Local development server - runs the real Lambda code against *mocked* AWS.

    python local/dev_server.py

What it does:
  * starts a moto server (fake S3/DynamoDB/SNS) on http://127.0.0.1:5001
  * creates the buckets, table and topic there
  * serves the API on http://127.0.0.1:8000 by turning each HTTP request into an
    API Gateway event and calling imgproc.job_api.handler
  * after complete-upload succeeds, calls imgproc.processor.handler with an
    S3-style event in a background thread (simulating the S3 trigger)

IMPORTANT: this proves the application logic works, NOT that real AWS integration
(IAM permissions, S3 notifications, CORS on real buckets...) works.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Make the real Lambda package (backend/src/imgproc) importable from this script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MOTO_PORT = 5001
API_PORT = 8000
REGION = "ap-south-1"

# Fake credentials and the same environment variables Terraform sets on the real
# Lambdas, but pointing at the local moto server instead of AWS. These are set
# BEFORE importing boto3/imgproc so every client picks them up.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "local",
    "AWS_REGION": REGION,
    "AWS_DEFAULT_REGION": REGION,
    "INPUT_BUCKET": "local-input",
    "OUTPUT_BUCKET": "local-output",
    "JOBS_TABLE": "local-jobs",
    "LOCAL_AWS_ENDPOINT": f"http://127.0.0.1:{MOTO_PORT}",
    "LOCAL_S3_PUBLIC_ENDPOINT": f"http://127.0.0.1:{MOTO_PORT}",
})
os.environ.pop("AWS_PROFILE", None)

# INTERVIEW:
# moto is a library that imitates AWS APIs in memory. It lets us run and test the
# real code without an AWS account or charges - but it is a mock: it does not check
# IAM permissions and, for example, does not enforce presigned-URL expiry.
import boto3  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402

from imgproc import job_api, processor  # noqa: E402

# The same four routes API Gateway exposes in AWS (None = the {jobId} part).
ROUTES = [
    ("POST", ["api", "jobs"], "POST /api/jobs"),
    ("POST", ["api", "jobs", None, "complete-upload"], "POST /api/jobs/{jobId}/complete-upload"),
    ("GET", ["api", "jobs", None], "GET /api/jobs/{jobId}"),
    ("GET", ["api", "jobs", None, "results"], "GET /api/jobs/{jobId}/results"),
]


# Create the buckets, table and topic inside the mock, like Terraform does in AWS.
def setup_mock_aws() -> None:
    endpoint = os.environ["LOCAL_AWS_ENDPOINT"]
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name=REGION)
    for bucket in (os.environ["INPUT_BUCKET"], os.environ["OUTPUT_BUCKET"]):
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    boto3.client("dynamodb", endpoint_url=endpoint, region_name=REGION).create_table(
        TableName=os.environ["JOBS_TABLE"],
        KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    topic = boto3.client("sns", endpoint_url=endpoint, region_name=REGION).create_topic(Name="local-topic")
    os.environ["SNS_TOPIC_ARN"] = topic["TopicArn"]


# Turn "/api/jobs/<id>/results" into the routeKey API Gateway would send,
# e.g. ("GET /api/jobs/{jobId}/results", {"jobId": "<id>"}).
def match_route(method: str, path: str):
    parts = [p for p in path.split("/") if p]
    for route_method, pattern, key in ROUTES:
        if method != route_method or len(parts) != len(pattern):
            continue
        params = {}
        for part, expected in zip(parts, pattern):
            if expected is None:
                params["jobId"] = part
            elif part != expected:
                break
        else:
            return key, params
    return None, None


# In AWS, S3 itself invokes the processor when an object is created. The mock cannot
# do that, so we build the same kind of S3 event and call the handler ourselves.
def simulate_s3_trigger(job_id: str) -> None:
    event = {"Records": [{"eventSource": "aws:s3", "s3": {
        "bucket": {"name": os.environ["INPUT_BUCKET"]},
        "object": {"key": f"uploads/{job_id}/original"}}}]}
    try:
        processor.handler(event, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[local] processor raised {type(exc).__name__} (real Lambda would retry)")


# A tiny HTTP server that plays the role of API Gateway.
class ApiHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, headers: dict | None = None):
        self.send_response(status)
        for k, v in (headers or {"Content-Type": "application/json"}).items():
            self.send_header(k, v)
        # Local CORS (in AWS this is configured on API Gateway)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type,x-job-token")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_OPTIONS(self):
        self._send(204, "")

    # Convert the HTTP request into an API Gateway (payload v2) event, call the real
    # job_api.handler, and send its response back.
    def _handle(self, method: str):
        path = urlparse(self.path).path
        route, params = match_route(method, path)
        if not route:
            return self._send(404, json.dumps({"error": {"code": "NOT_FOUND", "message": "Route not found."}}))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 64 * 1024)).decode("utf-8", "replace") if length else None
        event = {
            "routeKey": route,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "pathParameters": params or None,
            "body": body,
            "isBase64Encoded": False,
        }
        resp = job_api.handler(event, None)
        self._send(resp["statusCode"], resp["body"], resp["headers"])
        # After a successful complete-upload, simulate the S3 trigger in the background
        # (in AWS this happens when the upload lands, independently of this call).
        if route.endswith("complete-upload") and resp["statusCode"] == 200:
            threading.Thread(target=simulate_s3_trigger, args=(params["jobId"],), daemon=True).start()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        # Print method + path only (never headers, which contain the job token).
        print(f"[api] {self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}")


def main():
    # Show the Lambda handlers' JSON log lines (CloudWatch does this in AWS).
    logging.basicConfig(level=logging.INFO, format="[lambda] %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # Start the mock AWS server, create the resources, then serve the API.
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=MOTO_PORT, verbose=False)
    moto.start()
    setup_mock_aws()
    server = ThreadingHTTPServer(("127.0.0.1", API_PORT), ApiHandler)
    print(f"Mock AWS (moto) : http://127.0.0.1:{MOTO_PORT}")
    print(f"Local API       : http://127.0.0.1:{API_PORT}/api/jobs")
    print("NOTE: AWS is mocked. This does not prove real AWS integration works.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        moto.stop()


if __name__ == "__main__":
    main()
