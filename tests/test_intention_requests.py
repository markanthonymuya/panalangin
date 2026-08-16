import os
import unittest
import base64
import json
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
        self.death_category = models.Category(
            parish_id=self.ctkp.id,
            label="Death Anniversary",
            display_order=3,
        )
        self.db.add_all([
            self.ctkp_category,
            self.other_category,
            self.gift_category,
            self.death_category,
        ])
        self.db.commit()
        self.ctkp_user = SimpleNamespace(parish_id=self.ctkp.id)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_category_settings_are_ordered_and_scoped_to_current_parish(self):
        result = main.update_category_settings(
            main.CategorySettingsUpdate(categories=[
                main.CategorySetting(id=self.death_category.id, is_active=True),
                main.CategorySetting(id=self.ctkp_category.id, is_active=True),
                main.CategorySetting(id=self.gift_category.id, is_active=False),
            ]),
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.assertEqual(result["message"], "Category settings saved")
        active = main.list_categories(current_user=self.ctkp_user, db=self.db)
        self.assertEqual(
            [category["label"] for category in active],
            ["Death Anniversary", "Thanksgiving"],
        )
        self.db.refresh(self.other_category)
        self.assertTrue(self.other_category.is_active)
        self.assertEqual(self.other_category.display_order, 0)

    def test_category_settings_reject_another_parish_category(self):
        with self.assertRaises(HTTPException) as raised:
            main.update_category_settings(
                main.CategorySettingsUpdate(categories=[
                    main.CategorySetting(id=self.ctkp_category.id, is_active=True),
                    main.CategorySetting(id=self.gift_category.id, is_active=True),
                    main.CategorySetting(id=self.other_category.id, is_active=True),
                ]),
                current_user=self.ctkp_user,
                db=self.db,
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_startup_category_sync_preserves_parish_order(self):
        self.ctkp_category.display_order = 9
        self.gift_category.display_order = 0
        self.death_category.display_order = 1
        self.db.commit()
        main.sync_categories(self.db)
        self.db.refresh(self.ctkp_category)
        self.db.refresh(self.gift_category)
        self.db.refresh(self.death_category)
        self.assertEqual(self.ctkp_category.display_order, 9)
        self.assertEqual(self.gift_category.display_order, 0)
        self.assertEqual(self.death_category.display_order, 1)

    def test_disabled_category_is_hidden_from_public_search(self):
        today = date.today()
        self.db.add(models.Intention(
            parish_id=self.ctkp.id,
            category_id=self.gift_category.id,
            name="Hidden Birthday Name",
            offered_by="Test Offeror",
            start_date=today,
            end_date=today,
            is_active=True,
        ))
        self.gift_category.is_active = False
        self.db.commit()
        result = main.search_intentions("ctkp", q="Hidden", db=self.db)
        self.assertEqual(result, {"results": [], "total": 0})

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
                display_parish_name="Christ the King Parish",
                display_parish_color="#336699",
                display_parish_font_family="Verdana",
                display_parish_font_size=18,
                display_parish_font_bold=True,
                display_parish_text_case="proper",
                display_title_font_family="Tahoma",
                display_title_font_size=22,
                display_title_font_bold=True,
                display_title_text_case="lower",
                display_transition_color="#112233",
                display_transition_font_family="Garamond",
                display_transition_font_size=36,
                display_transition_font_bold=True,
                display_transition_text_case="proper",
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
        self.assertEqual(self.ctkp.display_parish_name, "Christ the King Parish")
        self.assertEqual(self.ctkp.display_parish_color, "#336699")
        self.assertEqual(self.ctkp.display_parish_font_family, "Verdana")
        self.assertEqual(self.ctkp.display_parish_font_size, 18)
        self.assertTrue(self.ctkp.display_parish_font_bold)
        self.assertEqual(self.ctkp.display_title_font_family, "Tahoma")
        self.assertEqual(self.ctkp.display_title_font_size, 22)
        self.assertTrue(self.ctkp.display_title_font_bold)
        self.assertEqual(self.ctkp.display_transition_color, "#112233")
        self.assertEqual(self.ctkp.display_transition_font_family, "Garamond")
        self.assertEqual(self.ctkp.display_transition_font_size, 36)
        self.assertTrue(self.ctkp.display_transition_font_bold)

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

    def test_registered_parish_name_is_legacy_display_name_fallback(self):
        self.ctkp.display_parish_name = "Parish Name"
        self.db.commit()

        theme = main.get_theme(
            current_user=self.ctkp_user,
            db=self.db,
        )
        self.assertEqual(
            theme["display_parish_name"],
            "Christ the King Parish",
        )

    def test_display_etag_is_parish_specific_and_returns_not_modified(self):
        first_request = main.Request({
            "type": "http",
            "method": "GET",
            "path": "/api/ctkp/intentions",
            "headers": [],
        })
        first_response = main.get_display_intentions(
            "ctkp",
            first_request,
            db=self.db,
        )
        first_etag = first_response.headers["etag"]
        first_payload = json.loads(first_response.body)
        self.assertEqual(first_payload["parish"], "Christ the King Parish")

        unchanged_request = main.Request({
            "type": "http",
            "method": "GET",
            "path": "/api/ctkp/intentions",
            "headers": [(b"if-none-match", first_etag.encode())],
        })
        unchanged_response = main.get_display_intentions(
            "ctkp",
            unchanged_request,
            db=self.db,
        )
        self.assertEqual(unchanged_response.status_code, 304)

        self.other.theme_bg = "#445566"
        self.db.commit()
        other_parish_change_response = main.get_display_intentions(
            "ctkp",
            unchanged_request,
            db=self.db,
        )
        self.assertEqual(other_parish_change_response.status_code, 304)

        self.ctkp.theme_bg = "#112233"
        self.db.commit()
        changed_response = main.get_display_intentions(
            "ctkp",
            unchanged_request,
            db=self.db,
        )
        self.assertEqual(changed_response.status_code, 200)
        self.assertNotEqual(changed_response.headers["etag"], first_etag)

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

    def test_quick_group_create_supports_multiple_categories(self):
        second_category = models.Category(
            parish_id=self.ctkp.id,
            label="Healing",
            display_order=1,
        )
        self.db.add(second_category)
        self.db.commit()

        result = main.create_batch_intention_request(
            main.BatchIntentionRequestCreate(
                offered_by="Group Offeror",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                intentions=[
                    main.BatchIntentionItem(
                        name="Thanksgiving Name",
                        category_id=self.ctkp_category.id,
                    ),
                    main.BatchIntentionItem(
                        name="Healing Name",
                        category_id=second_category.id,
                    ),
                ],
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
            .all()
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(
            {item.category_id for item in intentions},
            {self.ctkp_category.id, second_category.id},
        )
        self.assertEqual(
            {item.offered_by for item in intentions},
            {"Group Offeror"},
        )
        self.assertEqual(
            {(item.start_date, item.end_date) for item in intentions},
            {(date(2026, 8, 1), date(2026, 8, 31))},
        )

    def test_quick_group_create_rejects_gift_of_life(self):
        with self.assertRaises(HTTPException) as raised:
            main.create_batch_intention_request(
                main.BatchIntentionRequestCreate(
                    offered_by="Birthday Offeror",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 31),
                    intentions=[
                        main.BatchIntentionItem(
                            name="Birthday Name",
                            category_id=self.gift_category.id,
                        ),
                    ],
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

    def test_death_anniversary_uses_one_exact_date_per_name(self):
        first_date = date(2026, 10, 2)
        second_date = date(2026, 11, 4)
        result = main.create_intention_request(
            main.IntentionRequestCreate(
                names=["First Anniversary", "Second Anniversary"],
                offered_by="Memorial Offeror",
                category_id=self.death_category.id,
                birthday_dates={
                    "First Anniversary": first_date,
                    "Second Anniversary": second_date,
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
            .all()
        )
        self.assertEqual(
            {
                item.name: (item.start_date, item.end_date)
                for item in intentions
            },
            {
                "First Anniversary": (first_date, first_date),
                "Second Anniversary": (second_date, second_date),
            },
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
