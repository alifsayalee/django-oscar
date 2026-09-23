"""HTTP plumbing for the JSON API: body parsing, JSON responses, auth guards and
a single error boundary that turns domain errors into JSON.

Authentication reuses Django's own session login (the way the sandbox already
authenticates); the caller's identity is taken from ``request.user``. Operator
endpoints additionally require ``is_staff``.

These endpoints are CSRF-exempt: they are a headless JSON API driven by a session
cookie (curl / scripts / tests), not browser forms. Identity still comes from the
authenticated session and authorization from the staff check. A browser-facing
deployment would layer CSRF or token auth on top.
"""

import functools
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import errors


def json_response(data, status=200):
    return JsonResponse(data, status=status)


def error_response(exc):
    body = {"error": {"message": exc.message, "type": exc.__class__.__name__}}
    if getattr(exc, "outcome_unknown", False):
        body["error"]["outcomeUnknown"] = True
    return JsonResponse(body, status=exc.status_code)


def parse_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise errors.ApiValidationError("Request body must be valid JSON.")
    if not isinstance(data, dict):
        raise errors.ApiValidationError("Request body must be a JSON object.")
    return data


def api_endpoint(methods, *, login=True, staff=False):
    """Wrap a view: method allow-list, session auth, staff gate, error boundary."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return JsonResponse(
                    {"error": {"message": "Method not allowed.", "type": "MethodNotAllowed"}},
                    status=405,
                )
            if login and not request.user.is_authenticated:
                return JsonResponse(
                    {"error": {"message": "Authentication required.", "type": "NotAuthenticated"}},
                    status=401,
                )
            if staff and not request.user.is_staff:
                return JsonResponse(
                    {"error": {"message": "Operator access required.", "type": "Forbidden"}},
                    status=403,
                )
            try:
                return view(request, *args, **kwargs)
            except errors.PayPalError as exc:
                return error_response(exc)

        return wrapper

    return decorator
