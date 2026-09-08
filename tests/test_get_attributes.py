from unittest.mock import MagicMock, patch

import pytest

from custom_components.hue_dimmer import _handle_get_attributes
from tests.conftest import make_service_call

RESOURCE_ID = "abc-123"
ENTITY_ID = "light.kitchen"


def make_light_model(brightness=50.0, mirek=370, xy=(0.4, 0.4)):
    model = MagicMock()
    model.dimming.brightness = brightness
    model.color_temperature.mirek = mirek
    model.color.xy.x, model.color.xy.y = xy
    return model


@pytest.fixture(autouse=True)
def patch_extract_entity_ids():
    async def _extract(call):
        return set(call.data.get("entity_id", []))

    with patch("custom_components.hue_dimmer.async_extract_entity_ids", side_effect=_extract):
        yield


@pytest.mark.asyncio
async def test_response_uses_xy_color_key(mock_hass):
    # Response field names must match set_attributes inputs and HA attribute names (issue #16).
    bridge = MagicMock()
    bridge.api.lights.get.return_value = make_light_model()
    mock_hass.states.get.return_value = None
    call = make_service_call({"entity_id": [ENTITY_ID]})

    with patch("custom_components.hue_dimmer.resolve_entity", return_value=(bridge, "light", RESOURCE_ID)):
        result = await _handle_get_attributes(mock_hass, call)

    attrs = result[ENTITY_ID]
    assert attrs["xy_color"] == [0.4, 0.4]
    assert "color_xy" not in attrs
    assert set(attrs) == {"brightness", "color_temp_kelvin", "xy_color", "rgb_color", "hs_color"}
