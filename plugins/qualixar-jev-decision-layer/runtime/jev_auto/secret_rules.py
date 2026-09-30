"""Credential formats that are never sent to a hosted provider.

One rule set serves every path that screens outgoing text. It is a best-effort
screen for common credential formats, not a data-loss-prevention product.
Callers get labels only; matched text is never returned or logged.

Text is checked as written, after Unicode normalisation with invisible
characters removed (so a zero-width space or a full-width lookalike cannot
split a key), and once more URL-decoded. Every repeated part of every pattern
is bounded, so a long line of repetitive text is screened in linear time.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from urllib.parse import unquote

_INVISIBLE = re.compile('[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]')

# (label, pattern) pairs whose match alone is a finding. Each starts at a fixed
# prefix, so no start position scans more than a bounded run.
FORMATS = [
    ('PRIVATE_KEY', re.compile(r'-----BEGIN (?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d')),
    ('API_KEY', re.compile(
        r'\b(?:sk-(?:proj-|ant-|or-|svcacct-)?[A-Za-z0-9_-]{16,}'
        r'|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{22,}'
        r'|gl(?:pat|dt|ptt|rt|cbt)-[A-Za-z0-9_-]{20,}'
        r'|(?:AKIA|ASIA)[A-Z0-9]{16}'
        r'|xox[abposr]-[A-Za-z0-9-]{10,}|xapp-\d-[A-Za-z0-9-]{10,}'
        r'|AIza[0-9A-Za-z_-]{35}'
        r'|(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}|whsec_[A-Za-z0-9]{24,}'
        r'|npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_-]{50,}|hf_[A-Za-z0-9]{30,}'
        r'|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})')),
    # Documented vendor token prefixes, each with the shape that follows it.
    ('API_KEY', re.compile(
        r'\b(?:(?:A3T[A-Z0-9]|ABIA|ACCA)[A-Z2-7]{16}|ABSK[A-Za-z0-9+/]{100,300}'
        r'|A3-[A-Z0-9]{6}-[A-Z0-9-]{20,40}|ops_eyJ[A-Za-z0-9+/]{100,}|AGE-SECRET-KEY-1[0-9A-Z]{58}'
        r'|p8e-[A-Za-z0-9]{32}|LTAI[A-Za-z0-9]{20}|AKCp[A-Za-z0-9]{60,80}|dapi[a-f0-9]{32}'
        r'|do[opr]_v1_[a-f0-9]{64}|dp\.pt\.[A-Za-z0-9]{43}|duffel_(?:test|live)_[A-Za-z0-9_=-]{43}'
        r'|dt0c01\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{64}|EZ[AT]K[A-Za-z0-9]{54}|EAA[MC][A-Za-z0-9]{100,}'
        r'|FLW(?:SECK|PUBK)_TEST-[a-h0-9]{12,32}|fo1_[A-Za-z0-9_-]{43}|fio-u-[A-Za-z0-9_=-]{64}'
        r'|gl(?:ffct|ft|imt|agent|oas|soat)-[A-Za-z0-9_-]{20,}|GR1348941[A-Za-z0-9_-]{20}'
        r'|glc_[A-Za-z0-9+/]{32,400}|glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}|eyJrIjoi[A-Za-z0-9]{70,400}'
        r'|HRKU-AA[0-9A-Za-z_-]{58}|api_org_[A-Za-z]{34}|lin_api_[A-Za-z0-9]{40}|ntn_[0-9]{11}[A-Za-z0-9]{35}'
        r'|sha256~[A-Za-z0-9_-]{43}|pplx-[A-Za-z0-9]{48}|pscale_(?:tkn|oauth|pw)_[A-Za-z0-9_=.-]{32,64}'
        r'|PMAK-[a-f0-9]{24}-[a-f0-9]{34}|pnu_[A-Za-z0-9]{36}|pul-[a-f0-9]{40}|rdme_[a-z0-9]{70}'
        r'|rubygems_[a-f0-9]{48}|xkeysib-[a-f0-9]{64}-[A-Za-z0-9]{16}|sntry[su]_[A-Za-z0-9+/_]{40,400}'
        r'|shippo_(?:live|test)_[a-fA-F0-9]{40}|shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}'
        r'|xoxe(?:\.xox[bp])?-\d-[A-Za-z0-9]{100,}|(?:EAAA|sq0atp-)[A-Za-z0-9_-]{22,60}'
        r'|(?:sk|rk)_prod_[A-Za-z0-9]{10,99}|SK[0-9a-fA-F]{32}|hv[sb]\.[A-Za-z0-9_-]{90,300}'
        r'|ya29\.[A-Za-z0-9_-]{20,}|dckr_pat_[A-Za-z0-9_-]{27,}|figd_[A-Za-z0-9_-]{40,}|sgp_[a-fA-F0-9]{40}'
        r'|tk-us-[A-Za-z0-9_-]{48}|CLOJARS_[a-z0-9]{60}|ico-[A-Za-z0-9]{32}'
        r'|(?:pat|sat)\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{20}'
        r'|[a-z0-9]{14}\.atlasv1\.[a-z0-9_=-]{60,70})\b'
        r'|hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{40,60}'
        r'|\.webhook\.office\.com/webhookb2/[a-z0-9-]{20,}'
        r'|(?<![A-Za-z0-9_~.])[A-Za-z0-9_~.]{3}\dQ~[A-Za-z0-9_~.-]{31,34}(?![A-Za-z0-9_~.-])')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}')),
    ('CREDENTIAL_ASSIGNMENT', re.compile(
        r'(?i)\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)[ \t]*=[ \t]*[^;\s"\']{16,}'
        r'|[?&]sig=[A-Za-z0-9%+/=]{20,}')),
]

# A named credential being given a value: `password = ...`, `"api_key": "..."`.
_NAMES = (r'pass(?:word|wd|phrase)|pwd|secret|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key'
          r'|client[_-]?secret|account[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token'
          r'|session[_-]?token|bearer[_-]?token')
_KEY = r'(?<![\w.-])(?P<key>[\w.-]{0,64}?(?:' + _NAMES + r'))'
_QUOTED = re.compile(r'(?i)(?P<kq>["\']?)' + _KEY + r'(?P=kq)[ \t]*(?:=>|:=|:|=)[ \t]*'
                     r'(?P<vq>["\'])(?P<value>[^"\'\r\n]{1,512})(?P=vq)')
_UNQUOTED = re.compile(r'(?i)' + _KEY + r'(?P<pre>[ \t]*)'
                       r'(?P<sep>=>|:=|==|!=|:|=)(?P<post>[ \t]*)(?P<value>[^\s"\'`,;#]{6,512})')
# A YAML, INI or compose line whose whole value is one word: `password: meadowlark`.
_CONFIG_LINE = re.compile(r'(?im)^[ \t]*(?:-[ \t]+|export[ \t]+)?["\']?(?P<key>[\w.-]{0,64}?'
                          r'(?:pass(?:word|wd|phrase)|pwd|secret|client[_-]?secret))["\']?[ \t]*[:=][ \t]*'
                          r'(?P<value>[A-Za-z]{6,64})[ \t]*(?:#[^\n]*)?$')
_AUTH_HEADER = re.compile(r'(?i)\b(?:authorization|proxy-authorization)["\']?[ \t]*[:=][ \t]*["\']?'
                          r'(?:basic|bearer|token|digest|negotiate|ntlm)[ \t]+([A-Za-z0-9._~+/-]{8,4096}=*)')
_BEARER = re.compile(r'(?i)\bbearer[ \t]+([A-Za-z0-9._~+/-]{8,4096}=*)')
_CREDENTIAL_URL = re.compile(r'(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://(?P<user>[^/\s:@]{0,128}):'
                             r'(?P<secret>[^/\s@]{3,256})@[^\s/]')
_CURL_USER = re.compile(r'(?:^|\s)(?:-u|--user)[ \t]+(?P<user>[^\s:]{1,128}):(?P<secret>[^\s]{4,256})')
_DOCKER_AUTH = re.compile(r'(?i)["\']auth["\'][ \t]*:[ \t]*["\'](?P<value>[A-Za-z0-9+/]{8,4096}={0,2})["\']')

_PLACEHOLDER = re.compile(
    r'(?i)^(?:\$\{?[\w.:-]*\}?|<[^>]*>|\{\{.*\}\}|\{[\w.]*\}|%\(?\w+\)?s?%?|\*+|x{3,}|\.{3,}|…'
    r'|\[redacted[^\]]*\]'
    r'|(?:your|example|sample|dummy|fake|placeholder|change[_-]?me|redacted)(?:[_-][\w-]*)?'
    r'|test|secret|password|passwd|pass|pwd|none|null|nil|undefined|true|false|todo|tbd|string|value)$')
# Whole-line words that describe a field rather than fill it.
_SCHEMA_WORDS = frozenset({'required', 'optional', 'string', 'boolean', 'integer', 'number', 'hidden', 'masked',
                           'redacted', 'encrypted', 'sensitive', 'disabled', 'enabled', 'default', 'generated',
                           'mandatory', 'protected', 'password', 'passwords', 'secret', 'secrets', 'changeme',
                           'example', 'placeholder', 'undefined', 'text', 'secure', 'readonly',
                           # where a value comes from, not the value itself
                           'prompt', 'prompted', 'interactive', 'keychain', 'keyring', 'vault', 'environment',
                           'stdin', 'inherit', 'inherited', 'unset', 'empty'})
_IDENTIFIER = re.compile(r'^[A-Za-z_]\w*$')
_DOTTED = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$')
_PATHLIKE = re.compile(r'^(?:~?/|\.\.?/|[A-Za-z]:[\\/])')


def variants(text: str) -> tuple[str, ...]:
    """The forms of `text` every rule is checked against."""
    normalised = _INVISIBLE.sub('', unicodedata.normalize('NFKC', text))
    forms = [text] if normalised == text else [text, normalised]
    if '%' in normalised:
        decoded = unquote(normalised)
        if decoded != normalised:
            forms.append(decoded)
    return tuple(forms)


def _looks_secret(value: str) -> bool:
    """A value with a digit, a symbol, inner capitals, or real length. Not a word."""
    return (any(c.isdigit() for c in value) or any(not c.isalnum() and c not in '._' for c in value)
            or any(c.isupper() for c in value[1:]) or len(value) >= 12)


def _placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.match(value)) or bool(_PATHLIKE.match(value))


def _quoted_is_secret(match) -> bool:
    value = match['value']
    if any(c.isspace() for c in value) or len(value) < 4 or _placeholder(value):
        return False
    # A quoted key is JSON or a translation file, where `"password": "Passwort"`
    # is a label. An unquoted key is code, TOML or YAML giving a real value.
    return _looks_secret(value) if match['kq'] else True


def _unquoted_is_secret(match) -> bool:
    value, sep = match['value'], match['sep']
    if sep in ('==', '!=') or _placeholder(value):
        return False
    if any(c in value for c in '([{'):
        return False  # a call, index or literal: code, not a stored value
    env_style = sep == '=' and not match['pre'] and not match['post'] and match['key'].upper() == match['key']
    if env_style:
        return True
    if _DOTTED.match(value):
        return False
    if _IDENTIFIER.match(value):
        return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return True


# Words that follow "Bearer" or "Basic" in ordinary prose about auth schemes.
_SCHEME_PROSE = frozenset({'authentication', 'authorization', 'credentials', 'credential', 'scheme',
                           'schemes', 'header', 'headers', 'tokens'})


def _decodes_to_user_and_password(value: str) -> bool:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    user, colon, secret = decoded.partition(b':')
    return bool(colon and user and len(secret) >= 3 and decoded.isascii() and decoded.decode().isprintable())


def _assignments(text: str) -> set[str]:
    found = set()
    if any(_quoted_is_secret(match) for match in _QUOTED.finditer(text)) \
            or any(_unquoted_is_secret(match) for match in _UNQUOTED.finditer(text)) \
            or any(match['value'].lower() not in _SCHEMA_WORDS for match in _CONFIG_LINE.finditer(text)):
        found.add('CREDENTIAL_ASSIGNMENT')
    for regex in (_AUTH_HEADER, _BEARER):
        if any(match.group(1).lower() not in _SCHEME_PROSE for match in regex.finditer(text)):
            found.add('BEARER_TOKEN')
    if any(not _placeholder(match['secret']) for match in _CURL_USER.finditer(text)):
        found.add('BEARER_TOKEN')
    if any(not _placeholder(match['secret']) for match in _CREDENTIAL_URL.finditer(text)):
        found.add('CREDENTIAL_URL')
    if any(_decodes_to_user_and_password(match['value']) for match in _DOCKER_AUTH.finditer(text)):
        found.add('CREDENTIAL_ASSIGNMENT')
    return found


def find(text: str) -> set[str]:
    """Labels of the credential formats present in `text`."""
    found = set()
    for form in variants(text):
        found.update(label for label, regex in FORMATS if regex.search(form))
        found.update(_assignments(form))
    return found


_STRICT_KEY = re.compile(
    r'(?i)^[\w-]{0,64}?(?:api[_-]?key|apikey|password|passwd|passphrase|secret|access[_-]?key|secret[_-]?key'
    r'|private[_-]?key|account[_-]?key|access[_-]?token|authorization|credentials?)$')
_TOKEN_KEY = re.compile(
    r'(?i)^[\w-]{0,64}?(?:token|auth[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|pwd|cookie)$')


def credential_field(key: str, value) -> bool:
    """True when a structured field is a named credential carrying a value.

    Callers skip this for the option labels of a choice question: a triage
    label named `password` is a category, not a credential.
    """
    if not value:
        return False
    if _STRICT_KEY.match(key):
        return True
    if key.lower() == 'auth' and isinstance(value, str):
        return _decodes_to_user_and_password(value.strip())
    if _TOKEN_KEY.match(key) and not key.lower().startswith(('max', 'num', 'total', 'count')):
        return (isinstance(value, str) and len(value.strip()) >= 8 and not _placeholder(value.strip()))
    return False
