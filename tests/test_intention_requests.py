import os
import unittest
import base64
from datetime import date, timedelta
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SEED_DEMO"] = "false"

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
import models
from database import Base


class IntentionRequestTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = Session()

        self.ctkp = models.Parish(name="Christ the King Parish", slug="ctkp")
        self.other = models.Parish(name="Other Parish", slug="other")
        self.db.add_all([self.ctkp, self.other])
        self.db.flush()
        self.ctkp_category = models.Category(
            parish_id=self.ctkp.id,
            label="Thanksgiving",
            display_order=0,
        )
        self.other_category = models.Category(
            parish_id=self.other.id,
            label="Thanksgiving",
            display_order=0,
        )
        self.gift_category = models.Category(
            parish_id=self.ctkp.id,
            label="Gift of Life",
            display_order=2,
        )
        self.db.add_all([
            self.ctkp_category,
            self.other_category,
            self.gift_category,
        ])
        self.db.commit()
        self.ctkp_user = SimpleNamespace(parish_id=self.ctkp.id)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_display_settings_and_background_are_saved(self):
        image_data = "data:image/png;base64," + base64.b64encode(b"small-image").decode()
        result = main.update_theme(
            main.ThemeUpdate(
                theme_bg="#080c18",
                theme_text="#f0ead6",
                theme_accent="#c9b97a",
                theme_label="#c9b97a",
                dash_accent="#2d5a3d",
                display_interval_seconds=8,
                display_names_per_page=6,
                display_columns=2,
                display_bg_fit="tile",
                background_image=image_data,
                display_font_family="Arial",
                display_font_size=42,
                display_font_bold=True,
                display_text_case="upper",
            ),
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.db.refresh(self.ctkp)
        self.assertEqual(result["message"], "Theme updated")
        self.assertEqual(self.ctkp.display_interval_seconds, 8)
        self.assertEqual(self.ctkp.display_names_per_page, 6)
        self.assertEqual(self.ctkp.display_columns, 2)
        self.assertEqual(self.ctkp.display_bg_image, image_data)
        self.assertEqual(self.ctkp.display_font_family, "Arial")
        self.assertTrue(self.ctkp.display_font_bold)

    def test_display_settings_reject_less_than_four_names(self):
        with self.assertRaises(HTTPException) as raised:
            main.update_theme(
                main.ThemeUpdate(
                    theme_bg="#080c18",
                    theme_text="#f0ead6",
                    theme_accent="#c9b97a",
                    theme_label="#c9b97a",
                    dash_accent="#2d5a3d",
                    display_names_per_page=3,
                ),
                current_user=self.ctkp_user,
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_group_search_returns_only_ranked_near_matches(self):
        today = date.today()
        groups = [
            ("Mark Muya", "Family Intention"),
            ("Mark Anthony Santos", "Thanksgiving Intention"),
            ("Different Offeror", "Mark Dela Cruz"),
            ("Unrelated Person", "Completely Different"),
            ("", "Juan Villanueva"),
        ]
        for offered_by, name in groups:
            request = models.OfferingRequest(
                parish_id=self.ctkp.id,
                offered_by=offered_by,
                start_date=today,
                end_date=today + timedelta(days=7),
            )
            self.db.add(request)
            self.db.flush()
            self.db.add(models.Intention(
                parish_id=self.ctkp.id,
                category_id=self.ctkp_category.id,
                offering_request_id=request.id,
                name=name,
                offered_by=offered_by,
                start_date=today,
                end_date=today + timedelta(days=7),
                is_active=True,
            ))
        self.db.commit()

        results = main.list_intention_requests(
            q="Mark",
            current_user=self.ctkp_user,
            db=self.db,
        )

        self.assertEqual(
            [item["offered_by"] for item in results],
            ["Mark Muya", "Mark Anthony Santos", "Different Offeror"],
        )
        self.assertEqual(results[2]["match_source"], "intention")
        self.assertEqual(results[2]["matched_intention_names"], ["Mark Dela Cruz"])
        unspecified = main.list_intention_requests(
            q="Villanueva",
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.assertEqual(len(unspecified), 1)
        self.assertEqual(unspecified[0]["offered_by"], "")
        self.assertEqual(unspecified[0]["match_source"], "intention")
        self.assertEqual(
            main.list_intention_requests(
                q="",
                current_user=self.ctkp_user,
                db=self.db,
            ),
            [],
        )

    def test_batch_create_groups_all_names_under_one_request(self):
        result = main.create_intention_request(
            main.IntentionRequestCreate(
                names=["First Intention", "Second Intention"],
                offered_by="Test Offeror",
                category_id=self.ctkp_category.id,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=7),
            ),
            current_user=self.ctkp_user,
            db=self.db,
        )

        intentions = (
            self.db.query(models.Intention)
            .filter(models.Intention.parish_id == self.ctkp.id)
            .all()
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(intentions), 2)
        self.assertEqual(
            {row.offering_request_id for row in intentions},
            {result["request_id"]},
        )
        self.assertEqual(
            {row.offered_by for row in intentions},
            {"Test Offeror"},
        )

    def test_batch_create_rejects_another_parish_category(self):
        with self.assertRaises(HTTPException) as raised:
            main.create_intention_request(
                main.IntentionRequestCreate(
                    names=["Should Not Save"],
                    offered_by="Test Offeror",
                    category_id=self.other_category.id,
                    start_date=date.today(),
                    end_date=date.today(),
                ),
                current_user=self.ctkp_user,
                db=self.db,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            self.db.query(models.Intention)
            .filter(models.Intention.parish_id == self.ctkp.id)
            .count(),
            0,
        )

    def test_gift_of_life_uses_one_exact_date_per_name(self):
        first_birthday = date(2026, 8, 10)
        second_birthday = date(2026, 9, 15)
        result = main.create_intention_request(
            main.IntentionRequestCreate(
                names=["First Birthday", "Second Birthday"],
                offered_by="Birthday Offeror",
                category_id=self.gift_category.id,
                birthday_dates={
                    "First Birthday": first_birthday,
                    "Second Birthday": second_birthday,
                },
            ),
            current_user=self.ctkp_user,
            db=self.db,
        )

        intentions = (
            self.db.query(models.Intention)
            .filter(
                models.Intention.offering_request_id
                == result["request_id"]
            )
            .order_by(models.Intention.name)
            .all()
        )
        dates = {
            item.name: (item.start_date, item.end_date)
            for item in intentions
        }
        self.assertEqual(
            dates["First Birthday"],
            (first_birthday, first_birthday),
        )
        self.assertEqual(
            dates["Second Birthday"],
            (second_birthday, second_birthday),
        )

    def test_extension_skips_gift_of_life_intentions(self):
        request = models.OfferingRequest(
            parish_id=self.ctkp.id,
            offered_by="Mixed Offeror",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
        )
        self.db.add(request)
        self.db.flush()
        range_intention = models.Intention(
            parish_id=self.ctkp.id,
            category_id=self.ctkp_category.id,
            name="Range Intention",
            offered_by="Mixed Offeror",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            offering_request_id=request.id,
        )
        gift_intention = models.Intention(
            parish_id=self.ctkp.id,
            category_id=self.gift_category.id,
            name="Birthday Intention",
            offered_by="Mixed Offeror",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 15),
            offering_request_id=request.id,
        )
        self.db.add_all([range_intention, gift_intention])
        self.db.commit()

        result = main.extend_intention_request(
            request.id,
            main.RequestEndDateUpdate(end_date=date(2026, 9, 30)),
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.db.refresh(range_intention)
        self.db.refresh(gift_intention)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["gift_of_life_unchanged"], 1)
        self.assertEqual(range_intention.end_date, date(2026, 9, 30))
        self.assertEqual(gift_intention.end_date, date(2026, 8, 15))

    def test_sweep_is_parish_scoped_and_cleans_only_empty_affected_groups(self):
        expired_date = date.today() - timedelta(days=61)
        current_date = date.today()

        expired_group = self._add_request(
            self.ctkp.id, expired_date, expired_date
        )
        mixed_group = self._add_request(
            self.ctkp.id, expired_date, current_date
        )
        other_group = self._add_request(
            self.other.id, expired_date, expired_date
        )
        self.db.commit()
        expired_group_id = expired_group.id
        mixed_group_id = mixed_group.id
        other_group_id = other_group.id

        result = main.sweep_intentions(
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.db.expire_all()

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["deleted_requests"], 1)
        self.assertIsNone(
            self.db.get(models.OfferingRequest, expired_group_id)
        )
        self.assertIsNotNone(
            self.db.get(models.OfferingRequest, mixed_group_id)
        )
        self.assertIsNotNone(
            self.db.get(models.OfferingRequest, other_group_id)
        )
        self.assertEqual(
            self.db.query(models.Intention)
            .filter(models.Intention.parish_id == self.ctkp.id)
            .count(),
            1,
        )
        self.assertEqual(
            self.db.query(models.Intention)
            .filter(models.Intention.parish_id == self.other.id)
            .count(),
            1,
        )

    def _add_request(self, parish_id, first_end_date, second_end_date):
        category = (
            self.ctkp_category
            if parish_id == self.ctkp.id
            else self.other_category
        )
        request = models.OfferingRequest(
            parish_id=parish_id,
            offered_by="Offeror",
            start_date=min(first_end_date, second_end_date),
            end_date=max(first_end_date, second_end_date),
        )
        self.db.add(request)
        self.db.flush()
        self.db.add(
            models.Intention(
                parish_id=parish_id,
                category_id=category.id,
                name="First",
                offered_by="Offeror",
                start_date=first_end_date,
                end_date=first_end_date,
                offering_request_id=request.id,
            )
        )
        if second_end_date != first_end_date:
            self.db.add(
                models.Intention(
                    parish_id=parish_id,
                    category_id=category.id,
                    name="Second",
                    offered_by="Offeror",
                    start_date=second_end_date,
                    end_date=second_end_date,
                    offering_request_id=request.id,
                )
            )
        return request


if __name__ == "__main__":
    unittest.main()
