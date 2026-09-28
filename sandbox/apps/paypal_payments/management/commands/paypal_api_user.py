import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ('Create (or update the password of) a user who can log in to the payments API. '
            'Use --staff for an operator who may fulfil, cancel, refund and reconcile.')

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--email', default='')
        parser.add_argument('--staff', action='store_true')
        parser.add_argument('--password', help='Omit to be prompted.')

    def handle(self, *args, username, email, staff, password, **options):
        password = password or getpass.getpass('Password for %s: ' % username)
        if len(password) < 9:
            raise CommandError('Password must be at least 9 characters.')
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        if email:
            user.email = email
        user.is_staff = user.is_staff or staff
        user.set_password(password)
        user.save()
        self.stdout.write('%s %s%s' % ('Created' if created else 'Updated', username,
                                       ' (staff)' if user.is_staff else ''))
