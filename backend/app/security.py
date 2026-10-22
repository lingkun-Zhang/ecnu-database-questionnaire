from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import unicodedata
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
import jwt

PASSWORDS = PasswordHasher()


def utcnow():
    # BSON Date stores milliseconds. Initial and replayed receipts must agree.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def normalize_username(value):
    display = unicodedata.normalize("NFKC", value).strip()
    if not 3 <= len(display) <= 32 or not all(c.isalnum() or c == "_" for c in display):
        raise ValueError("用户名须为3—32个字母、数字或下划线")
    return display, display.casefold()


def verify_password(hashed, password):
    try:
        return PASSWORDS.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def token(secret, purpose, user_id, seconds, **claims):
    now = utcnow()
    expires = now + timedelta(seconds=seconds)
    payload = {"sub": user_id, "aud": purpose, "iss": "db-lab", "iat": now,
               "exp": expires, "jti": str(uuid4()), **claims}
    return jwt.encode(payload, secret, algorithm="HS256"), expires


def decode_token(value, secret, purpose):
    return jwt.decode(value, secret, algorithms=["HS256"], audience=purpose,
                      issuer="db-lab", options={"require": ["sub", "aud", "iss", "iat", "exp"]})


def participant_key(secret, survey_id, user_id):
    # Structured encoding prevents ambiguous concatenation of the two identities.
    message = json.dumps([survey_id, user_id], separators=(",", ":")).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_answers(definition, answers):
    kinds = {q["id"]: q["kind"] for q in definition["questions"]}
    result = {}
    for qid, value in answers.items():
        if kinds[qid] == "multi":
            value = sorted(value)
        elif kinds[qid] == "number" and float(value).is_integer():
            value = int(value)
        result[qid] = value
    return result


def submission_digest(version_id, anonymous, answers):
    return digest({"canonicalizationVersion": 1, "versionId": version_id,
                   "anonymous": anonymous, "answers": answers})
