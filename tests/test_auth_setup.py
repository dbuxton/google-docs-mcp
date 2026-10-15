"""auth_setup.py saves the scopes Google granted, so a later token refresh never asks for more."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

import auth_setup
import docs_edit

# What Google returns when the user leaves some of the requested scopes unticked.
GRANTED = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def token_response(**fields) -> dict:
    return {"access_token": "access", "refresh_token": "refresh", **fields}


class FakeTokenEndpoint:
    """Stands in for google.auth.transport.requests.Request and records the refresh it is sent."""

    def __init__(self):
        self.sent = None

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        self.sent = parse_qs(body.decode())
        data = json.dumps({"access_token": "access", "expires_in": 3600}).encode()
        return SimpleNamespace(status=200, headers={}, data=data)


class SaveTokenTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "token.json"
        # save_token looks the account's email up over the network.
        email = mock.patch.object(auth_setup, "get_email", return_value="me@example.com")
        email.start()
        self.addCleanup(email.stop)

    def save(self, response: dict) -> dict:
        auth_setup.save_token(response, "client-id", "client-secret", self.path)
        return json.loads(self.path.read_text())

    def test_saves_the_scopes_google_granted_not_the_ones_requested(self):
        saved = self.save(token_response(scope=" ".join(GRANTED)))

        self.assertEqual(saved["scopes"], GRANTED)

    def test_falls_back_to_the_requested_scopes_when_google_names_none(self):
        for response in (token_response(), token_response(scope="")):
            with self.subTest(response):
                self.assertEqual(self.save(response)["scopes"], auth_setup.SCOPES)

    def test_a_refresh_asks_only_for_the_scopes_google_granted(self):
        self.save(token_response(scope=" ".join(GRANTED)))
        endpoint = FakeTokenEndpoint()
        env = {name: "" for name in docs_edit.TOKEN_ENV_ALIASES}
        env[docs_edit.TOKEN_ENV_VAR] = str(self.path)

        with mock.patch.dict(os.environ, env), mock.patch(
            "google.auth.transport.requests.Request", return_value=endpoint
        ):
            docs_edit._load_creds()

        self.assertEqual(endpoint.sent["scope"][0].split(), GRANTED)


if __name__ == "__main__":
    unittest.main()
