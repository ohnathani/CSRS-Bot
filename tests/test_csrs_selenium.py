import unittest
from unittest.mock import patch

from csrs_selenium import (
    PortalSession,
    _classify_feedback,
    run_selected_enlistments,
)


def _subject(key: str) -> dict:
    return {
        "key": key,
        "cells": [f"PE {key}", "A", "MWF", "8:00 AM"],
        "selectable": True,
        "reason": None,
        "enlist_args": ("student-id", key, f"PE {key}"),
        "category": "PE",
        "action_label": "Click to add",
    }


class SelectedEnlistmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subjects = [_subject("one"), _subject("two"), _subject("three")]
        self.session = PortalSession(
            driver=None,
            tables=[],
            subjects_by_key={subject["key"]: subject for subject in self.subjects},
        )

    def test_rejects_empty_unknown_and_duplicate_selections(self) -> None:
        with patch("csrs_selenium._attempt_subject") as attempt:
            for keys in ([], ["unknown"], ["one", "one"]):
                with self.subTest(keys=keys):
                    with self.assertRaises(ValueError):
                        run_selected_enlistments(self.session, keys)
            attempt.assert_not_called()

    def test_stops_after_unconfirmed_result_but_reports_remaining_keys(self) -> None:
        with patch(
            "csrs_selenium._attempt_subject",
            side_effect=[
                ("failed", "Course full."),
                ("unconfirmed", "No portal confirmation."),
            ],
        ) as attempt:
            results = run_selected_enlistments(
                self.session,
                ["one", "two", "three"],
            )

        self.assertEqual(
            [result["status"] for result in results],
            ["failed", "unconfirmed", "not_attempted"],
        )
        self.assertEqual(attempt.call_count, 2)

    def test_success_requires_known_explicit_success_feedback(self) -> None:
        self.assertEqual(
            _classify_feedback("Enlistment successful."),
            "success",
        )
        self.assertIsNone(_classify_feedback("Request received."))

    def test_public_subject_data_does_not_include_enlist_arguments(self) -> None:
        self.session.tables = [
            {
                "category": "PE",
                "headers": ["Course Number"],
                "subjects": [self.subjects[0]],
            }
        ]

        public = self.session.public_tables()

        self.assertNotIn("enlist_args", public[0]["subjects"][0])


if __name__ == "__main__":
    unittest.main()
