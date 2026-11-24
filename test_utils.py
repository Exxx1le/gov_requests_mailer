import json
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import call, patch

import requests

import utils


class FetchDocumentsByDateTests(unittest.TestCase):
    date = "16.11.2026"
    authority_id = "8005d8c9-4b6d-48d3-861a-2a37e69fccb3"

    def setUp(self):
        get_patch = patch("utils.requests.get")
        self.get = get_patch.start()
        self.addCleanup(get_patch.stop)
        sleep_patch = patch("utils.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def response(self, status=200, *, body=b'{"items": []}'):
        response = requests.Response()
        response.status_code = status
        response.url = utils.API_URL
        response.encoding = "utf-8"
        response.raw = BytesIO(body)
        return response

    def fetch(self):
        return utils.fetch_documents_by_date(self.date, self.authority_id)

    def test_passes_timeout_and_keeps_existing_filter(self):
        included = {"documentTypeId": "another-type", "eoNumber": "123"}
        excluded = {"documentTypeId": "7ff5b3b5-3757-44f1-bb76-3766cabe3593"}
        without_type = {"eoNumber": "456"}
        self.get.return_value = self.response(body=json.dumps({
            "items": [included, excluded, without_type],
        }).encode("utf-8"))

        self.assertEqual(self.fetch(), [included, without_type])
        self.get.assert_called_once_with(utils.API_URL, params={
            "periodType": "day",
            "date": self.date,
            "SignatoryAuthorityId": self.authority_id,
        }, timeout=30)
        self.sleep.assert_not_called()

    def test_returns_empty_list_for_a_valid_empty_response(self):
        self.get.return_value = self.response()

        self.assertEqual(self.fetch(), [])
        self.sleep.assert_not_called()

    def test_retries_network_errors_with_increasing_delays(self):
        self.get.side_effect = [
            requests.exceptions.Timeout("timed out"),
            requests.exceptions.ConnectionError("connection reset"),
            self.response(),
        ]

        with self.assertLogs("utils", level="WARNING") as logs:
            self.assertEqual(self.fetch(), [])

        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(len(logs.records), 2)
        for attempt, reason in enumerate(("timed out", "connection reset"), start=1):
            record = logs.records[attempt - 1]
            for field in (self.date, self.authority_id, f"{attempt}/3", reason):
                self.assertIn(field, record.getMessage())

    def test_retries_429_and_5xx(self):
        self.get.side_effect = [self.response(429), self.response(503), self.response()]

        with self.assertLogs("utils", level="WARNING"):
            self.assertEqual(self.fetch(), [])

        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_raises_last_http_error_after_three_attempts(self):
        final_response = self.response(502)
        self.get.side_effect = [self.response(500), self.response(429), final_response]

        with self.assertLogs("utils", level="WARNING") as logs:
            with self.assertRaises(requests.exceptions.HTTPError) as raised:
                self.fetch()

        self.assertIs(raised.exception.response, final_response)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(logs.records[-1].levelname, "ERROR")
        for field in (self.date, self.authority_id, "3/3", "502"):
            self.assertIn(field, logs.records[-1].getMessage())

    def test_raises_other_client_errors_without_retrying(self):
        self.get.return_value = self.response(404)

        with self.assertLogs("utils", level="ERROR"):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.fetch()

        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_raises_for_invalid_json_or_unexpected_structure(self):
        for body in (b"not json", b"[]", b"{}", b'{"items": null}', b'{"items": [42]}'):
            with self.subTest(body=body):
                self.get.reset_mock()
                self.get.return_value = self.response(body=body)

                with self.assertLogs("utils", level="ERROR"):
                    with self.assertRaises(ValueError):
                        self.fetch()

                self.get.assert_called_once()
                self.sleep.assert_not_called()

    def test_fetch_failure_prevents_saving_even_after_receiving_documents(self):
        self.get.side_effect = [
            self.response(body=b'{"items": [{"eoNumber": "123"}]}'),
            *[requests.exceptions.Timeout("timed out") for _ in range(3)],
        ]
        day = date(2026, 11, 16)

        with patch("utils.save_to_doc") as save, self.assertLogs("utils", level="WARNING"):
            with self.assertRaises(requests.exceptions.Timeout):
                utils.process_documents(day, day)

        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
