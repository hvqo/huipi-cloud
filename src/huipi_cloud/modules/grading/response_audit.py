"""Private immutable model receipts; never log their sensitive content or object key."""

import hashlib
import json
from dataclasses import asdict
from io import BytesIO
from uuid import uuid4

from huipi_cloud.infrastructure.grading.provider import MAX_PROVIDER_RESPONSE_BYTES
from huipi_cloud.modules.grading.protocol import json_sha256


def response_auditor(storage, document, request_id, provider, prompt_version):
    """One receipt per actual call, including rejected responses; no database privilege."""

    async def save(response, *, converted, error_code, prompt_sha256):
        # Provider bounds the raw response; independently cap this trust boundary.
        if len(response.content.encode()) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("model_audit_response_size_limit")
        payload = {
            "schema_name": "huipi.grading.model-call-audit",
            "schema_version": "1.0",
            "request_id": str(request_id),
            "grade_input_artifact_id": str(document.artifact_id),
            "grade_input_sha256": document.input_sha256,
            "request_fingerprint": json_sha256(
                {
                    "grade_input_artifact_id": str(document.artifact_id),
                    "grade_input_sha256": document.input_sha256,
                    "provider": provider.provider_name,
                    "model_id": provider.model_id,
                    "prompt_version": prompt_version,
                    "generation_config_digest": provider.generation_config_digest,
                }
            ),
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "generation_config_digest": provider.generation_config_digest,
            "response": asdict(response),
            "response_sha256": hashlib.sha256(response.content.encode()).hexdigest(),
            "locator_version": "segment-unique-exact-v1",
            "converted_reply": converted.model_dump(mode="json") if converted else None,
            "validation_error_code": error_code,
            "semantic_review_status": "unreviewed",
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > 1024 * 1024:
            raise ValueError("model_audit_size_limit")
        prefix = storage.object_key_prefix.strip("/")
        key = f"grading/model-call-audits/{document.artifact_id}/{request_id}/{uuid4()}.json"
        await storage.upload_fileobj(
            BytesIO(encoded), f"{prefix}/{key}" if prefix else key, "application/json"
        )
        return json_sha256(payload)

    return save
