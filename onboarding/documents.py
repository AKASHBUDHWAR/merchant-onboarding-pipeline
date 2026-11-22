import io
import warnings

from PIL import Image
from pypdf import PdfReader

from .domain import DomainError, FIELD_SCHEMA, require


def inspect_file(content, mime, cfg):
    require(mime in cfg["allowed_mime"], "Upload a PDF, PNG, or JPEG.", "UNSUPPORTED_FORMAT", 415)
    require(0 < len(content) <= cfg["max_file_mb"] * 1024 * 1024,
            "File is empty or exceeds the configured limit.", "FILE_SIZE", 413)
    try:
        if mime == "application/pdf":
            require(content.startswith(b"%PDF-"), "The file is not a PDF.", "UNSUPPORTED_FORMAT", 415)
            pdf = PdfReader(io.BytesIO(content))
            if pdf.is_encrypted:
                return None, "PASSWORD_PROTECTED"
            pages = len(pdf.pages)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    require(image.format == {"image/png": "PNG", "image/jpeg": "JPEG"}[mime],
                            "File bytes do not match the declared image type.", "UNSUPPORTED_FORMAT", 415)
                    image.verify()
            pages = 1
    except DomainError:
        raise
    except Exception:
        raise DomainError("UNREADABLE_FILE", "The uploaded file could not be read.", 422) from None
    require(1 <= pages <= cfg["max_pages_per_doc"], "Document page limit exceeded.", "PAGE_COUNT", 422)
    return pages, "UPLOADED"


def extract(doc, body, simulation, config):
    quality = simulation.get("quality_score", 1)
    detected = simulation.get("detected_type", doc["doc_type"])
    if doc["status"] == "PASSWORD_PROTECTED":
        status = "PASSWORD_PROTECTED"
    elif simulation.get("tampered"):
        status = "TAMPER_SUSPECTED"
    elif simulation.get("extraction_error"):
        status = "EXTRACTION_ERROR"
    elif quality < config["doc_quality_min_score"]:
        status = "UNREADABLE"
    elif detected != doc["doc_type"] and simulation.get("doc_type_confidence", 1) > 0.8:
        status = "WRONG_TYPE"
    elif doc["source"] == "FILE" and not body:
        status = "EXTRACTION_ERROR"
    else:
        status = "STRUCTURED" if doc["source"] == "STRUCTURED_PAYLOAD" else "EXTRACTED"
    fields = {}
    if status in {"STRUCTURED", "EXTRACTED"}:
        for key in FIELD_SCHEMA[doc["doc_type"]]:
            if key not in body:
                continue
            value = body[key]
            confidence = simulation.get("confidence_override", {}).get(key, 1)
            if confidence >= config["field_confidence_auto_accept"]:
                field_status = "AUTO_ACCEPTED"
            elif confidence >= config["field_confidence_review"]:
                field_status = "NEEDS_CONFIRMATION"
            else:
                field_status = "LOW_CONFIDENCE"
            fields[key] = {"value": value, "confidence": confidence, "status": field_status}
    return status, quality, detected, fields
