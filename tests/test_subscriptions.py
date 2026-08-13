import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from main import add_calendar_months, parish_status


class SubscriptionTests(unittest.TestCase):
    def parish(self, **changes):
        values = {
            "plan": "trial",
            "trial_ends_at": datetime.utcnow() + timedelta(days=30, hours=1),
            "grace_ends_at": datetime.utcnow() + timedelta(days=37),
            "paid_until": None,
            "subscription_plan": None,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_trial_reminder_is_due_during_last_30_days(self):
        status = parish_status(self.parish())
        self.assertTrue(status["reminder_due"])
        self.assertEqual(status["days_left"], 30)

    def test_monthly_reminder_is_due_during_last_15_days(self):
        status = parish_status(self.parish(
            plan="active",
            subscription_plan="monthly",
            paid_until=datetime.utcnow() + timedelta(days=15, hours=1),
        ))
        self.assertTrue(status["reminder_due"])

    def test_annual_reminder_is_not_due_with_more_than_30_days(self):
        status = parish_status(self.parish(
            plan="active",
            subscription_plan="annual",
            paid_until=datetime.utcnow() + timedelta(days=31, hours=1),
        ))
        self.assertFalse(status["reminder_due"])

    def test_calendar_months_preserve_real_annual_date(self):
        self.assertEqual(
            add_calendar_months(datetime(2028, 2, 29), 12),
            datetime(2029, 2, 28),
        )

    def test_monthly_reminder_is_not_due_before_15_days(self):
        status = parish_status(self.parish(
            plan="active",
            subscription_plan="monthly",
            paid_until=datetime.utcnow() + timedelta(days=16, hours=1),
        ))
        self.assertFalse(status["reminder_due"])


if __name__ == "__main__":
    unittest.main()
