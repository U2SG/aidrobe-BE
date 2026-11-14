from dataclasses import dataclass

_MESSAGES = {
    "source_invalid": "The import image or link could not be used",
    "image_invalid": "The import image is invalid",
    "no_garments": "No garments were detected",
    "provider_unavailable": "Image recognition is temporarily unavailable",
    "provider_configuration": "Image recognition is unavailable",
    "provider_response_invalid": "Image recognition returned an invalid result",
    "import_attempt_timed_out": "Import took too long",
    "import_failed": "Import failed",
    "vision_failed": "Image recognition failed",
    "ocr_failed": "Receipt recognition failed",
}


@dataclass(frozen=True)
class ImportFailure(Exception):
    code: str
    retryable: bool
    stage: str
    provider_status: int | None = None


def public_error_message(code: str | None) -> str | None:
    return _MESSAGES.get(code, _MESSAGES["import_failed"]) if code else None
