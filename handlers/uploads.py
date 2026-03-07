"""Upload handlers: POST /upload (multipart), GET /uploads/* (file serving)"""

import os
import re
import uuid
import mimetypes

from aircp_daemon import transport, _bot_send, UPLOAD_DIR, UPLOAD_MAX_BYTES, UPLOAD_BODY_MAX, UPLOAD_ALLOWED_MIME


def _sanitize_filename(name):
    """Sanitize filename: keep alphanum, dots, hyphens, underscores."""
    # Remove path separators
    name = name.replace("/", "_").replace("\\", "_")
    # Keep only safe characters
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    # Collapse multiple underscores
    safe = re.sub(r'_+', '_', safe).strip('_')
    # Limit length
    if len(safe) > 100:
        ext = os.path.splitext(safe)[1]
        safe = safe[:96] + ext
    return safe or "unnamed"


def post_upload_dispatch(handler):
    """Handle file upload via multipart/form-data.

    This is a 'raw' POST handler -- it reads handler.rfile directly
    instead of receiving a pre-parsed JSON body.
    """
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", 0))

    if length > UPLOAD_BODY_MAX:
        handler.send_json({"error": f"Upload too large ({length} bytes, max body size {UPLOAD_BODY_MAX})"}, 413)
        return

    if "multipart/form-data" not in content_type:
        handler.send_json({"error": "Expected multipart/form-data"}, 400)
        return

    # Extract boundary
    boundary = None
    for part in content_type.split(";"):
        p = part.strip()
        if p.startswith("boundary="):
            boundary = p.split("=", 1)[1].strip('"')
    if not boundary:
        handler.send_json({"error": "Missing multipart boundary"}, 400)
        return

    raw = handler.rfile.read(length)
    sep = ("--" + boundary).encode()
    parts = raw.split(sep)

    file_data = None
    file_name = None
    file_mime = None
    fields = {}

    for part in parts:
        if not part or part.strip() in (b"", b"--", b"--\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        hdr_raw, body = part.split(b"\r\n\r\n", 1)
        if body.endswith(b"\r\n"):
            body = body[:-2]

        hdr_str = hdr_raw.decode("utf-8", errors="replace")
        name = None
        fname = None
        ct = "application/octet-stream"
        for line in hdr_str.split("\r\n"):
            lo = line.lower()
            if "content-disposition:" in lo:
                if 'name="' in line:
                    name = line.split('name="')[1].split('"')[0]
                if 'filename="' in line:
                    fname = line.split('filename="')[1].split('"')[0]
            if "content-type:" in lo:
                ct = line.split(":", 1)[1].strip()

        if fname:
            file_data = body
            file_name = fname
            file_mime = ct
        elif name:
            fields[name] = body.decode("utf-8", errors="replace")

    if file_data is None:
        handler.send_json({"error": "No file found in upload"}, 400)
        return

    if len(file_data) > UPLOAD_MAX_BYTES:
        handler.send_json({"error": f"File too large ({len(file_data)} bytes, max {UPLOAD_MAX_BYTES})"}, 413)
        return

    # MIME validation
    if file_mime not in UPLOAD_ALLOWED_MIME:
        # Try to guess from filename
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed and guessed in UPLOAD_ALLOWED_MIME:
            file_mime = guessed
        else:
            handler.send_json({
                "error": f"File type not allowed: {file_mime}",
                "allowed": sorted(UPLOAD_ALLOWED_MIME)
            }, 415)
            return

    # Sanitize and save
    safe_name = _sanitize_filename(file_name)
    file_id = str(uuid.uuid4())[:8]
    stored_name = f"{file_id}_{safe_name}"
    file_path = os.path.join(UPLOAD_DIR, stored_name)

    try:
        with open(file_path, "wb") as f:
            f.write(file_data)
    except Exception as e:
        handler.send_json({"error": f"Failed to save file: {e}"}, 500)
        return

    # Build URL (served via GET /uploads/)
    file_url = f"/uploads/{stored_name}"
    file_size = len(file_data)
    room = fields.get("room", "#brainstorm")
    from_id = fields.get("from", transport.agent_id if transport else "@system")

    # Send chat message with file metadata
    content = f"[FILE:{file_url}|{file_mime}|{safe_name}|{file_size}]"

    _bot_send(room, content, from_id=from_id)

    handler.send_json({
        "ok": True,
        "file_id": file_id,
        "url": file_url,
        "filename": safe_name,
        "mime": file_mime,
        "size": file_size,
        "room": room,
    }, 201)


def get_upload_file(handler, parsed, params):
    """Serve uploaded files from /uploads/ directory."""
    # Extract filename from path: /uploads/xxxx_name.ext
    filename = parsed.path.replace("/uploads/", "", 1)
    if not filename or ".." in filename or "/" in filename:
        handler.send_json({"error": "Invalid filename"}, 400)
        return

    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(file_path):
        handler.send_json({"error": "File not found"}, 404)
        return

    # Resolve MIME type
    mime, _ = mimetypes.guess_type(filename)
    if not mime:
        mime = "application/octet-stream"

    try:
        with open(file_path, "rb") as f:
            data = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Content-Disposition", f'inline; filename="{filename}"')
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler._send_cors_headers()
        handler.end_headers()
        handler.wfile.write(data)
    except Exception as e:
        handler.send_json({"error": f"Failed to read file: {e}"}, 500)


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_PREFIX_ROUTES = [("/uploads/", get_upload_file)]

# POST /upload uses raw multipart body (no JSON parsing).
# Registered via POST_RAW_ROUTES -- dispatch calls handler directly.
POST_RAW_ROUTES = {"/upload": post_upload_dispatch}
