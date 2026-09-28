"""
An in-memory stand-in for the parts of Twilio this app uses, plugged in at the
SDK's transport seam (``custom_http_client``) so the real request-building and
decoding pipeline runs and no network call is made.
"""
import itertools
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from twilio_sdk.core import FormBody, HttpRequest, HttpResponse

ACCOUNT_SID = 'AC' + '0' * 32
MESSAGES_PATH = '/2010-04-01/Accounts/%s/Messages' % ACCOUNT_SID


def _json(status, body):
    return HttpResponse(status_code=status, headers={'content-type': 'application/json'},
                        content=json.dumps(body).encode())


class FakeTwilio:
    """Implements the SDK's sync transport protocol (``send`` + ``close``)."""

    def __init__(self, *, deliverable=(), undeliverable=(), page_size=50):
        # number as typed -> canonical form; anything else is not a valid number
        self.valid_numbers = {}
        self.deliverable = set(deliverable)
        self.undeliverable = set(undeliverable)
        self.messages = {}          # sid -> dict (wire shape)
        self.requests = []
        self.page_size = page_size
        self._sids = itertools.count(1)
        self.fail_next = []         # queued failures: ('before', exc) | ('after', exc) | ('status', code)
        self.now = lambda: datetime.now(timezone.utc)

    def close(self):
        pass

    # -- helpers for tests -----------------------------------------------------------------------

    def add_number(self, typed, canonical, country='CA'):
        self.valid_numbers[typed] = (canonical, country)

    def writes(self, method='POST'):
        return [r for r in self.requests if r.method == method and 'Messages' in r.url]

    def creates(self):
        return [r for r in self.requests if r.method == 'POST' and urlsplit(r.url).path.endswith('/Messages.json')]

    def add_message(self, to, from_, body, status='delivered', date_sent=None):
        sid = 'SM%032d' % next(self._sids)
        when = date_sent or self.now()
        self.messages[sid] = {
            'sid': sid, 'account_sid': ACCOUNT_SID, 'to': to, 'from': from_, 'body': body,
            'status': status, 'date_created': format_datetime(when), 'date_sent': format_datetime(when),
            'date_updated': format_datetime(when), 'error_code': None, 'error_message': None,
            'direction': 'outbound-api', 'num_segments': '1', 'num_media': '0', 'price': None,
            'price_unit': 'USD', 'api_version': '2010-04-01', 'messaging_service_sid': None,
            'uri': '%s/%s.json' % (MESSAGES_PATH, sid), 'subresource_uris': {},
        }
        return self.messages[sid]

    def settle(self):
        """What the carrier eventually reports: delivered or undelivered, per destination."""
        for m in self.messages.values():
            if m['status'] in ('queued', 'sent', 'accepted'):
                if m['to'] in self.undeliverable:
                    m['status'], m['error_code'] = 'undelivered', 30034
                else:
                    m['status'] = 'delivered'
                m['date_sent'] = format_datetime(self.now())

    # -- transport -------------------------------------------------------------------------------

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        mode = None
        if self.fail_next:
            mode = self.fail_next.pop(0)
            if mode[0] == 'before':
                raise mode[1]
            if mode[0] == 'status':
                return _json(mode[1], {'code': 20003, 'message': 'Authenticate', 'status': mode[1]})
        response = self._route(request)
        if mode and mode[0] == 'after':
            raise mode[1]           # it happened, but the answer is lost
        return response

    def _route(self, request):
        url = urlsplit(request.url)
        path = url.path
        if url.netloc == 'lookups.twilio.com' and path.startswith('/v2/PhoneNumbers/'):
            number = httpx.URL(request.url).path.rsplit('/', 1)[1]
            if number in self.valid_numbers:
                canonical, country = self.valid_numbers[number]
                return _json(200, {'valid': True, 'phone_number': canonical, 'country_code': country,
                                   'calling_country_code': '1', 'national_format': canonical,
                                   'validation_errors': []})
            return _json(200, {'valid': False, 'phone_number': number, 'country_code': None,
                               'validation_errors': ['INVALID_BUT_POSSIBLE']})
        if path == MESSAGES_PATH + '.json' and request.method == 'POST':
            return self._create(request)
        if path == MESSAGES_PATH + '.json' and request.method == 'GET':
            return self._list(url)
        if path.startswith(MESSAGES_PATH + '/'):
            sid = path.rsplit('/', 1)[1][:-len('.json')]
            message = self.messages.get(sid)
            if message is None:
                return _json(404, {'code': 20404, 'message': 'Not found', 'status': 404})
            if request.method == 'GET':
                return _json(200, message)
            if request.method == 'POST':
                return self._update(message, request)
        return _json(404, {'code': 20404, 'message': 'Not found', 'status': 404})

    @staticmethod
    def _fields(request):
        assert isinstance(request.body, FormBody)
        return dict(request.body.fields)

    def _create(self, request):
        fields = self._fields(request)
        scheduled = fields.get('ScheduleType') == 'fixed'
        if scheduled and not fields.get('MessagingServiceSid'):
            return _json(400, {'code': 35111, 'message': 'Scheduling needs a messaging service', 'status': 400})
        message = self.add_message(fields['To'], fields.get('From'), fields.get('Body', ''),
                                   status='scheduled' if scheduled else 'queued')
        message['messaging_service_sid'] = fields.get('MessagingServiceSid')
        message['date_sent'] = None
        return _json(201, message)

    def _update(self, message, request):
        fields = self._fields(request)
        if fields.get('Status') == 'canceled':
            if message['status'] != 'scheduled':
                return _json(400, {'code': 30409, 'message': 'Message cannot be canceled', 'status': 400})
            message['status'] = 'canceled'
        if 'Body' in fields:
            if fields['Body'] != '':
                return _json(400, {'code': 21610, 'message': 'Body can only be redacted', 'status': 400})
            message['body'] = ''
        return _json(200, message)

    def _list(self, url):
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        items = sorted(self.messages.values(), key=lambda m: m['sid'], reverse=True)
        if 'To' in query:
            items = [m for m in items if m['to'] == query['To']]
        if 'From' in query:
            items = [m for m in items if m['from'] == query['From']]
        for key, keep in (('DateSent>', lambda d, b: d >= b), ('DateSent<', lambda d, b: d <= b)):
            if key in query:
                bound = datetime.fromisoformat(query[key].replace('Z', '+00:00'))
                items = [m for m in items if m['date_sent'] and keep(_parse(m['date_sent']), bound)]
        size = min(int(query.get('PageSize', 50)), self.page_size)
        start = int(query.get('PageToken', '0') or 0)
        page = items[start:start + size]
        next_uri = None
        if start + size < len(items):
            next_query = dict(query, Page=str(int(query.get('Page', '0')) + 1), PageToken=str(start + size))
            next_uri = '%s.json?%s' % (MESSAGES_PATH, urlencode(next_query))
        return _json(200, {'messages': page, 'next_page_uri': next_uri, 'page': int(query.get('Page', '0')),
                           'page_size': size, 'start': start, 'end': start + len(page),
                           'uri': url.path, 'first_page_uri': url.path, 'previous_page_uri': None})


def _parse(value):
    from email.utils import parsedate_to_datetime
    return parsedate_to_datetime(value)
