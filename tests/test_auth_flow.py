import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import auth_flow
import station_network as network


class CookieScopeTests(unittest.TestCase):
    def test_prefers_exact_yandex_cookie_scope(self):
        self.assertEqual(
            network._yandex_cookie_host(["id.yandex.ru", ".yandex.ru"]),
            ".yandex.ru",
        )

    def test_token_exchange_keeps_leading_dot(self):
        captured = {}
        original = network.YandexSession._json_request

        def fake_request(_self, _url, **kwargs):
            captured.update(kwargs["headers"])
            return {"access_token": "synthetic-token"}

        network.YandexSession._json_request = fake_request
        try:
            token = network.token_from_cookies(
                json.dumps(
                    [
                        {
                            "name": "Session_id",
                            "value": "synthetic-cookie",
                            "domain": ".yandex.ru",
                            "path": "/",
                        }
                    ]
                )
            )
        finally:
            network.YandexSession._json_request = original

        self.assertEqual(token, "synthetic-token")
        self.assertEqual(captured["Ya-Client-Host"], ".yandex.ru")


class EmbeddedLoginTests(unittest.TestCase):
    records = [
        {
            "name": "Session_id",
            "value": "first-session",
            "domain": ".yandex.ru",
            "path": "/",
        }
    ]

    def test_cookie_gate_waits_and_rate_limits(self):
        gate = auth_flow.LoginCookieGate(stable_seconds=2.0, retry_seconds=12.0)
        self.assertFalse(gate.should_attempt(self.records, 10.0))
        self.assertFalse(gate.should_attempt(self.records, 11.9))
        self.assertTrue(gate.should_attempt(self.records, 12.0))
        self.assertFalse(gate.should_attempt(self.records, 23.9))
        self.assertTrue(gate.should_attempt(self.records, 24.0))

    def test_cookie_change_requires_new_stability_window(self):
        gate = auth_flow.LoginCookieGate(stable_seconds=2.0, retry_seconds=12.0)
        self.assertFalse(gate.should_attempt(self.records, 0.0))
        self.assertTrue(gate.should_attempt(self.records, 2.0))
        changed = [dict(self.records[0], value="final-session")]
        self.assertFalse(gate.should_attempt(changed, 2.1))
        self.assertTrue(gate.should_attempt(changed, 4.2))

    def test_validation_failure_is_not_accepted(self):
        exchange = lambda _value: "synthetic-token"
        validate = lambda _token: (_ for _ in ()).throw(RuntimeError("401"))
        with self.assertRaises(RuntimeError):
            auth_flow.exchange_and_validate_cookie_records(self.records, exchange, validate)

    def test_success_returns_only_after_device_api_validation(self):
        calls = []
        exchange = lambda _value: calls.append("exchange") or "synthetic-token"
        validate = lambda token: calls.append(("validate", token)) or [{"name": "Max"}]
        result = auth_flow.exchange_and_validate_cookie_records(self.records, exchange, validate)

        self.assertEqual(result, ("synthetic-token", [{"name": "Max"}]))
        self.assertEqual(calls, ["exchange", ("validate", "synthetic-token")])

    def test_safe_error_does_not_include_sensitive_message(self):
        class HttpError(RuntimeError):
            code = 401

        try:
            try:
                raise HttpError("cookie=secret")
            except HttpError as cause:
                raise RuntimeError("token=secret") from cause
        except RuntimeError as error:
            self.assertEqual(auth_flow.safe_auth_error(error), "HTTP 401")

    def test_connection_dialog_exposes_one_login_choice(self):
        source = (SRC / "station_max_studio.py").read_text(encoding="utf-8")
        source = source.split("class ConnectionDialog", 1)[1].split("class StationMaxStudio", 1)[0]
        self.assertIn('text="Войти в Яндекс"', source)
        self.assertNotIn("Войти по QR", source)
        self.assertNotIn("Cookies вручную", source)
        self.assertNotIn("Импорт из Home Assistant", source)
        self.assertNotIn("Импорт settings.json", source)


if __name__ == "__main__":
    unittest.main()
