"""Documented vendor token formats are blocked, and look-alike ordinary text is not.

Each sample is generated to the vendor's documented shape with random
characters, so no real credential appears here.
"""

from __future__ import annotations

import random
import string
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins" / "qualixar-jev-decision-layer" / "runtime"
sys.path.insert(0, str(RUNTIME))

from jev_auto.common import screen as auto_screen  # noqa: E402
from jevkit.security import screen as typed_screen  # noqa: E402

RNG = random.Random(1013)
ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"


def r(alphabet: str, n: int) -> str:
    return "".join(RNG.choice(alphabet) for _ in range(n))


SAMPLES = {
    "AWS temporary key": "ASIA" + r(string.ascii_uppercase + "234567", 16),
    "AWS A3T key": "A3TX" + r(string.ascii_uppercase + "234567", 16),
    "Amazon Bedrock key": "ABSK" + r(ALNUM, 120),
    "1Password secret key": "A3-" + r(string.ascii_uppercase + string.digits, 6) + "-" + r(string.ascii_uppercase, 26),
    "age secret key": "AGE-SECRET-KEY-1" + r(string.ascii_uppercase + string.digits, 58),
    "Adobe client secret": "p8e-" + r(ALNUM, 32),
    "Alibaba access key": "LTAI" + r(ALNUM, 20),
    "Databricks token": "dapi" + r(HEX, 32),
    "DigitalOcean token": "dop_v1_" + r(HEX, 64),
    "Doppler token": "dp.pt." + r(ALNUM, 43),
    "Dynatrace token": "dt0c01." + r(ALNUM, 24) + "." + r(ALNUM, 64),
    "EasyPost key": "EZAK" + r(ALNUM, 54),
    "Facebook page token": "EAAC" + r(ALNUM, 120),
    "fly.io token": "fo1_" + r(ALNUM, 43),
    "GitLab feed token": "glft-" + r(ALNUM, 20),
    "GitLab runner registration": "GR1348941" + r(ALNUM, 20),
    "Grafana cloud token": "glc_" + r(ALNUM, 40),
    "Grafana service account": "glsa_" + r(ALNUM, 32) + "_" + r(HEX, 8),
    "Heroku key": "HRKU-AA" + r(ALNUM, 58),
    "Hugging Face org token": "api_org_" + r(string.ascii_letters, 34),
    "Linear key": "lin_api_" + r(ALNUM, 40),
    "Notion token": "ntn_" + r(string.digits, 11) + r(ALNUM, 35),
    "OpenShift token": "sha256~" + r(ALNUM, 43),
    "Perplexity key": "pplx-" + r(ALNUM, 48),
    "PlanetScale password": "pscale_pw_" + r(ALNUM, 40),
    "Postman key": "PMAK-" + r(HEX, 24) + "-" + r(HEX, 34),
    "Prefect key": "pnu_" + r(ALNUM, 36),
    "Pulumi token": "pul-" + r(HEX, 40),
    "RubyGems key": "rubygems_" + r(HEX, 48),
    "Brevo key": "xkeysib-" + r(HEX, 64) + "-" + r(ALNUM, 16),
    "Sentry token": "sntryu_" + r(HEX, 64),
    "Shopify token": "shpat_" + r(HEX, 32),
    "Square token": "sq0atp-" + r(ALNUM, 22),
    "Stripe production key": "sk_prod_" + r(ALNUM, 24),
    "Twilio key": "SK" + r(HEX, 32),
    "Vault token": "hvs." + r(ALNUM, 95),
    "Google OAuth token": "ya29." + r(ALNUM, 40),
    "Docker Hub token": "dckr_pat_" + r(ALNUM, 27),
    "Figma token": "figd_" + r(ALNUM, 40),
    "Sourcegraph token": "sgp_" + r(HEX, 40),
    "Slack webhook": "https://hooks.slack.com/services/" + r(ALNUM, 44),
    "Teams webhook": ("https://acme.webhook.office.com/webhookb2/" + "-".join(r(HEX, n) for n in (8, 4, 4, 4, 12))
                      + "@" + "-".join(r(HEX, n) for n in (8, 4, 4, 4, 12))),
    "Azure AD client secret": r(ALNUM, 3) + "7Q~" + r(ALNUM, 32),
    "Terraform Cloud token": r(string.ascii_lowercase + string.digits, 14) + ".atlasv1." + r(ALNUM.lower(), 64),
}

LOOKALIKES = (
    "The SKU list and the SK-II skincare line were reviewed.",
    "Use sha256 to hash; the tilde in ~/Documents is your home.",
    "dapi is short for data API in our notes.",
    "A 64-character hex digest: " + "ab" * 32,
    "The model returned 3Q results for Q~3 planning.",
)


class VendorFormatTests(unittest.TestCase):
    def test_every_documented_vendor_format_is_blocked_by_both_screens(self):
        for label, token in SAMPLES.items():
            with self.subTest(label=label):
                text = f"deploy with {token} today"
                self.assertTrue(auto_screen(text, allow_context=True))
                self.assertTrue(typed_screen(text, allow_context=True)[1])

    def test_look_alike_ordinary_text_passes(self):
        for text in LOOKALIKES:
            with self.subTest(text=text[:30]):
                self.assertEqual(auto_screen(text, allow_context=True), [])
                self.assertEqual(typed_screen(text, allow_context=True)[1], [])


if __name__ == "__main__":
    unittest.main()
