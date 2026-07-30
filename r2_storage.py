import mimetypes
import os
from typing import Optional

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


def template_prefix(user_id: str, template_id: str) -> str:
    return f"users/{user_id}/templates/{template_id}"


def template_asset_key(user_id: str, template_id: str, asset_path: str) -> str:
    return f"{template_prefix(user_id, template_id)}/{asset_path}"


def extraction_key(user_id: str, patient_id: str, extraction_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/extractions/{extraction_id}.md"


def summary_prefix(user_id: str, patient_id: str, summary_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/summaries/{summary_id}"


def summary_asset_key(user_id: str, patient_id: str, summary_id: str, asset_path: str) -> str:
    return f"{summary_prefix(user_id, patient_id, summary_id)}/{asset_path}"


def legacy_job_prefix(job_id: str) -> str:
    return f"jobs/{job_id}"


def upload_dir(client, prefix: str, local_dir: str) -> None:
    for root, _, files in os.walk(local_dir):
        for name in files:
            local_path = os.path.join(root, name)
            rel = os.path.relpath(local_path, local_dir).replace("\\", "/")
            key = f"{prefix}/{rel}"
            extra: dict = {}
            content_type, _ = mimetypes.guess_type(local_path)
            if content_type:
                extra["ContentType"] = content_type
            if extra:
                client.upload_file(local_path, bucket(), key, ExtraArgs=extra)
            else:
                client.upload_file(local_path, bucket(), key)


def put_text(client, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    client.put_object(
        Bucket=bucket(),
        Key=key,
        Body=text.encode("utf-8"),
        ContentType=content_type,
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


def get_bytes(client, key: str) -> tuple[bytes, Optional[str]]:
    try:
        obj = client.get_object(Bucket=bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise FileNotFoundError(key) from exc
        raise
    content_type = obj.get("ContentType")
    return obj["Body"].read(), content_type


def copy_prefix(
    client,
    source_prefix: str,
    dest_prefix: str,
    *,
    exclude_names: set[str] | None = None,
) -> int:
    exclude_names = exclude_names or set()
    source_prefix = source_prefix.rstrip("/") + "/"
    dest_prefix = dest_prefix.rstrip("/") + "/"
    copied = 0

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=source_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(source_prefix) :]
            if not rel or rel.endswith("/"):
                continue
            if rel.split("/")[-1] in exclude_names:
                continue
            client.copy_object(
                Bucket=bucket(),
                CopySource={"Bucket": bucket(), "Key": key},
                Key=f"{dest_prefix}{rel}",
            )
            copied += 1

    return copied
