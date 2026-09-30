"""Common credential formats never reach a hosted provider, and ordinary code still does.

Both outgoing screens (the typed query path and the decision engine path) use
one rule set. The screen is best effort, not a data-loss-prevention product:
this corpus pins what it must block and what it must let through, so the
rules cannot drift in either direction. Every value is synthetic.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import AutoError, require_clean, screen as auto_screen  # noqa: E402
from jevkit.security import screen as typed_screen  # noqa: E402
from src.adl.queries.typed import QueryError, _prepare_query_with_policy  # noqa: E402

MUST_BLOCK = {
    "encrypted private key": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIFHzBJBgkqhkiG9w0BBQ0w\n-----END ENCRYPTED PRIVATE KEY-----",
    "DSA private key": "-----BEGIN DSA PRIVATE KEY-----\nMIIBuwIBAAKBgQ\n-----END DSA PRIVATE KEY-----",
    "PGP private key": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBFAKE\n-----END PGP PRIVATE KEY BLOCK-----",
    "PuTTY key": "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none",
    "JSON password in text": 'config = {"password": "Sup3r-S3cret-Value"}',
    "JSON api_key in text": '{"api_key": "abcdEFGHijklMNOP1234"}',
    "quoted code assignment": 'DATABASE_PASSWORD = "k9#Lm2!pQ"',
    "long letters-only quoted password": "password = 'correcthorsebatterystaple'",
    "AWS secret key": "aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYFAKEKEY00",
    "env file password": "DB_PASSWORD=supersecretvalue",
    "env file with export": "export CLIENT_SECRET=a1b2c3d4e5f6",
    "yaml password": "database:\n  password: hunter2hunter",
    "inline api key": "call it with api_key=synthetic-secret-value-12345 today",
    "Basic auth header": "Authorization: Basic dXNlcjpGQUtFLXBhc3N3b3Jk",
    "Basic auth without digits": "Authorization: Basic dXNlcjpwYXNz",
    "Bearer token": "Bearer syntheticBearerToken123456789",
    "Bearer lowercase token": "Bearer abcdefgh",
    "curl user and password": "curl -u deploy:Pa55word-x https://example.com/api",
    "credential in URL": "postgres://app:Pa55-word@db.example.com:5432/app",
    # Token-shaped samples are split in the source so repository secret scanners do not flag them.
    "Slack bot token": "xox" "b-000000000000-000000000000-FAKEFAKEFAKEFAKEFAKEFAKE",
    "Google API key": "AIzaSyFAKE-FAKEFAKEFAKEFAKEFAKEFAKE0000",
    "Stripe live key": "sk_" "live_FAKEFAKEFAKEFAKEFAKEFAKE",
    "Stripe webhook secret": "whsec_FAKEFAKEFAKEFAKEFAKEFAKE1234",
    "GitHub fine-grained token": "github_pat_11FAKEFAKE0000000000_FAKEFAKEFAKEFAKEFAKEFAKEFAKE",
    "GitHub classic token": "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE0000",
    "GitLab token": "glpat-FAKEFAKEFAKEFAKEFAKE",
    "npm token": "npm_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
    "PyPI token": "pypi-AgEIcHlwaS5vcmcFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
    "Hugging Face token": "hf" "_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFA",
    "SendGrid key": "SG.FAKEFAKEFAKEFAKEFAKE12.FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234567",
    "OpenAI key": "sk-proj-FAKEFAKEFAKEFAKEFAKEFAKE",
    "zero-width split key": "sk-proj-FAKE​FAKEFAKEFAKEFAKEFAKE",
    "full-width lookalike key": "ｓｋ-proj-FAKEFAKEFAKEFAKEFAKEFAKE",
    "AWS access key id": "AKIAFAKEFAKEFAKE0000",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.FAKEsignatureFAKE",
    "Azure storage key": "DefaultEndpointsProtocol=https;AccountName=acme;AccountKey=FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE==",
    "Azure service bus key": "Endpoint=sb://x.servicebus.windows.net/;SharedAccessKeyName=Root;SharedAccessKey=FAKEFAKEFAKEFAKEFAKE0=",
    "Azure SAS URL": "https://acct.blob.core.windows.net/c/f?sv=2024-01-01&sig=FAKEFAKEFAKEFAKEFAKEFAKE%3D",
    "dict key token": {"token": "s3ss-t0k-1234567890"},
    "dict key auth_token": {"auth_token": "s3ss-t0k-1234567890"},
    "password starting with my": "password: my-dog-rex-2019",
    "dict key camelCase": {"clientSecret": "anything-at-all"},
    "dict key apiKey": {"apiKey": "anything-at-all"},
    "dict key password": {"metadata": {"password": "synthetic-password-value"}},
    "YAML password, letters only": "database:\n  password: sunshinemeadow\n",
    "compose map style": "environment:\n  POSTGRES_PASSWORD: bluelagoonriver\n",
    "INI letters only": "[db]\npassword = orchardwhistle\n",
    "TOML quoted short word": 'password = "hunter"',
    "Python keyword argument": 'connect(host="db", password="hunter")',
    "Redis URL with an empty user": "redis://:s3cretpass@cache.example.com:6379/0",
    "AMQP URL with an empty user": "amqp://:orchardwhistle@queue.example.com/",
    "Docker registry auth": {"auths": {"registry.example.com": {"auth": "dXNlcjpzM2NyZXQtcGFzcw=="}}},
    "credential dict named criteria inside the state": {"state": {"criteria": {"password": "hunter2hunter"}},
                                                         "questions": {}},
    "Docker registry auth as text": '{"auths": {"registry.example.com": {"auth": "dXNlcjpzM2NyZXQtcGFzcw=="}}}',
}

MUST_PASS = {
    "reading a password": 'password = input("Password: ")',
    "getpass": "password = getpass.getpass()",
    "attribute copy": "self.password = password",
    "comparison": "if password == confirm_password:",
    "type annotation": "password: str",
    "settings reference": "password = settings.DB_PASSWORD",
    "environment lookup": 'api_key = os.environ["API_KEY"]',
    "length setting": "PASSWORD_MIN_LENGTH = 12",
    "hash field": 'password_hash = "sha256"',
    "translation label": '"password": "Passwort"',
    "translation sentence": '"password": "Enter your password"',
    "placeholder variable": "DB_PASSWORD=${DB_PASSWORD}",
    "placeholder angle": "DB_PASSWORD=<your-password>",
    "placeholder word": "api_key=changeme",
    "example prefix": 'api_key = "your-api-key-here"',
    "labelled fake token": {"token": "fake-token-for-the-docs"},
    "kubernetes secret name": "secretName: tls-secret-2",
    "tokenizer": "token = tokenizer.encode(text)",
    "token count": {"max_tokens": 512, "token": 3},
    "prose about auth": "Bearer authentication is required; Basic authentication is disabled.",
    "prose about passwords": "Password: reset links expire after one hour.",
    "working directory": "PWD=/srv/app/current",
    "word containing sk": "The task-management-framework-v2 release notes",
    "boolean flags": {"password": False, "secret": ""},
    "diff that reads a password": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-user = input()\n+password = input()\n",
    "OpenAPI bearer scheme": "components:\n  securitySchemes:\n    bearerAuth:\n      type: http\n      scheme: bearer\n      bearerFormat: JWT",
    "curl placeholder credentials": "Use `curl -u username:password https://api.example.com/` to test basic authentication.",
    "URL with placeholder credentials": "Connect with mongodb://<username>:<password>@cluster0.example.net/test",
    "URL with the word password": "postgres://user:password@db.example.com/app",
    "YAML password field that is required": "password:\n  type: string\n  required: true",
    "YAML schema word": "password: required",
    "YAML value source": "passphrase: prompt",
    "route labels named like credentials": {"questions": {"pick": {"type": "choice", "instructions": "Which queue?",
                                             "criteria": {"password": "Sign-in problems and password resets",
                                                          "token": "Expired or missing access token",
                                                          "authorization": "Permission requests",
                                                          "hardware": "Laptop faults"}}}},
}


def _blocked(value) -> tuple[bool, bool]:
    return bool(auto_screen(value, allow_context=True)), bool(typed_screen(value, allow_context=True)[1])


class MustBlockTests(unittest.TestCase):
    def test_every_credential_shape_is_blocked_by_both_screens(self):
        for label, value in MUST_BLOCK.items():
            with self.subTest(label=label):
                self.assertEqual(_blocked(value), (True, True))
                with self.assertRaisesRegex(AutoError, "SENSITIVE_PAYLOAD_NOT_SENT"):
                    require_clean(value, allow_context=True)

    def test_a_typed_query_carrying_any_credential_is_refused_even_under_jev_maximum(self):
        policy = {"provider": "typesafe", "generic_query_enabled": True, "decision_mode": "jev-maximum",
                  "data_classification": "restricted"}
        questions = {"q": {"type": "noul", "instructions": "Is this about configuration?"}}
        for label, value in MUST_BLOCK.items():
            with self.subTest(label=label), self.assertRaisesRegex(QueryError, "INPUT_DATA_BLOCKED"):
                _prepare_query_with_policy({"text": value}, questions, provider="typesafe", policy=policy,
                                           data_classification="restricted")

    def test_a_url_encoded_home_path_counts_as_a_home_path(self):
        value = "open file%3A%2F%2F%2FUsers%2Falice%2Fclients%2Fplan.docx"
        self.assertIn("HOME_PATH", auto_screen(value))
        self.assertIn("HOME_PATH", typed_screen(value)[1])

    def test_findings_are_labels_and_never_the_matched_text(self):
        findings = auto_screen(MUST_BLOCK["JSON password in text"], allow_context=True)
        self.assertTrue(findings)
        self.assertFalse(any("Sup3r" in label for label in findings))
        cleaned, _ = typed_screen(MUST_BLOCK["JSON password in text"], allow_context=True)
        self.assertNotIn("Sup3r-S3cret-Value", str(cleaned))


class ScreenCostTests(unittest.TestCase):
    """A long line of repetitive text is screened in bounded time, not quadratic time."""

    def test_adversarial_100_kb_lines_screen_quickly(self):
        import time
        for label, text in (("dotted", "a." * 50_000), ("kebab", "ab-" * 33_000), ("words", "abc " * 25_000),
                            ("at-less email run", "a" * 100_000), ("scheme run", "a+" * 50_000),
                            ("colon run", "x:" * 50_000), ("equals run", "password=" * 11_000)):
            with self.subTest(shape=label):
                started = time.monotonic()
                auto_screen(text, allow_context=True)
                typed_screen(text, allow_context=True)
                self.assertLess(time.monotonic() - started, 2.0)


class MustPassTests(unittest.TestCase):
    def test_ordinary_code_and_prose_pass_both_screens(self):
        for label, value in MUST_PASS.items():
            with self.subTest(label=label):
                self.assertEqual(_blocked(value), (False, False))
                require_clean(value, allow_context=True)


if __name__ == "__main__":
    unittest.main()
