import json
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


def extraction_prefix(user_id: str, patient_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/extractions"


def extraction_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/context.md"


def extraction_json_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/context.json"


def discharge_summary_docx_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/discharge-summary.docx"


def user_patients_prefix(user_id: str) -> str:
    return f"users/{user_id}/patients/"


def list_patient_ids(client, user_id: str) -> list[str]:
    prefix = user_patients_prefix(user_id)
    patient_ids: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            suffix = common_prefix["Prefix"][len(prefix) :].rstrip("/")
            if suffix:
                patient_ids.append(suffix)
    return patient_ids


def head_object_last_modified(client, key: str) -> Optional[str]:
    try:
        obj = client.head_object(Bucket=bucket(), Key=key)
    except ClientError:
        return None
    return obj["LastModified"].isoformat()


def build_patient_list_item(client, user_id: str, patient_id: str) -> Optional[dict]:
    docx_key = discharge_summary_docx_key(user_id, patient_id)
    json_key = extraction_json_key(user_id, patient_id)
    md_key = extraction_key(user_id, patient_id)

    updated_at = head_object_last_modified(client, docx_key)
    has_summary = updated_at is not None
    if not updated_at:
        updated_at = head_object_last_modified(client, json_key)
    if not updated_at:
        updated_at = head_object_last_modified(client, md_key)
    if not updated_at:
        return None

    item = {
        "patient_id": patient_id,
        "patient_name": "",
        "age": "",
        "sex": "",
        "uhid_no": "",
        "date_of_admission": "",
        "date_of_discharge": "",
        "updated_at": updated_at,
        "has_summary": has_summary,
    }
    try:
        context = get_json(client, json_key)
        for field in (
            "patient_name",
            "age",
            "sex",
            "uhid_no",
            "date_of_admission",
            "date_of_discharge",
        ):
            item[field] = str(context.get(field, "") or "")
    except FileNotFoundError:
        pass
    return item


def list_user_patients(client, user_id: str) -> list[dict]:
    items: list[dict] = []
    for patient_id in list_patient_ids(client, user_id):
        item = build_patient_list_item(client, user_id, patient_id)
        if item:
            items.append(item)
    items.sort(key=lambda row: row["updated_at"], reverse=True)
    return items


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


def put_bytes(
    client,
    key: str,
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> None:
    client.put_object(
        Bucket=bucket(),
        Key=key,
        Body=body,
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


def get_json(client, key: str) -> dict:
    try:
        obj = client.get_object(Bucket=bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            raise FileNotFoundError(key) from exc
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


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
