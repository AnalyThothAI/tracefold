from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

from curl_cffi import requests as curl_requests

TOKEN_IMAGE_MIRROR_ALLOWED_PATH_PREFIXES = {
    "bin.bnbstatic.com": ("/",),
    "gmgn.ai": ("/external-res/",),
    "static.oklink.com": ("/cdn/web3/currency/token/",),
}
TOKEN_IMAGE_MIRROR_CURL_IMPERSONATE = "chrome142"
TOKEN_IMAGE_MIRROR_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/121 Safari/537.36",
}
TOKEN_IMAGE_MIRROR_MAX_BYTES = 3 * 1024 * 1024
TOKEN_IMAGE_MIRROR_TIMEOUT_SECONDS = 8.0

_MEDIA_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def mirror_token_image_source(
    row: dict[str, Any],
    *,
    app_home: str | Path,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """Perform exactly one provider attempt and local file write, without DB access."""

    source_url = str(row.get("source_url") or "").strip()
    try:
        source_url = _required_claimed_source_url(source_url)
        artifact = _fetch_and_store(
            source_url=source_url,
            app_home=Path(app_home),
            http_client=http_client or _CurlCffiTokenImageClient(),
        )
    except ValueError as exc:
        error = _error_text(exc)
        return {"status": "unsupported", "source_url": source_url, "error": error}
    except _TokenImageMirrorError as exc:
        error = str(exc)
        return {
            "status": ("unsupported" if _is_terminal_unsupported_error(error) else "error"),
            "source_url": source_url,
            "error": error,
        }
    return {"status": "ready", "source_url": source_url, "asset": artifact}


def _fetch_and_store(
    *,
    source_url: str,
    app_home: Path,
    http_client: Any,
) -> dict[str, Any]:
    parsed = validated_token_image_source_url(source_url)
    with _response_stream(http_client, parsed.geturl()) as response:
        final_url = str(getattr(response, "url", "") or parsed.geturl())
        validated_token_image_source_url(final_url)

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise _TokenImageMirrorError(f"image_fetch_failed: upstream_status_{status_code}")

        headers = _response_headers(response)
        content_length = _int_header(headers.get("content-length"))
        if content_length is not None and content_length > TOKEN_IMAGE_MIRROR_MAX_BYTES:
            raise _TokenImageMirrorError("image_too_large: content_length_exceeded")

        content = _read_bounded_content(response)

    media = _verified_media(content=content)
    content_hash = sha256(content).hexdigest()
    filename = f"{content_hash}{media.file_extension}"
    _write_cache_file(app_home=app_home, filename=filename, content=content)
    return {
        "source_url": source_url,
        "media_type": media.media_type,
        "file_extension": media.file_extension,
        "content_sha256": content_hash,
        "byte_size": len(content),
        "storage_path": filename,
    }


@contextmanager
def _response_stream(http_client: Any, url: str) -> Iterator[Any]:
    try:
        stream = getattr(http_client, "stream", None)
        if callable(stream):
            with stream(
                url,
                allow_redirects=True,
                headers=dict(TOKEN_IMAGE_MIRROR_HEADERS),
                timeout=TOKEN_IMAGE_MIRROR_TIMEOUT_SECONDS,
            ) as response:
                yield response
            return
        response = http_client.get(
            url,
            allow_redirects=True,
            headers=dict(TOKEN_IMAGE_MIRROR_HEADERS),
            timeout=TOKEN_IMAGE_MIRROR_TIMEOUT_SECONDS,
        )
        yield response
    except (curl_requests.RequestsError, OSError) as exc:
        raise _TokenImageMirrorError(f"image_fetch_failed: {_error_text(exc)}") from exc


def _read_bounded_content(response: Any) -> bytes:
    iterator = getattr(response, "iter_content", None)
    chunks = iterator() if callable(iterator) else (bytes(getattr(response, "content", b"") or b""),)
    content = bytearray()
    for chunk in chunks:
        materialized = bytes(chunk or b"")
        if len(content) + len(materialized) > TOKEN_IMAGE_MIRROR_MAX_BYTES:
            raise _TokenImageMirrorError("image_too_large: byte_limit_exceeded")
        content.extend(materialized)
    return bytes(content)


def _write_cache_file(*, app_home: Path, filename: str, content: bytes) -> None:
    cache_dir = app_home / "cache" / "token-images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = cache_dir / filename
    tmp_path = cache_dir / f".{filename}.{uuid4().hex}.tmp"
    try:
        tmp_path.write_bytes(content)
        tmp_path.replace(final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def is_allowed_token_image_source_url(raw_url: str) -> bool:
    try:
        validated_token_image_source_url(raw_url)
    except ValueError:
        return False
    return True


def validated_token_image_source_url(raw_url: str) -> ParseResult:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except ValueError as exc:
        raise ValueError("invalid_image_url") from exc
    path_prefixes = TOKEN_IMAGE_MIRROR_ALLOWED_PATH_PREFIXES.get((parsed.hostname or "").lower())
    if (
        parsed.scheme.lower() != "https"
        or path_prefixes is None
        or not parsed.path
        or not any(parsed.path.startswith(prefix) for prefix in path_prefixes)
    ):
        raise ValueError("unsupported_image_url")
    return parsed


@dataclass(frozen=True)
class _VerifiedMedia:
    media_type: str
    file_extension: str


class _TokenImageMirrorError(Exception):
    pass


class _CurlCffiTokenImageClient:
    @contextmanager
    def stream(self, url: str, **kwargs: Any) -> Iterator[Any]:
        session = curl_requests.Session(impersonate=cast(Any, TOKEN_IMAGE_MIRROR_CURL_IMPERSONATE))
        try:
            response = session.get(url, stream=True, **kwargs)
            try:
                yield response
            finally:
                cast(Any, response).close()
        finally:
            session.close()


def _required_claimed_source_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("source_url is required")
    if not text.startswith(("http://", "https://")):
        raise ValueError("source_url must be an absolute URL")
    return text


def _verified_media(*, content: bytes) -> _VerifiedMedia:
    magic_media_type = _magic_media_type(content)
    if magic_media_type is None:
        raise _TokenImageMirrorError("unsupported_image_bytes: unknown_magic")

    return _VerifiedMedia(
        media_type=magic_media_type,
        file_extension=_MEDIA_EXTENSIONS[magic_media_type],
    )


def _magic_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _response_headers(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", None) or {}
    return {str(key).lower(): str(value) for key, value in dict(headers).items()}


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:200] if text else exc.__class__.__name__


def _is_terminal_unsupported_error(error: str) -> bool:
    if error.startswith("unsupported_") or error.startswith("image_too_large:"):
        return True
    if not error.startswith("image_fetch_failed: upstream_status_"):
        return False
    status_text = error.rsplit("_", maxsplit=1)[-1]
    try:
        status_code = int(status_text)
    except ValueError:
        return False
    return status_code in {404, 410, 415}


__all__ = [
    "TOKEN_IMAGE_MIRROR_ALLOWED_PATH_PREFIXES",
    "is_allowed_token_image_source_url",
    "mirror_token_image_source",
    "validated_token_image_source_url",
]
