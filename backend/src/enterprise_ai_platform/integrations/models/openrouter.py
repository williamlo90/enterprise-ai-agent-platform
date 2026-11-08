"""Explicit fictional-only free chat lane; no retries, fallback or embeddings."""
import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx

from enterprise_ai_platform.integrations.models.bedrock import strict_json, MAX_RESPONSE_BYTES
from enterprise_ai_platform.integrations.models.nova import native_messages, NOVA_SYSTEM, RAG_SYSTEM
from enterprise_ai_platform.modules.models.domain import (
    MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, TOOL_NAME, TOOL_SCHEMA,
    ModelFailure, Usage, Message, ChatReply, ResponseContract, count, tool_call,
)

FREE_MODEL = 'liquid/lfm-2.5-2.6b:free'
URL = 'https://openrouter.ai/api/v1/chat/completions'


def messages_for_openrouter(messages, tools):
    # Use the existing application's Message/tool-result validation boundary.
    native = native_messages(messages, tools)
    system = RAG_SYSTEM if messages[0].response_contract is ResponseContract.EVIDENCE_SELECTION else NOVA_SYSTEM
    result = [{'role': 'system', 'content': system},
              {'role': 'user', 'content': native[0]['content'][0]['text']}]
    if len(native) == 3:
        call = messages[1].tool
        result.extend([
            {'role': 'assistant', 'content': messages[1].text or None, 'tool_calls': [
                {'id': call.id, 'type': 'function', 'function': {'name': TOOL_NAME,
                 'arguments': json.dumps({'topic': 'travel', 'language': call.language})}}]},
            {'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps({
                'deadline_days': messages[2].result.deadline_days,
                'source': messages[2].result.source, 'policy': messages[2].result.policy})},
        ])
    return result


class OpenRouterFreeChat:
    """One serial, bounded run. Caller owns fictional data and authority observer.

    The default HTTP client never reads proxy env, follows redirects or retries.
    Injected clients are for controlled transport tests and remain caller-owned.
    """
    def __init__(self, client, api_key, before_call, *, max_requests=6, clock=time.monotonic):
        if (not isinstance(api_key, str) or not 20 <= len(api_key) <= 1024
                or re.search(r'[\x00-\x20\x7f]', api_key) or not callable(before_call)
                or type(max_requests) is not int or not 1 <= max_requests <= 10):
            raise ModelFailure('invalid_input')
        self._client, self._key, self._before = client, api_key, before_call
        self._limit, self._clock = max_requests, clock
        self._attempts, self._last_attempt = 0, None
        self._pricing_violation = False
        self._lock = threading.Lock()
        self.last_usage = None
        self.last_cost_usd = None
        self.last_status_code = None
        self.last_failure = None

    def __repr__(self):
        return f'OpenRouterFreeChat(model={FREE_MODEL!r}, attempts={self._attempts})'

    @property
    def attempts(self):
        return self._attempts

    def chat(self, messages, *, tools=False):
        if not self._lock.acquire(blocking=False):
            raise ModelFailure('rate_limited')
        try:
            if self._pricing_violation:
                raise ModelFailure('budget_exhausted')
            self.last_usage = self.last_cost_usd = None
            self.last_status_code = self.last_failure = None
            return self._chat(messages, tools)
        except ModelFailure as error:
            self.last_failure = error.code
            raise
        except httpx.TimeoutException:
            self.last_failure = 'timeout'
            raise ModelFailure('timeout') from None
        except httpx.HTTPError:
            self.last_failure = 'provider_unavailable'
            raise ModelFailure('provider_unavailable') from None
        except Exception:
            self.last_failure = 'invalid_output'
            raise ModelFailure('invalid_output') from None
        finally:
            self._lock.release()

    def _chat(self, messages, tools):
        native = messages_for_openrouter(messages, tools)
        payload = {'model': FREE_MODEL, 'messages': native, 'max_tokens': MAX_OUTPUT_TOKENS,
                   'temperature': 0, 'stream': False,
                   'provider': {'allow_fallbacks': False, 'require_parameters': True,
                                'max_price': {'prompt': 0, 'completion': 0, 'request': 0}}}
        # On the final tool-result turn omit tools/tool_choice: this provider
        # rejected tool_choice=none. Unsolicited output tool calls still fail.
        expecting_tool = tools and len(messages) == 1
        if expecting_tool:
            payload['tools'] = [{'type': 'function', 'function': {
                'name': TOOL_NAME, 'description': 'Read a fictional travel policy. No transactions.',
                'parameters': TOOL_SCHEMA}}]
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 8192:
            raise ModelFailure('invalid_input')
        if self._attempts >= self._limit:
            raise ModelFailure('budget_exhausted')
        if self._last_attempt is not None and self._clock() - self._last_attempt < 3.1:
            raise ModelFailure('rate_limited')
        # Authority/reservation failure prevents egress; failed HTTP calls consume
        # the attempt limit too. No automatic retry, including after 429.
        self._before('chat', MAX_INPUT_TOKENS)
        self._attempts += 1
        self._last_attempt = self._clock()
        deadline = self._last_attempt + 20
        with self._client.stream('POST', URL, content=raw, headers={
                'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'},
                timeout=httpx.Timeout(5, connect=3)) as response:
            self.last_status_code = response.status_code
            if self._clock() >= deadline:
                raise ModelFailure('timeout')
            if response.status_code != 200:
                code = {429: 'rate_limited', 401: 'access_denied', 403: 'access_denied',
                        402: 'budget_exhausted'}.get(response.status_code, 'provider_unavailable')
                raise ModelFailure(code)
            body = bytearray()
            for chunk in response.iter_bytes():
                if self._clock() >= deadline:
                    raise ModelFailure('timeout')
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ModelFailure('invalid_output')
                body.extend(chunk)
        if self._clock() >= deadline:
            raise ModelFailure('timeout')
        value = strict_json(body)
        if not isinstance(value, dict):
            raise ModelFailure('invalid_output')
        usage = value.get('usage', {})
        # Capture independent known accounting fields before model/envelope/output
        # validation. Rejected content does not erase an already incurred cost.
        usage_valid = cost_valid = False
        if not isinstance(usage, dict):
            usage = {}
        try:
            inp = count(usage.get('prompt_tokens'), 1048576)
            out = count(usage.get('completion_tokens'), 131072)
            if count(usage.get('total_tokens'), 1179648) == inp + out:
                self.last_usage = Usage(inp, out)
                usage_valid = True
        except ModelFailure:
            pass
        try:
            if type(usage.get('cost')) not in (int, float, str):
                raise ValueError
            cost = Decimal(str(usage['cost']))
            if not cost.is_finite() or cost < 0:
                raise ValueError
            self.last_cost_usd = str(cost)
            cost_valid = True
            if cost != 0:
                self._pricing_violation = True
        except (InvalidOperation, ValueError):
            pass
        if (not usage_valid or not cost_valid or self._pricing_violation
                or value.get('error') or value.get('model') != FREE_MODEL):
            raise ModelFailure('invalid_output')
        if inp > MAX_INPUT_TOKENS or out > MAX_OUTPUT_TOKENS:
            raise ModelFailure('invalid_output')
        choices = value.get('choices')
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelFailure('invalid_output')
        choice = choices[0]
        message = choice['message']
        if message.get('role') != 'assistant' or message.get('refusal'):
            raise ModelFailure('invalid_output')
        text = message.get('content')
        calls = message.get('tool_calls') or []
        if text is None and calls:
            text = ''
        if not isinstance(text, str) or len(text.encode()) > 4096 or not isinstance(calls, list) or len(calls) > 1:
            raise ModelFailure('invalid_output')
        selected = None
        if calls:
            if not expecting_tool or choice.get('finish_reason') != 'tool_calls':
                raise ModelFailure('invalid_output')
            call = calls[0]
            if call.get('type') != 'function':
                raise ModelFailure('invalid_output')
            selected = tool_call({'toolUseId': call['id'], 'name': call['function']['name'],
                                  'input': strict_json(call['function']['arguments'])})
        elif choice.get('finish_reason') != 'stop' or not text.strip():
            raise ModelFailure('invalid_output')
        return ChatReply(text, selected, self.last_usage, Message('assistant', text=text, tool=selected))
