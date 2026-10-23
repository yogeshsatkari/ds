from datetime import datetime, timezone
import json
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


def extraction_prefix(user_id: str, patient_id: str) -> str:
    return f"users/{user_id}/patients/{patient_id}/extractions"


def extraction_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/context.md"


def extraction_json_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/context.json"


def discharge_summary_md_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/discharge-summary.md"


def discharge_summary_docx_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/discharge-summary.docx"


def patient_metadata_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/metadata.json"


def extraction_metrics_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/metrics.json"


def template_md_key(user_id: str, patient_id: str) -> str:
    return f"{extraction_prefix(user_id, patient_id)}/template.md"


def user_template_prefix(user_id: str) -> str:
    return f"users/{user_id}/template/"


def user_template_meta_key(user_id: str) -> str:
    return f"users/{user_id}/template/meta.json"


def user_template_files_prefix(user_id: str) -> str:
    return f"users/{user_id}/template/files/"


def user_template_md_key(user_id: str) -> str:
    return f"users/{user_id}/template/template.md"


def user_metadata_key(user_id: str) -> str:
    return f"users/{user_id}/metadata.json"


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
    meta_key = patient_metadata_key(user_id, patient_id)
    summary_md_key = discharge_summary_md_key(user_id, patient_id)
    json_key = extraction_json_key(user_id, patient_id)
    md_key = extraction_key(user_id, patient_id)

    updated_at = head_object_last_modified(client, docx_key)
    has_summary = updated_at is not None
    if not updated_at:
        updated_at = head_object_last_modified(client, meta_key)
    if not updated_at:
        updated_at = head_object_last_modified(client, summary_md_key)
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

    # 1. Try unified metadata.json first (Standardized for both flows)
    try:
        meta = get_json(client, meta_key)
        for field in (
            "patient_name",
            "age",
            "sex",
            "uhid_no",
            "date_of_admission",
            "date_of_discharge",
        ):
            item[field] = str(meta.get(field, "") or "").strip()
        if "discharge_type" in meta:
            item["discharge_type"] = meta["discharge_type"]
        if "template_name" in meta:
            item["template_name"] = meta["template_name"]
        return item
    except FileNotFoundError:
        pass

    # 2. Backward compatibility: Try context.json (Custom flow)
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
            item[field] = str(context.get(field, "") or "").strip()
        item["discharge_type"] = "custom"
        return item
    except FileNotFoundError:
        pass

    # 3. Backward compatibility: Try discharge-summary.md (Standard flow)
    try:
        from context_to_discharge.render import parse_patient_metadata_from_md
        summary_text = get_text(client, summary_md_key)
        meta = parse_patient_metadata_from_md(summary_text)

        # Lazy fallback: only fetch context.md if essential metadata is missing
        if any(not meta.get(f) for f in ("patient_name", "uhid_no")):
            try:
                context_text = get_text(client, md_key)
                meta = parse_patient_metadata_from_md(summary_text, fallback_context_md=context_text)
            except Exception:
                pass
        for field in (
            "patient_name",
            "age",
            "sex",
            "uhid_no",
            "date_of_admission",
            "date_of_discharge",
        ):
            item[field] = str(meta.get(field, "") or "").strip()
        item["discharge_type"] = "standard"
        return item
    except FileNotFoundError:
        pass

    return item




def list_user_patients(client, user_id: str) -> list[dict]:
    items: list[dict] = []
    for patient_id in list_patient_ids(client, user_id):
        item = build_patient_list_item(client, user_id, patient_id)
        if item:
            item["user_id"] = user_id
            items.append(item)
    items.sort(key=lambda row: row["updated_at"], reverse=True)
    return items


def list_all_user_ids(client) -> list[str]:
    prefix = "users/"
    user_ids: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            suffix = common_prefix["Prefix"][len(prefix) :].rstrip("/")
            if suffix:
                user_ids.append(suffix)
    return user_ids


def list_all_patients(client) -> list[dict]:
    all_patients: list[dict] = []
    for uid in list_all_user_ids(client):
        all_patients.extend(list_user_patients(client, uid))
    all_patients.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
    return all_patients



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


def delete_prefix_objects(client, prefix: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    objects_to_delete: list[dict] = []
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            objects_to_delete.append({"Key": obj["Key"]})
            if len(objects_to_delete) >= 1000:
                client.delete_objects(
                    Bucket=bucket(),
                    Delete={"Objects": objects_to_delete, "Quiet": True},
                )
                objects_to_delete = []
    if objects_to_delete:
        client.delete_objects(
            Bucket=bucket(),
            Delete={"Objects": objects_to_delete, "Quiet": True},
        )


def delete_user_template(client, user_id: str) -> None:
    delete_prefix_objects(client, user_template_prefix(user_id))


def put_user_template(
    client,
    user_id: str,
    template_items: list[tuple[str, bytes]],
    filename: str,
    template_md: Optional[str] = None,
) -> None:
    # 1. Clear any previous files under users/{user_id}/template/files/
    delete_prefix_objects(client, user_template_files_prefix(user_id))

    # 2. Upload each file under users/{user_id}/template/files/{item_filename}
    files_prefix = user_template_files_prefix(user_id)
    for item_filename, item_bytes in template_items:
        safe_filename = os.path.basename(item_filename).strip() or "template_file"
        file_key = f"{files_prefix}{safe_filename}"
        mime, _ = mimetypes.guess_type(safe_filename)
        content_type = mime or "application/octet-stream"
        put_bytes(client, file_key, item_bytes, content_type=content_type)

    # 3. Store template.md if pre-parsed
    if template_md:
        put_text(client, user_template_md_key(user_id), template_md)

    # 4. Save meta.json
    now_iso = datetime.now(timezone.utc).isoformat()
    meta = {
        "mode": "custom",
        "filename": filename,
        "file_count": len(template_items),
        "updated_at": now_iso,
    }
    put_json(client, user_template_meta_key(user_id), meta)


def get_user_template_meta(client, user_id: str) -> Optional[dict]:
    try:
        return get_json(client, user_template_meta_key(user_id))
    except FileNotFoundError:
        return None


def get_user_template_md(client, user_id: str) -> Optional[str]:
    try:
        return get_text(client, user_template_md_key(user_id))
    except FileNotFoundError:
        return None


def get_user_template_files(client, user_id: str) -> list[tuple[str, bytes]]:
    prefix = user_template_files_prefix(user_id)
    paginator = client.get_paginator("list_objects_v2")
    items: list[tuple[str, bytes]] = []
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key[len(prefix) :]
            if filename:
                try:
                    data, _ = get_bytes(client, key)
                    items.append((filename, data))
                except FileNotFoundError:
                    continue
    items.sort(key=lambda x: x[0])
    return items


def save_user_metadata(client, user_id: str, phone: str, pin: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    key = user_metadata_key(user_id)

    # Check if metadata already exists to preserve created_at
    existing = get_user_metadata(client, user_id)
    created_at = existing.get("created_at", now) if existing else now
    data = {
        "user_id": user_id,
        "phone": phone,
        "pin": pin,
        "created_at": created_at,
        "last_login": now,
    }

    put_json(client, key, data)
    return data


def get_user_metadata(client, user_id: str) -> Optional[dict]:
    key = user_metadata_key(user_id)
    try:
        return get_json(client, key)
    except FileNotFoundError:
        return None

