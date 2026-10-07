"""Job access tokens.

There is no login system. Instead, each job gets a random 256-bit token that is
returned only to the browser that created the job. Only a SHA-256 hash is stored.
This is limited job-level protection, not user authentication.

Used by job_api.py: new_token()/hash_token() when a job is created, and
verify_token() on every job-specific request (status, complete-upload, results).
"""

from __future__ import annotations

import hashlib
import hmac
# `secrets` (not `random`) is Python's cryptographically secure random generator.
import secrets

# Reject obviously wrong tokens early (a real token is 43 characters long).
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 128


# INTERVIEW:
# This is a "bearer token": whoever holds it can access the job. 32 random bytes
# (256 bits) are impossible to guess. It is authorization for ONE job, not
# authentication of a user - there are no accounts in this project.
def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits of randomness


# INTERVIEW:
# We store only a hash of the token in DynamoDB, the same idea as storing password
# hashes: if the database leaked, the attacker still could not use the tokens.
# A fast hash (SHA-256) is fine here because the token is long and random;
# slow hashes like bcrypt are needed for low-entropy, human-chosen passwords.
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Check a token sent by the browser against the stored hash.
def verify_token(token, stored_hash) -> bool:
    if not isinstance(token, str) or not isinstance(stored_hash, str):
        return False
    if not MIN_TOKEN_LENGTH <= len(token) <= MAX_TOKEN_LENGTH:
        return False
    # INTERVIEW:
    # compare_digest takes the same time whether the first or last character
    # differs, which prevents "timing attacks" that guess a secret byte by byte.
    return hmac.compare_digest(hash_token(token), stored_hash)
