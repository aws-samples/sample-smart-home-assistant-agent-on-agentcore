import sys
import pathlib
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bundle_config
from bundle_config import _parse_baggage, load_from_baggage


def setup_function(_):
    # Reset memoised client between tests so patch.object on _client takes effect.
    bundle_config._cached_client = None


def test_parse_baggage_returns_kv_dict():
    assert _parse_baggage("bundle-arn=arn:abc,bundle-version-id=v3") == {
        "bundle-arn": "arn:abc",
        "bundle-version-id": "v3",
    }


def test_parse_baggage_handles_url_encoded_values():
    assert _parse_baggage("bundle-arn=arn%3Aabc")["bundle-arn"] == "arn:abc"


def test_parse_baggage_returns_empty_dict_for_malformed():
    assert _parse_baggage("") == {}
    assert _parse_baggage("not-key-value") == {}


def test_load_from_baggage_returns_none_when_no_header():
    assert load_from_baggage({}, "text") is None


def test_load_from_baggage_returns_none_when_baggage_lacks_bundle_keys():
    assert load_from_baggage({"baggage": "userId=foo"}, "text") is None


def test_load_from_baggage_calls_control_api_and_extracts_text_prompt():
    fake_client = MagicMock()
    fake_client.get_configuration_bundle_version.return_value = {
        "components": [
            {"componentArn": "arn:runtime",
             "configuration": {"system_prompt": "from-bundle"}},
        ],
    }
    headers = {"baggage": "bundle-arn=arn:bundle/foo,bundle-version-id=v1"}
    with patch("bundle_config._client", return_value=fake_client):
        result = load_from_baggage(headers, "text")
    assert result == "from-bundle"
    fake_client.get_configuration_bundle_version.assert_called_once_with(
        bundleId="arn:bundle/foo", versionId="v1",
    )


def test_load_from_baggage_returns_none_on_sdk_error():
    fake_client = MagicMock()
    fake_client.get_configuration_bundle_version.side_effect = Exception("boom")
    headers = {"baggage": "bundle-arn=arn:bundle/foo,bundle-version-id=v1"}
    with patch("bundle_config._client", return_value=fake_client):
        assert load_from_baggage(headers, "text") is None


def test_load_from_baggage_handles_case_insensitive_header_name():
    fake_client = MagicMock()
    fake_client.get_configuration_bundle_version.return_value = {
        "components": [
            {"componentArn": "arn:runtime",
             "configuration": {"system_prompt": "from-bundle"}},
        ],
    }
    headers = {"Baggage": "bundle-arn=arn:bundle/foo,bundle-version-id=v1"}
    with patch("bundle_config._client", return_value=fake_client):
        assert load_from_baggage(headers, "text") == "from-bundle"
