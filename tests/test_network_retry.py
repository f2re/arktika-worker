"""Focused retry tests using synthetic HTTP responses only."""
import datetime as dt
import threading
import unittest
from email.utils import format_datetime
from unittest import mock

from arktika import network
from arktika.network import Cancelled, Client, Response


class RawResponse:
    def __init__(self, status, headers=None):
        self.code = status
        self.headers = headers or {}

    def close(self):
        pass


class SequenceTransport:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def open(self, *args, **kwargs):
        status, headers = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return Response(RawResponse(status, headers))


class NetworkRetryTests(unittest.TestCase):
    def test_retry_after_delta_date_and_clamp(self):
        self.assertEqual(Client._retry_delay(0, "4"), 4.0)
        self.assertEqual(Client._retry_delay(0, "999"), 30.0)
        with mock.patch.object(network.random, "uniform", return_value=0.5):
            self.assertEqual(Client._retry_delay(0, "invalid"), 0.5)
        retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        delay = Client._retry_delay(0, format_datetime(retry_at, usegmt=True))
        self.assertGreaterEqual(delay, 3.5)
        self.assertLessEqual(delay, 5.0)

    def test_retry_honors_header(self):
        transport = SequenceTransport([(429, {"Retry-After": "2"}), (200, {})])
        client = Client(transport=transport)
        with mock.patch.object(network.time, "sleep") as sleep:
            response = client.open_retry("https://api.gptl.ru/test")
        self.assertEqual(response.status, 200)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(transport.calls, 2)

    def test_backoff_wait_can_be_cancelled(self):
        transport = SequenceTransport([(503, {}), (200, {})])
        client = Client(transport=transport)
        cancelled = threading.Event()
        with mock.patch.object(network.random, "uniform", return_value=0.25):
            with mock.patch.object(cancelled, "wait", return_value=True) as wait:
                with self.assertRaises(Cancelled):
                    client.open_retry("https://api.gptl.ru/test", cancel=cancelled)
        wait.assert_called_once_with(0.25)
        self.assertEqual(transport.calls, 1)

    def test_permanent_status_is_not_retried(self):
        transport = SequenceTransport([(404, {}), (200, {})])
        client = Client(transport=transport)
        response = client.open_retry("https://api.gptl.ru/test")
        self.assertEqual(response.status, 404)
        self.assertEqual(transport.calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
