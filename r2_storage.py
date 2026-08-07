import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def r2_configured() -> bool:
    return bool(
        os.environ.get("R2_BUCKET")
        and os.environ.get("R2_ENDPOINT")
        and os.environ.get("ACCESS_KEY_ID")
        and os.environ.get("SECRET_ACCESS_KEY")
    )


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def bucket() -> str:
    return os.environ["R2_BUCKET"]


def extraction_key(user_id: str, patient_id: str, extraction_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/extractions/context.md"


def extraction_json_key(user_id: str, patient_id: str, extraction_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/extractions/context.json"


def put_text(client, key: str, text: str) -> None:
    client.put_object(
        Bucket=bucket(),
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def put_json(client, key: str, data: dict) -> None:
    client.put_object(
        Bucket=bucket(),
        Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def get_text(client, key: str) -> str:
    try:
        obj = client.get_object(Bucket=bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise FileNotFoundError(key) from exc
        raise
    return obj["Body"].read().decode("utf-8")


def get_json(client, key: str) -> dict:
    try:
        obj = client.get_object(Bucket=bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise FileNotFoundError(key) from exc
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))
