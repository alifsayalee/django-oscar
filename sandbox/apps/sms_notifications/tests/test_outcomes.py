from django.test import SimpleTestCase
from twilio_sdk.models.enums import MessageEnumStatus

from apps.sms_notifications.gateway import cancel_outcome, redact_outcome, status_from_provider
from apps.sms_notifications.models import Outcome
from apps.sms_notifications.views import answer


class StatusMappingTests(SimpleTestCase):

    def test_every_send_status_is_mapped_by_name(self):
        expected = {
            'delivered': Outcome.DONE, 'read': Outcome.DONE,
            'queued': Outcome.PENDING, 'sending': Outcome.PENDING, 'sent': Outcome.PENDING,
            'accepted': Outcome.PENDING, 'scheduled': Outcome.PENDING,
            'failed': Outcome.FAILED, 'undelivered': Outcome.FAILED, 'canceled': Outcome.FAILED,
            'partially_delivered': Outcome.FAILED,
            'receiving': Outcome.UNKNOWN, 'received': Outcome.UNKNOWN,
        }
        self.assertEqual(set(expected), {m.value for m in MessageEnumStatus})
        for value, outcome in expected.items():
            self.assertEqual(status_from_provider(MessageEnumStatus(value)), outcome, value)

    def test_a_status_newer_than_the_sdk_or_absent_is_unknown(self):
        self.assertEqual(status_from_provider('something_new'), Outcome.UNKNOWN)
        self.assertEqual(status_from_provider(None), Outcome.UNKNOWN)

    def test_call_off(self):
        self.assertEqual(cancel_outcome(MessageEnumStatus.CANCELED), Outcome.DONE)
        self.assertEqual(cancel_outcome(MessageEnumStatus.SCHEDULED), Outcome.PENDING)
        self.assertEqual(cancel_outcome(MessageEnumStatus.DELIVERED), Outcome.FAILED)
        self.assertEqual(cancel_outcome('something_new'), Outcome.UNKNOWN)

    def test_redaction(self):
        self.assertEqual(redact_outcome(''), Outcome.DONE)
        self.assertEqual(redact_outcome('still here'), Outcome.UNKNOWN)
        self.assertEqual(redact_outcome(None), Outcome.UNKNOWN)

    def test_a_not_done_outcome_never_answers_success(self):
        for outcome in (Outcome.PENDING, Outcome.SENDING, Outcome.FAILED, Outcome.NEEDS_REVIEW,
                        Outcome.UNKNOWN, Outcome.SKIPPED, 'anything-else'):
            self.assertNotIn(answer(outcome)[0], (200, 201, 204), outcome)
        self.assertEqual(answer(Outcome.DONE)[0], 200)
