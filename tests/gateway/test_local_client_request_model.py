"""Schema tests for :class:`LocalClientRequest.from_json`."""

import pytest

from gateway.local_client_bridge import (
    LocalClientRequest,
    LocalClientValidationError,
    VALID_ACTIONS,
)


def _good_body(**overrides):
    body = {
        "client": {
            "kind": "browser-sidecar",
            "label": "Chrome Extension",
            "client_session_id": "panel-123",
        },
        "action": "state",
    }
    body.update(overrides)
    return body


class TestLocalClientRequestFromJson:
    def test_accepts_minimal_valid_body(self):
        req = LocalClientRequest.from_json(_good_body())
        assert req.action == "state"
        assert req.client.kind == "browser-sidecar"
        assert req.client.label == "Chrome Extension"
        assert req.client.client_session_id == "panel-123"
        assert req.message is None
        assert req.context is None

    @pytest.mark.parametrize("action", sorted(VALID_ACTIONS))
    def test_accepts_each_valid_action(self, action):
        req = LocalClientRequest.from_json(_good_body(action=action))
        assert req.action == action

    def test_rejects_non_dict_body(self):
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json("not a dict")

    def test_rejects_missing_client(self):
        body = {"action": "state"}
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(body)

    def test_rejects_non_dict_client(self):
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json({"client": "nope", "action": "state"})

    @pytest.mark.parametrize("missing", ["kind", "label", "client_session_id"])
    def test_rejects_missing_client_field(self, missing):
        body = _good_body()
        del body["client"][missing]
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(body)

    @pytest.mark.parametrize("missing", ["kind", "label", "client_session_id"])
    def test_rejects_empty_client_field(self, missing):
        body = _good_body()
        body["client"][missing] = "   "
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(body)

    def test_rejects_unknown_action(self):
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(_good_body(action="run"))

    def test_rejects_missing_action(self):
        body = _good_body()
        del body["action"]
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(body)

    def test_rejects_non_string_message(self):
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(_good_body(action="send", message=123))

    def test_accepts_arbitrary_context_dict(self):
        ctx = {
            "type": "reference_material",
            "url": "https://example.com",
            "metadata": {"anything": "goes", "nested": {"ok": True}},
        }
        req = LocalClientRequest.from_json(_good_body(context=ctx))
        assert req.context == ctx

    def test_rejects_non_dict_context(self):
        with pytest.raises(LocalClientValidationError):
            LocalClientRequest.from_json(_good_body(context=["list", "not", "dict"]))

    def test_accepts_send_without_message_at_schema_level(self):
        # Service layer surfaces the missing-message error; schema just
        # validates structural shape.
        req = LocalClientRequest.from_json(_good_body(action="send"))
        assert req.action == "send"
        assert req.message is None
