import hmac
import hashlib
import os
from fastapi import HTTPException

GITHUB_SECRET = os.getenv("GITHUB_SECRET")


def verify_github_signature(payload_body: bytes, signature_header: str):
    if not GITHUB_SECRET:
        return True
    hash_object = hmac.new(GITHUB_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")