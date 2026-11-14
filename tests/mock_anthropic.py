"""A stand-in for anthropic.Anthropic used by every extraction test (no network, no spend)."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Callable

_ID = re.compile(r'<transmission id="([^"]+)"')


def default_item(tid: str, transcript: str) -> dict:
    return {"id": tid, "has_location": False,
            "location": {"kind": "none", "street1": None, "street2": None, "number": None, "city": None, "raw": None},
            "incident_type": "NONE", "priority": "LOW", "units": [], "is_rail": False, "rail": None,
            "short_summary": "radio traffic"}


def transcripts_by_id(user: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'<transmission id="([^"]+)"[^>]*>\n(.*?)\n</transmission>', user, re.S):
        out[m.group(1)] = m.group(2)
    return out


class MockMessages:
    def __init__(self, owner: "MockAnthropic"):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.raise_exc is not None:
            exc, self.owner.raise_exc = self.owner.raise_exc, None
            raise exc
        user = kwargs["messages"][0]["content"]
        tx = transcripts_by_id(user)
        ids = _ID.findall(user)
        stop = self.owner.stop_reason_for(len(ids))
        items = [self.owner.responder(i, tx.get(i, "")) for i in ids]
        items = [x for x in items if x is not None]
        if self.owner.drop_ids:
            items = [x for x in items if x["id"] not in self.owner.drop_ids]
        text = json.dumps({"results": items})
        if stop == "max_tokens":
            text = text[: len(text) // 2]
        content = [] if stop == "refusal" else [SimpleNamespace(type="text", text=text)]
        n_in = len(user) // 3
        first = len(self.owner.calls) == 1
        sysp = kwargs["system"]
        sys_tokens = len(sysp if isinstance(sysp, str) else sysp[0]["text"]) // 3
        usage = SimpleNamespace(
            input_tokens=n_in, output_tokens=len(text) // 3,
            cache_creation_input_tokens=sys_tokens if first else 0,
            cache_read_input_tokens=0 if first else sys_tokens,
            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=0,
                                           ephemeral_1h_input_tokens=sys_tokens if first else 0))
        sd = SimpleNamespace(category="cyber", explanation="mock") if stop == "refusal" else None
        return SimpleNamespace(stop_reason=stop, stop_details=sd, content=content, usage=usage,
                               model=kwargs["model"], _request_id=f"req_mock_{len(self.owner.calls)}")


class MockAnthropic:
    def __init__(self, responder: Callable[[str, str], dict | None] | None = None):
        self.responder = responder or default_item
        self.calls: list[dict] = []
        self.messages = MockMessages(self)
        self.raise_exc: Exception | None = None
        self.drop_ids: set[str] = set()
        self.force_stop: str | None = None
        self.max_items_before_truncation: int | None = None

    def stop_reason_for(self, n_items: int) -> str:
        if self.force_stop:
            return self.force_stop
        if self.max_items_before_truncation is not None and n_items > self.max_items_before_truncation:
            return "max_tokens"
        return "end_turn"
