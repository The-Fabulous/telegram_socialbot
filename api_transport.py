"""Browser access to the API from the separately hosted Pages frontend."""

import os

from aiohttp import web


ALLOWED_ORIGINS = frozenset(
    origin.strip().rstrip("/")
    for origin in os.getenv("ALLOWED_ORIGINS", "https://the-fabulous.github.io").split(",")
    if origin.strip()
)
ALLOWED_HEADERS = frozenset({"authorization", "content-type", "x-init-data"})


def session_token(request: web.Request, cookie_name: str) -> str:
    """An explicit Authorization header takes precedence over ambient cookies."""
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""
    return request.cookies.get(cookie_name, "")


@web.middleware
async def cors(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)

    origin = request.headers.get("Origin")
    # Same-origin requests retain the existing HttpOnly cookie authentication.
    own_origin = f"{request.scheme}://{request.host}"
    public_origin = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if origin and origin not in ALLOWED_ORIGINS and origin not in {own_origin, public_origin}:
        return web.json_response({"error": "Этот сайт не имеет доступа к API."}, status=403)

    if request.method == "OPTIONS":
        requested_headers = {
            value.strip().lower()
            for value in request.headers.get("Access-Control-Request-Headers", "").split(",")
            if value.strip()
        }
        if request.headers.get("Access-Control-Request-Method") != "POST" or not requested_headers <= ALLOWED_HEADERS:
            return web.json_response({"error": "Недопустимый запрос."}, status=403)
        response = web.Response(status=204)
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Init-Data"
        response.headers["Access-Control-Max-Age"] = "600"
    else:
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = error

    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    return response
