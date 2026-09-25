"""
The one path every provider write that *sends* goes through: claim, call,
check, complete.

Twilio's message create has no client-reference field and no documented
de-duplication, so a second create is always a second message. Therefore:

* the claim (a unique DB row) is taken before the call, and a request that
  loses it never creates - it answers from the stored record, or, when that
  record is stale or unknown, *looks up* by the reference token embedded in
  the message body;
* an answer we could not read, or no answer at all, is ``unknown`` - never
  ``failed`` - and only the provider's word (found by that lookup) settles it.
"""
import logging
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError
from twilio_sdk.core import ApiError
from twilio_sdk.models import ApiV2010AccountMessage

from . import outcomes
from .claims import ClaimStore
from .errors import OutcomeUnknown
from .gateway import NEVER_SENT, ProviderUnreadable, require_readable, translate_provider_failure
from .models import SmsNotification

logger = logging.getLogger('apps.sms_notifications')

Send = Callable[[str], ApiV2010AccountMessage]
Find = Callable[[str], ApiV2010AccountMessage | None]
StatusMap = Callable[[object], str]


def safe_write(store: ClaimStore, reference: str, fields: dict[str, Any], send: Send, find: Find,
               status_map: StatusMap = outcomes.status_from_provider) -> SmsNotification:
    """
    Returns the record under ``reference`` with its outcome. Raises
    ``OutcomeUnknown`` when the provider may have acted and a lookup could not
    say, and the translated provider error when a first send was refused or
    never left.
    """
    checking = False
    if not store.try_claim(reference, fields):
        existing = store.load(reference)
        if store.is_in_flight(existing):
            return existing                     # another request is sending it: no provider call
        if existing.outcome not in (outcomes.SENDING, outcomes.UNKNOWN):
            return existing                     # settled or pending: answer from it
        checking = True                         # stale sender or unresolved: LOOK, never create

    result: ApiV2010AccountMessage | None = None
    if not checking:
        try:
            result = send(reference)
        except NEVER_SENT as e:
            store.complete(reference, outcomes.FAILED, error='never sent: provider unreachable')
            raise translate_provider_failure(e, is_write=True) from e
        except ApiError as e:
            if e.status_code < 500:
                # A first send the provider refused: nothing was created.
                failure = translate_provider_failure(e, is_write=True)
                store.complete(reference, outcomes.FAILED, error=failure.message)
                raise failure from e
            logger.warning('SMS send %s answered HTTP %s; checking by reference', _short(reference),
                           e.status_code)
        except (httpx.RequestError, ValidationError, ValueError) as e:
            logger.warning('SMS send %s got no readable answer (%s); checking by reference',
                           _short(reference), type(e).__name__)

    snap = None
    if result is not None:
        try:
            snap = require_readable(result)
        except ProviderUnreadable:
            result = None

    if result is None:
        try:
            result = find(reference)
            snap = require_readable(result) if result is not None else None
        except (ApiError, httpx.RequestError, ValidationError, ValueError, ProviderUnreadable) as e:
            store.complete(reference, outcomes.UNKNOWN, error='outcome unknown; lookup failed (%s)'
                           % type(e).__name__)
            raise OutcomeUnknown(reference) from e
        if snap is None:
            store.complete(reference, outcomes.UNKNOWN, error='outcome unknown; not found at provider yet')
            raise OutcomeUnknown(reference)

    assert snap is not None
    return store.complete(reference, status_map(snap.status), snap)


def _short(reference: str) -> str:
    # References carry order numbers, never phone numbers; still keep log lines short.
    return reference[-48:]
