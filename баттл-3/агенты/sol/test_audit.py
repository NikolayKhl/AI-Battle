import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from audit import (
    Anonymizer,
    AuditError,
    DealBundle,
    Evidence,
    Violation,
    format_note,
    selected_leads,
    validate_model_result,
    working_seconds,
)


class AuditTests(unittest.TestCase):
    def test_anonymizer_masks_known_and_discovered_pii(self):
        masker = Anonymizer()
        masker.add_person("Ольга Кузнецова")
        masker.add_person("Сергей")
        source = (
            "Менеджер: Сергей, здравствуйте! Меня зовут Ольга. "
            "Пишите на test@example.ru или +7 (999) 123-45-67"
        )
        masked = masker.mask(source)
        masker.assert_clean(masked)
        self.assertNotIn("Ольга", masked)
        self.assertNotIn("Сергей", masked)
        self.assertNotIn("example.ru", masked)
        self.assertNotIn("123-45-67", masked)

    def test_working_seconds_skips_night_and_weekend(self):
        timezone = ZoneInfo("Europe/Moscow")
        friday = dt.datetime(2026, 8, 7, 18, 30, tzinfo=timezone)
        monday = dt.datetime(2026, 8, 10, 9, 30, tzinfo=timezone)
        self.assertEqual(working_seconds(friday, monday, set()), 3600)

    def test_selected_leads_is_exact_and_ordered_by_request(self):
        leads = [
            {"id": 1, "name": "D-01 тест", "custom_fields_values": []},
            {"id": 2, "name": "D-02 тест", "custom_fields_values": []},
        ]
        chosen = selected_leads(leads, ["d-02", "D-01"], False)
        self.assertEqual([row["id"] for row in chosen], [2, 1])
        with self.assertRaises(AuditError):
            selected_leads(leads, ["D-03"], False)

    def test_note_format_is_exact(self):
        violation = Violation("3.2", "согласился и пропал", "E1", "Понимаю", "Понимаю", "чат", "01.08 12:40")
        self.assertEqual(
            format_note([violation]),
            "АУДИТ SOL\nНарушений: 1\n"
            "3.2 — согласился и пропал. Цитата: «Понимаю» (чат, 01.08 12:40)",
        )
        self.assertEqual(format_note([]), "АУДИТ SOL\nНарушений: нет")

    def test_model_citation_must_be_exact(self):
        masker = Anonymizer()
        evidence = Evidence("E1", 1, "01.08 10:00", "менеджер", "чат", "Цена 50 тысяч", "Цена 50 тысяч")
        bundle = DealBundle(
            code="D-01",
            lead={"price": 1},
            notes=[],
            tasks=[],
            events=[],
            contacts=[],
            manager="М",
            client="К",
            status_name="Открыта",
            outcome="открыта",
            evidence=[evidence],
            payload={},
            prompt="",
            anonymizer=masker,
        )
        raw = {
            "violations": [{"rule": "2.1", "summary": "цена дана слишком рано", "evidence_id": "E1", "quote": "Цена 50 тысяч"}],
            "applicable_checks": [3],
            "result_assessment": "",
            "recoverability": "open_actionable",
            "next_action": "Позвонить",
        }
        result = validate_model_result(bundle, raw, {"2.1"})
        self.assertEqual(result.violations[0].rule, "2.1")
        raw["violations"][0]["quote"] = "выдуманная цитата"
        with self.assertRaises(AuditError):
            validate_model_result(bundle, raw, {"2.1"})


if __name__ == "__main__":
    unittest.main()
