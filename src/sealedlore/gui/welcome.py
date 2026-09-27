"""The first-run welcome: what to set up before a story can run.

Shown where the messages would be while no story is open and the endpoint,
its key or the story model is missing (a first run, or a config that broke
and was set aside). Before this the start screen checked only that an
endpoint existed, and offered New story to someone with no key or model.
"""

from __future__ import annotations

from html import escape

from sealedlore.gui import theme
from sealedlore.models.config import Config
from sealedlore.providers.http import is_loopback

NANO_GPT_URL = "https://nano-gpt.com/"
# The developer's referral link, offered second and said to be one.
REFERRAL_URL = "https://nano-gpt.com/r/mackztyf"


def setup_missing(config: Config) -> list[str]:
    """What the chat endpoint still needs, in words; empty when it can run.
    A local server needs no key."""
    provider = config.active_provider()
    if provider is None:
        return ["an endpoint", "an API key", "a story model"]
    missing = []
    base_url = provider.base_url.strip()
    if not base_url:
        missing.append("an endpoint")
    if not provider.api_key.strip() and not (base_url and is_loopback(base_url)):
        missing.append("an API key")
    if not provider.model.strip():
        missing.append("a story model")
    return missing


def _listed(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def welcome_html(missing: list[str], *, partly_set: bool) -> str:
    """The welcome as rich text, links in the theme's accent colour (a
    label's own link colour is the palette's blue, unreadable in High
    contrast)."""
    link = theme.colour("accent").name()

    def a(url: str, text: str) -> str:
        return f'<a href="{url}" style="color: {link};">{escape(text)}</a>'

    still = f"<p>Still missing: {escape(_listed(missing))}.</p>" if partly_set else ""
    return (
        "<h2>Welcome to SealedLore</h2>"
        "<p>Before you can start playing, you need to configure your AI model "
        "settings.</p>"
        f"{still}"
        "<p>We recommend <b>NanoGPT</b>: one account gives you every model "
        "SealedLore uses, including the end-to-end encrypted private models for "
        "private scenes and chats. If you don't have an account, you can make one "
        f"at {a(NANO_GPT_URL, 'nano-gpt.com')}. Or sign up through the developer's "
        f"{a(REFERRAL_URL, 'referral link')}. It applies to what you use directly "
        "on the NanoGPT website (image and video generation, text chat and so "
        "on): you get a 5% discount there, and the developer gets credit worth "
        "10% of it, which helps support SealedLore. It doesn't discount what "
        "SealedLore itself uses through your API key.</p>"
        "<ol>"
        "<li>Open <b>Settings</b> (the button below, or File → Settings).</li>"
        "<li>On the <b>Endpoint</b> tab the base URL is already NanoGPT's. Paste "
        "the API key from your NanoGPT account into <b>API key</b>.</li>"
        "<li>On the <b>Models</b> tab the <b>Story model</b> is already Claude "
        "Sonnet 4.6, which with Opus 5.5 writes best; GLM 5.3 is a good cheaper "
        "one. <b>Browse…</b> beside it chooses another. Leave the other models "
        "blank: they use the story model.</li>"
        "<li><b>Save</b>, then start a story.</li>"
        "</ol>"
        "<p>Any other OpenAI-compatible endpoint (OpenRouter, a local server) "
        "works too: put its base URL on the Endpoint tab instead.</p>"
    )
