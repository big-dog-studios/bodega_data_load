"""GCS upload helper for the survey write path.

Photos arrive as multipart parts on POST /submissions; this streams each one to
the GCS bucket and returns the object path to persist on the row (the bytes live
in the bucket, never in Postgres). No signed URLs / no IAM signBlob dance — the
runtime service account just needs object-write on the bucket (roles/storage.objectAdmin).
Bucket name comes from env GCS_BUCKET.
"""
import os
import uuid

from google.cloud import storage

# Built once — the Service stays warm, same as the DB engine in db.py. Uses the
# runtime service account's Application Default Credentials automatically.
_client = storage.Client()
_bucket = _client.bucket(os.environ["GCS_BUCKET"])

# Tidy extension on the object name; default to .bin for anything unmapped.
_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/heic": "heic", "application/pdf": "pdf",
}


def upload_file(license_number: str, kind: str, file) -> str:
    """Stream one Starlette/FastAPI UploadFile to GCS; return its object path.

    `kind` (receipt|photo) is just a folder hint. The path is foldered by store so
    a license's media stays together; the filename is a random uuid to avoid clobber.
    """
    ext = _EXT.get(file.content_type, "bin")
    obj = f"submissions/{license_number}/{kind}/{uuid.uuid4().hex}.{ext}"
    blob = _bucket.blob(obj)
    blob.upload_from_file(file.file, content_type=file.content_type)
    return obj
