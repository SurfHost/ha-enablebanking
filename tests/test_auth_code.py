"""Tests for pulling the authorisation code out of what the user pastes.

The bank redirect lands the PSU on a URL carrying both `state` and `code`.
Selecting exactly the code out of an address bar — no `code=` prefix, no
neighbouring `state`, no trailing whitespace — is easy to get wrong, and a
wrong selection only reports itself one request later as a rejected code. So
the field takes either form and this is where that is decided.

These are pure-function tests: no Home Assistant, no network.
"""

from __future__ import annotations

import pytest

from custom_components.enablebanking.config_flow import _extract_auth_code

# A syntactically realistic but entirely made-up code.
CODE = "1d2cc191-0000-0000-0000-5d059fa52b3e"
STATE = "0jOBrho7NChFopETIUsHFg"


@pytest.mark.parametrize(
    "pasted",
    [
        f"https://enablebanking.com/?state={STATE}&code={CODE}",
        f"https://enablebanking.com/?code={CODE}&state={STATE}",
        f"https://enablebanking.com/?code={CODE}",
        f"enablebanking.com/?code={CODE}",
        f"https://enablebanking.com/#code={CODE}&state={STATE}",
        f"https://example.org/callback?code={CODE}",
    ],
    ids=[
        "state-then-code",
        "code-then-state",
        "code-only-query",
        "no-scheme",
        "fragment",
        "custom-redirect-host",
    ],
)
def test_extracts_the_code_from_a_redirect_url(pasted: str) -> None:
    """The whole address is accepted, however the parameters are arranged."""
    assert _extract_auth_code(pasted) == CODE


def test_bare_code_still_accepted() -> None:
    """Backwards compatible with the instructions people already followed."""
    assert _extract_auth_code(CODE) == CODE


@pytest.mark.parametrize(
    "pasted",
    [f"  {CODE}  ", f"\n{CODE}\n", f"  https://enablebanking.com/?code={CODE}  "],
    ids=["padded-code", "newlines", "padded-url"],
)
def test_surrounding_whitespace_is_ignored(pasted: str) -> None:
    """Copying from a browser routinely picks up a trailing space or newline."""
    assert _extract_auth_code(pasted) == CODE


@pytest.mark.parametrize(
    "pasted",
    [
        "https://enablebanking.com/?state=abc&error=access_denied",
        "https://enablebanking.com/",
        "https://enablebanking.com/?code=",
        "",
        "   ",
    ],
    ids=["denied", "no-query", "empty-code", "empty", "whitespace"],
)
def test_missing_code_returns_none(pasted: str) -> None:
    """None is what makes the form say so, rather than posting a URL as a code.

    The declined-consent case matters most: the bank redirects back with
    `error=access_denied` and no code at all, and "that address contains no
    code" is a far better message than the API's rejection of a URL-shaped
    code.
    """
    assert _extract_auth_code(pasted) is None
