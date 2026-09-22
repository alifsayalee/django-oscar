"""Create/refresh a demo shopper and operator for exercising the PayPal API.

For local sandbox verification only. Passwords are supplied on the command line
(or defaulted) and are never read from or written to the repository.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or refresh a demo shopper and staff operator for the PayPal API."

    def add_arguments(self, parser):
        parser.add_argument("--shopper-email", default="shopper1@example.com")
        parser.add_argument("--shopper-password", default="ShopPass123!")
        parser.add_argument("--operator-email", default="operator1@example.com")
        parser.add_argument("--operator-password", default="OpPass123!")

    def handle(self, *args, **options):
        User = get_user_model()
        for email, password, is_staff, uname in (
            (options["shopper_email"], options["shopper_password"], False, "shopper1"),
            (options["operator_email"], options["operator_password"], True, "operator1"),
        ):
            defaults = {"email": email, "is_staff": is_staff, "is_active": True}
            user, _ = User.objects.get_or_create(username=uname, defaults=defaults)
            user.email = email
            user.is_staff = is_staff
            user.is_active = True
            user.set_password(password)
            user.save()
            role = "operator (is_staff)" if is_staff else "shopper"
            self.stdout.write(self.style.SUCCESS(f"{role}: {email} (id={user.id})"))
