import logging
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .auth import csrf_matches, csrf_token
from .errors import PhotoError
from .library import BIN_DAYS, LARGEST_DATABASE_INTEGER, VERSION_LENGTH, Library
from .maintenance import Maintenance
from .signin import SIGN_IN_PATHS, PasswordAuth, add_sign_in_pages
from .sniff import FileType, type_named
from .uploads import MAX_CHUNK_BYTES, OffsetMismatch, Uploads

STATIC_DIR = Path(__file__).resolve().parent / "static"
REQUEST_SLACK_BYTES = 64 * 1024
APP_NAME = "Starfire Photos"
MAX_REQUEST_BYTES = MAX_CHUNK_BYTES + REQUEST_SLACK_BYTES
CLIENT_CHUNK_BYTES = 4 * 1024 * 1024
CSRF_HEADER = "X-CSRF-Token"
OFFSET_HEADER = "Upload-Offset"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = ("/api/health",)
SIGNED_OUT_TEXT = "Sign in through the main sign-in page first."
NESTED_TOO_DEEPLY_TEXT = "that JSON is nested too deeply"
WRONG_HOST_TEXT = ("this app does not answer on that host name (AUTH_MODE=local answers on localhost, 127.0.0.1 or "
                   "[::1] at its own port; AUTH_MODE=proxy answers on the names in ALLOWED_HOSTS)")
OWN_SIGN_IN_WRONG_HOST_TEXT = ("this app does not answer on that host name (the own sign-in answers on the names in "
                               "ALLOWED_HOSTS, or with it empty on localhost, IP addresses and this machine's name)")
MISDIRECTED_REQUEST = 421
RENDITION_MAX_AGE = 365 * 24 * 60 * 60
RENDITION_CACHE = f"private, max-age={RENDITION_MAX_AGE}, immutable"
UNVERSIONED_CACHE = "private, no-cache"
VERSION_PARAMETER = "v"
OFFSET_DIGITS_LIMIT = 18
JPEG_MIME = "image/jpeg"
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "media-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))
MEDIA_CONTENT_SECURITY_POLICY = "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"

log = logging.getLogger("photos.web")


def _error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError as error:
        raise PhotoError(NESTED_TOO_DEEPLY_TEXT) from error
    if not isinstance(data, dict):
        raise PhotoError("send a JSON object")
    return data


def _is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def _is_plain_number(raw: str) -> bool:
    return raw.isascii() and raw.isdigit()


def _offset() -> int:
    raw = request.headers.get(OFFSET_HEADER, "")
    if not _is_plain_number(raw) or len(raw) > OFFSET_DIGITS_LIMIT:
        raise PhotoError(f"send the {OFFSET_HEADER} header with the byte position of this chunk")
    return int(raw)


def _album_param() -> int | None:
    raw = request.args.get("album", "")
    if not raw:
        return None
    if not _is_plain_number(raw) or int(raw) > LARGEST_DATABASE_INTEGER:
        raise PhotoError("no such album", 404)
    return int(raw)


def download_name(name: str, file_type: FileType) -> str:
    suffix = PurePath(name).suffix.lower().lstrip(".")
    return name if suffix in file_type.known_extensions else f"{name}.{file_type.extension}"


def _cache_rule(row) -> str:
    asked_for_this_version = request.args.get(VERSION_PARAMETER) == row["sha256"][:VERSION_LENGTH]
    return RENDITION_CACHE if asked_for_this_version else UNVERSIONED_CACHE


def create_app(library: Library, uploads: Uploads, auth, secret: bytes, maintenance: Maintenance | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    has_own_sign_in = isinstance(auth, PasswordAuth)
    if has_own_sign_in:
        add_sign_in_pages(app, auth, APP_NAME)
    upkeep = maintenance or Maintenance(library, uploads)

    @app.url_value_preprocessor
    def refuse_ids_beyond_the_database(endpoint, values):
        if any(isinstance(value, int) and value > LARGEST_DATABASE_INTEGER for value in (values or {}).values()):
            abort(404)

    @app.before_request
    def identify_and_guard_writes():
        if request.path in PUBLIC_PATHS:
            return None
        if not auth.accepts_host(request.host):
            return _error(OWN_SIGN_IN_WRONG_HOST_TEXT if has_own_sign_in else WRONG_HOST_TEXT, MISDIRECTED_REQUEST)
        if has_own_sign_in and request.path in SIGN_IN_PATHS:
            return None
        user = auth.user_for(request.remote_addr or "", request.headers)
        if user is None:
            if request.path.startswith("/api/"):
                return _error("sign in first", 401)
            if has_own_sign_in:
                return redirect(auth.entry_path(), code=303)
            return Response(SIGNED_OUT_TEXT, status=401, mimetype="text/plain")
        g.user = user
        if request.method in SAFE_METHODS:
            return None
        if not _is_same_origin():
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, user, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        is_media = request.path.startswith("/media/")
        response.headers["Content-Security-Policy"] = MEDIA_CONTENT_SECURITY_POLICY if is_media else CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(OffsetMismatch)
    def offset_mismatch(error: OffsetMismatch):
        return _error(str(error), error.status, received=error.received)

    @app.errorhandler(PhotoError)
    def photo_refused(error: PhotoError):
        return _error(str(error), error.status)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 413:
            return _error("that request is too large", 413)
        if request.path.startswith("/api/"):
            return _error(error.description or error.name, error.code or 500)
        return error

    @app.get("/")
    def index():
        response = send_from_directory(STATIC_DIR, "index.html", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not library.database.is_reachable():
            log.error("health check: the database could not be read at %s", library.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_state():
        return jsonify({"user": g.user, "csrfToken": csrf_token(secret, g.user), "version": __version__,
                        "chunkBytes": CLIENT_CHUNK_BYTES, "maxFileBytes": uploads.max_file_bytes, "binDays": BIN_DAYS,
                        "videoPosters": uploads.tools.can_make_posters})

    @app.get("/api/storage")
    def storage_state():
        return jsonify({**library.storage.space(uploads.outstanding_bytes()).as_json(), **library.totals()})

    @app.get("/api/media")
    def list_media():
        upkeep.run_if_due()
        return jsonify(library.list_media(
            view=request.args.get("view", "timeline"), album_id=_album_param(), query=request.args.get("q", ""),
            date_from=request.args.get("from", ""), date_to=request.args.get("to", ""),
            cursor=request.args.get("cursor", ""), limit=request.args.get("limit")))

    @app.get("/api/media/<int:media_id>")
    def read_media(media_id: int):
        return jsonify(library.get(media_id))

    @app.post("/api/media/bulk")
    def bulk_change():
        body = _body()
        return jsonify(library.bulk(body.get("action"), body.get("ids")))

    @app.post("/api/bin/empty")
    def empty_bin():
        return jsonify({"removed": library.empty_bin()})

    @app.get("/api/albums")
    def list_albums():
        return jsonify({"albums": library.albums()})

    @app.post("/api/albums")
    def create_album():
        return jsonify(library.create_album(_body().get("name"))), 201

    @app.get("/api/albums/<int:album_id>")
    def read_album(album_id: int):
        return jsonify(library.album(album_id))

    @app.patch("/api/albums/<int:album_id>")
    def rename_album(album_id: int):
        return jsonify(library.rename_album(album_id, _body().get("name")))

    @app.delete("/api/albums/<int:album_id>")
    def delete_album(album_id: int):
        library.delete_album(album_id)
        return "", 204

    @app.post("/api/albums/<int:album_id>/add")
    def add_to_album(album_id: int):
        return jsonify(library.add_to_album(album_id, _body().get("ids")))

    @app.post("/api/albums/<int:album_id>/remove")
    def remove_from_album(album_id: int):
        return jsonify(library.remove_from_album(album_id, _body().get("ids")))

    @app.post("/api/uploads")
    def start_upload():
        return jsonify(uploads.start(_body())), 201

    @app.get("/api/uploads/<upload_id>")
    def upload_status(upload_id: str):
        return jsonify(uploads.status(upload_id))

    @app.put("/api/uploads/<upload_id>")
    def upload_chunk(upload_id: str):
        return jsonify(uploads.append(upload_id, _offset(), request.content_length, request.stream))

    @app.post("/api/uploads/<upload_id>/finish")
    def finish_upload(upload_id: str):
        return jsonify(uploads.finish(upload_id))

    @app.delete("/api/uploads/<upload_id>")
    def cancel_upload(upload_id: str):
        uploads.status(upload_id)
        uploads.cancel(upload_id)
        return "", 204

    def _rendition(media_id: int, path_for) -> Response:
        row = library.stored(media_id)
        if not row["has_preview"]:
            abort(404)
        path = path_for(row["sha256"])
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype=JPEG_MIME, conditional=True,
                             download_name=f"{PurePath(row['original_name']).stem or 'photo'}.jpg")
        response.headers["Cache-Control"] = _cache_rule(row)
        return response

    @app.get("/media/<int:media_id>/thumb")
    def thumb(media_id: int):
        return _rendition(media_id, library.storage.thumb_path)

    @app.get("/media/<int:media_id>/preview")
    def preview(media_id: int):
        return _rendition(media_id, library.storage.preview_path)

    @app.get("/media/<int:media_id>/play")
    def play(media_id: int):
        row, path = library.original_file(media_id)
        if row["kind"] != "video" or not path.is_file():
            abort(404)
        response = send_file(path, mimetype=row["mime"], conditional=True,
                             download_name=download_name(row["original_name"], type_named(row["format"])))
        response.headers["Cache-Control"] = _cache_rule(row)
        return response

    @app.get("/media/<int:media_id>/original")
    def original(media_id: int):
        row, path = library.original_file(media_id)
        if not path.is_file():
            log.error("item %s is in the database but its file %s is missing", media_id, path.name)
            abort(404)
        return send_file(path, mimetype=row["mime"], as_attachment=True, conditional=True,
                         download_name=download_name(row["original_name"], type_named(row["format"])))

    return app
