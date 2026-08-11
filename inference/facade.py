"""One way in, whichever provider is behind it.

``Inference`` is what a manager, an advisor, or a worker holds. It resolves the
configured brain order, runs a turn against the best provider, and falls
through to the next only when the one above failed before producing a usable
answer. Bad tool JSON from a *successful* provider is not a provider failure —
that goes back to the same manager through the normal loop.
"""
from __future__ import annotations

from interface.progress import (
    provider_not_authenticated_message,
    redact_diagnostic_text,
)
from inference import config
from inference.base import InferenceProvider
from inference.errors import TIMEOUT_MSG, ProviderTimeoutError, error_tools
from inference.models import TurnRequest, TurnResult
from inference.parsing import parse_manager_output
from inference.registry import get_provider


class Inference:
    """The configured brains, in the order they should be tried."""

    def __init__(self, order: list[str] | None = None) -> None:
        self._order = list(order) if order else None

    @property
    def order(self) -> list[str]:
        """Provider names, best first. Re-read so config edits take effect."""
        return self._order if self._order is not None else config.brain_order()

    @property
    def primary(self) -> InferenceProvider:
        return get_provider(self.order[0])

    def provider(self, name: str) -> InferenceProvider:
        return get_provider(name)

    def providers(self):
        for name in self.order:
            yield get_provider(name)

    # -- sessions ---------------------------------------------------------

    def new_session(self, session_key: str, provider: str | None = None) -> str:
        """Reset a conversation on one provider, or on the configured brain."""
        return get_provider(provider or config.brain()).new_session(session_key)

    # -- turns ------------------------------------------------------------

    def run_turn(self, request: TurnRequest, *, trace=None) -> TurnResult:
        """Run a manager turn, falling through the configured order.

        A provider-level failure — empty output, a timeout, a rate limit, or a
        structured Manager error — moves to the next provider. Anything else is
        this manager's answer.
        """
        from inference.telemetry import provider_span

        last: TurnResult | None = None
        failures = []
        for name in self.order:
            provider = get_provider(name)
            try:
                with provider_span(trace, name) as diag_span:
                    request.diag_span = diag_span
                    result = provider.run_turn(request)
            except ProviderTimeoutError:
                result = TurnResult(TIMEOUT_MSG)
            except Exception as exc:
                result = TurnResult(error_tools(exc))

            last = result
            if not provider_failed(result.output, result.rate_limit):
                return result
            detail = (
                redact_diagnostic_text(result.output or result.rate_limit, limit=200)
                or "provider failed"
            )
            failures.append(f"{name}: {detail}")

        if last is not None:
            if failures:
                print(
                    f"  [{request.resolved_tag()}] all configured brains failed: "
                    f"{' | '.join(failures)}",
                    flush=True,
                )
            return last
        return TurnResult('{"tools": [{"tool": "do_nothing"}]}')

    def run_agent(self, request: TurnRequest) -> str:
        """Run a non-manager agent and return its final text.

        The advisor is the caller: same providers, same order, its own session
        and instructions. Returns an empty string if every provider failed.
        """
        failures = []
        for name in self.order:
            provider = get_provider(name)
            try:
                result = provider.run_turn(request)
            except ProviderTimeoutError:
                failures.append(f"{name}: timed out")
                continue
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}")
                continue
            if result.output and result.output.strip() and not result.rate_limit:
                return result.output.strip()
            failures.append(
                f"{name}: {'rate limited' if result.rate_limit else 'no output'}"
            )
        if failures:
            print(
                f"  [{request.resolved_tag()}] no provider produced a result: "
                f"{' | '.join(failures)}",
                flush=True,
            )
        return ""


def _not_authenticated_messages() -> set[str]:
    from inference.registry import provider_names

    return {provider_not_authenticated_message(name) for name in provider_names()}


def provider_failed(output, rate_limit) -> bool:
    """Whether a turn failed at the provider level and should fall through."""
    text = (output or "").strip()
    if not text:
        return True
    if text == TIMEOUT_MSG:
        return True
    # A complete tool invocation is usable even when its ordinary Markdown
    # happens to contain a phrase such as "rate limit". The one exception is
    # our own structured provider-error reply, which must still fall through.
    parsed = parse_manager_output(text, debug=False)
    if parsed:
        for tool in parsed.get("tools", []):
            if not isinstance(tool, dict):
                continue
            message = str(tool.get("message") or "")
            if tool.get("tool") == "reply" and (
                "Manager error:" in message
                or message in _not_authenticated_messages()
            ):
                return True
        return False
    if rate_limit:
        return True
    if "Manager error:" in text:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in (
        "failed to authenticate",
        "authentication failed",
        "oauth session expired",
        "login required",
        "not logged in",
    ))
