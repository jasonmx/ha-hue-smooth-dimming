import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_entity_ids
from homeassistant.util.color import color_hs_to_xy, color_RGB_to_xy, color_xy_to_hs, color_xy_to_RGB

from .const import (
    API_SETTLE_SECONDS,
    DEFAULT_MAX_BRIGHTNESS,
    DEFAULT_MIN_BRIGHTNESS,
    DEFAULT_SWEEP_TIME,
    DIR_DOWN,
    DIR_NONE,
    DIR_UP,
    DOMAIN,
    SERVICE_GET_ATTRIBUTES,
    SERVICE_LOWER,
    SERVICE_RAISE,
    SERVICE_SET_ATTRIBUTES,
    SERVICE_STOP,
)

_LOGGER = logging.getLogger(__name__)

# { entity_id: { "time": float, "bright": float, "target": float, "dir": str, "sweep": float } }
_brightness_cache = {}


def resolve_entity(hass: HomeAssistant, entity_id: str):
    # Retrieves the Hue Bridge instance and Resource UUID, ensuring it supports V2 API.
    from homeassistant.components.hue.const import DOMAIN as HUE_DOMAIN

    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)

    if not entry:
        _LOGGER.error("Entity %s not found in entity registry.", entity_id)
        return None, None, None

    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    if not config_entry or config_entry.domain != HUE_DOMAIN:
        _LOGGER.error("Entity %s is not a Philips Hue entity.", entity_id)
        return None, None, None

    bridge = getattr(config_entry, "runtime_data", None)

    if not bridge:
        _LOGGER.error("Hue bridge runtime_data not found for %s", entity_id)
        return None, None, None

    # V2 Bridge Check
    if getattr(bridge, "api_version", 1) < 2:
        _LOGGER.error("Hue Smooth Dimmer requires a Bridge V2 or Bridge Pro for %s", entity_id)
        return None, None, None

    resource_id = entry.unique_id

    if ":" in resource_id:
        resource_id = resource_id.split(":")[-1]

    state = hass.states.get(entity_id)
    is_group = bool(state and state.attributes.get("is_hue_group"))
    resource_type = "grouped_light" if is_group else "light"

    return bridge, resource_type, resource_id


def _get_controller(bridge, resource_type):
    if resource_type == "grouped_light":
        return bridge.api.groups.grouped_light
    return bridge.api.lights


def _get_ha_brightness(hass: HomeAssistant, entity_id: str):
    # Read brightness from HA entity state (0-255) and convert to Hue percentage (0-100).
    state = hass.states.get(entity_id)
    if not state:
        return 0.0
    ha_bright = state.attributes.get("brightness")
    return (ha_bright / 255 * 100) if ha_bright is not None else 0.0


def resolve_brightness(hass: HomeAssistant, entity_id: str):
    # During a dimming transition, the Hue API (and therefore HA's entity state) reports
    # brightness as though the transition happened instantaneously. If a transition stops
    # mid-flight, it takes ~10s to correct its reporting.
    #
    # The resolver decides whether to trust the reported brightness or predict its own, to
    # ensure dim-stop-dim sequences work smoothly. Expired cache entries are pruned inline.

    reported = _get_ha_brightness(hass, entity_id)
    cached = _brightness_cache.get(entity_id)
    if not cached:
        return reported

    now = time.time()
    elapsed = now - cached["time"]

    # Dynamic guard window: sweep duration + API settle buffer for active transitions,
    # just the settle buffer for stopped entries.
    guard_seconds = cached["sweep"] + API_SETTLE_SECONDS if cached["dir"] != DIR_NONE else API_SETTLE_SECONDS

    # Guard expired — trust the reported brightness and prune the cache entry
    if elapsed > guard_seconds:
        _brightness_cache.pop(entity_id, None)
        return reported

    # Guard active — predict brightness instead of trusting the report.

    # Stopped: return the cached brightness from when we stopped
    if cached["dir"] == DIR_NONE:
        _LOGGER.debug(
            "CACHE [%s]: Guard active (Stationary). Ignoring reported %.1f%%. Staying at %.1f%%",
            entity_id,
            reported,
            cached["bright"],
        )
        return cached["bright"]

    # Moving: extrapolate brightness based on elapsed time
    safe_sweep = max(cached["sweep"], 0.1)
    change = (100.0 / safe_sweep) * elapsed

    if cached["dir"] == DIR_UP:
        predicted = min(cached["bright"] + change, cached["target"])
    else:
        predicted = max(cached["bright"] - change, cached["target"])

    _LOGGER.debug(
        "CACHE [%s]: Guard active (Moving). Ignoring reported: %.1f%%, Predicted: %.1f%%",
        entity_id,
        reported,
        predicted,
    )

    return predicted


async def _start_transition(hass, bridge, resource_type, resource_id, entity_id, direction, sweep, limit):
    current_bright = resolve_brightness(hass, entity_id)
    distance = abs(limit - current_bright)
    dur_ms = int(distance * sweep * 10)  # 1000ms / 100% = 10ms/%

    _LOGGER.debug("CALC [%s]: %.1f%% -> %.1f%% | Dur: %dms", entity_id, current_bright, limit, dur_ms)

    if distance < 0.4:  # Min brightness step is 0.4% (1/254)
        return

    _brightness_cache[entity_id] = {
        "time": time.time(),
        "bright": current_bright,
        "target": limit,
        "dir": direction,
        "sweep": sweep,
    }

    controller = _get_controller(bridge, resource_type)
    on = True if direction == DIR_UP else (False if direction == DIR_DOWN and limit == 0.0 else None)

    try:
        await controller.set_state(resource_id, on=on, brightness=limit, transition_time=dur_ms)
    except Exception as exc:
        _LOGGER.debug("Transition command ignored for %s: %s", resource_id, exc)


async def _handle_transition(hass: HomeAssistant, call: ServiceCall, direction: str, default_limit: float):
    sweep = float(call.data.get("sweep_time", DEFAULT_SWEEP_TIME))
    sweep = max(sweep, 0.1)  # Restricts user-supplied value to +ve numbers
    limit = float(call.data.get("limit", default_limit))

    entity_ids = await async_extract_entity_ids(call)
    for entity_id in entity_ids:
        if not entity_id.startswith("light."):
            continue
        bridge, resource_type, resource_id = resolve_entity(hass, entity_id)
        if bridge and resource_id:
            await _start_transition(hass, bridge, resource_type, resource_id, entity_id, direction, sweep, limit)


async def _handle_stop(hass: HomeAssistant, call: ServiceCall):
    entity_ids = await async_extract_entity_ids(call)
    for entity_id in entity_ids:
        if not entity_id.startswith("light."):
            continue
        bridge, resource_type, resource_id = resolve_entity(hass, entity_id)
        if not bridge or not resource_id:
            continue

        controller = _get_controller(bridge, resource_type)
        try:
            await controller.set_dimming_delta(resource_id)
        except Exception as exc:
            _LOGGER.debug("Stop command ignored for %s: %s", resource_id, exc)
            continue

        current_bright = resolve_brightness(hass, entity_id)
        _brightness_cache[entity_id] = {
            "time": time.time(),
            "bright": current_bright,
            "target": current_bright,
            "dir": DIR_NONE,
            "sweep": 1.0,
        }

        _LOGGER.debug("STOP [%s]: Halted at %.1f%%", entity_id, current_bright)


def _resolve_group_light_ids(bridge, grouped_light_id):
    # Resolve a grouped_light to its member light resource IDs via aiohue cache.
    return [light.id for light in bridge.api.groups.grouped_light.get_lights(grouped_light_id)]


def _get_cached_brightness(bridge, resource_type, resource_id):
    # Read brightness from aiohue's cached model (sync, no API call).
    # Used as fallback when HA state is null (light off, cache expired).
    controller = _get_controller(bridge, resource_type)
    model = controller.get(resource_id)
    if model and model.dimming:
        return model.dimming.brightness
    return 0.0


def _get_cached_light_attributes(bridge, light_id):
    # Read brightness, CT, and XY color from aiohue's cached light model (sync).
    model = bridge.api.lights.get(light_id)
    if not model:
        return 0.0, None, None
    brightness = model.dimming.brightness if model.dimming else 0.0
    mirek = model.color_temperature.mirek if model.color_temperature else None
    color_temp_kelvin = round(1_000_000 / mirek) if mirek else None
    color_xy = (round(model.color.xy.x, 4), round(model.color.xy.y, 4)) if model.color else None
    return brightness, color_temp_kelvin, color_xy


def _get_cached_group_attributes(bridge, grouped_light_id):
    # Aggregate brightness, CT, and XY color from individual lights in a group (sync).
    # Groups don't report CT or color directly, and report brightness as 0 when off.
    lights = bridge.api.groups.grouped_light.get_lights(grouped_light_id)
    if not lights:
        return 0.0, None, None

    brightnesses, mireks, xs, ys = [], [], [], []
    for light in lights:
        if light.dimming:
            brightnesses.append(light.dimming.brightness)
        if light.color_temperature and light.color_temperature.mirek:
            mireks.append(light.color_temperature.mirek)
        if light.color:
            xs.append(light.color.xy.x)
            ys.append(light.color.xy.y)

    avg_brightness = sum(brightnesses) / len(brightnesses) if brightnesses else 0.0
    avg_ct_kelvin = round(1_000_000 / round(sum(mireks) / len(mireks))) if mireks else None
    color_xy = (round(sum(xs) / len(xs), 4), round(sum(ys) / len(ys), 4)) if xs else None
    return avg_brightness, avg_ct_kelvin, color_xy


def _resolve_color_xy(hass, entity_id, xy_color, hs_color, rgb_color):
    # Convert whichever color format was provided to an XY tuple.
    # Priority: rgb_color > hs_color > xy_color. Warns if more than one is set.
    provided = [f for f in [rgb_color, hs_color, xy_color] if f is not None]
    if not provided:
        return None
    if len(provided) > 1:
        _LOGGER.warning(
            "set_attributes: multiple color fields set for %s; using rgb_color > hs_color > xy_color priority.",
            entity_id,
        )

    state = hass.states.get(entity_id)
    supported_modes = state.attributes.get("supported_color_modes", []) if state else []
    if "xy" not in supported_modes:
        _LOGGER.warning("Entity %s does not support XY color. Skipping color.", entity_id)
        return None

    if rgb_color is not None:
        return color_RGB_to_xy(int(rgb_color[0]), int(rgb_color[1]), int(rgb_color[2]))
    if hs_color is not None:
        return color_hs_to_xy(float(hs_color[0]), float(hs_color[1]))
    return (float(xy_color[0]), float(xy_color[1]))


def _resolve_color_temp(hass, entity_id, color_temp_kelvin):
    # Validate CT support, clamp to entity range, and convert to mirek.
    state = hass.states.get(entity_id)
    supported_modes = state.attributes.get("supported_color_modes", []) if state else []

    if "color_temp" not in supported_modes:
        _LOGGER.warning(
            "Entity %s does not support color temperature. Skipping CT.",
            entity_id,
        )
        return None

    min_k = state.attributes.get("min_color_temp_kelvin", 2000)
    max_k = state.attributes.get("max_color_temp_kelvin", 6535)
    clamped_k = max(min_k, min(max_k, color_temp_kelvin))
    return round(1_000_000 / clamped_k)


async def _send_set_attributes(bridge, resource_type, resource_id, brightness, color_temp_mirek, color_xy):
    # For groups, send to each individual light so attributes apply even when off.
    if resource_type == "grouped_light":
        light_ids = _resolve_group_light_ids(bridge, resource_id)
        if not light_ids:
            _LOGGER.warning("No lights found in group %s", resource_id)
            return
    else:
        light_ids = [resource_id]

    for light_id in light_ids:
        try:
            await bridge.api.lights.set_state(
                light_id,
                brightness=float(brightness) if brightness is not None else None,
                color_temp=color_temp_mirek,
                color_xy=color_xy,
            )
        except Exception as exc:
            _LOGGER.error("set_attributes failed for light %s: %s", light_id, exc)


def _clamp_brightness(current, min_brightness, max_brightness):
    clamped = current
    if min_brightness is not None:
        clamped = max(float(min_brightness), clamped)
    if max_brightness is not None:
        clamped = min(float(max_brightness), clamped)
    return clamped if abs(clamped - current) > 0.1 else None


def _positive_or_none(value):
    if value is None:
        return None
    f = float(value)
    return f if f > 0 else None


async def _handle_set_attributes(hass: HomeAssistant, call: ServiceCall):
    brightness = call.data.get("brightness")
    min_brightness = _positive_or_none(call.data.get("min_brightness"))
    max_brightness = _positive_or_none(call.data.get("max_brightness"))
    color_temp_kelvin = _positive_or_none(call.data.get("color_temp_kelvin"))
    xy_color = call.data.get("xy_color")
    hs_color = call.data.get("hs_color")
    rgb_color = call.data.get("rgb_color")

    has_explicit = brightness is not None
    has_clamp = min_brightness is not None or max_brightness is not None
    has_ct = color_temp_kelvin is not None
    has_color = xy_color is not None or hs_color is not None or rgb_color is not None

    if not has_explicit and not has_clamp and not has_ct and not has_color:
        _LOGGER.warning("set_attributes called with no attributes to set.")
        return

    entity_ids = await async_extract_entity_ids(call)
    for entity_id in entity_ids:
        if not entity_id.startswith("light."):
            continue
        bridge, resource_type, resource_id = resolve_entity(hass, entity_id)
        if not bridge or not resource_id:
            continue

        # Resolve brightness per-entity (explicit value, or clamped from current)
        entity_brightness = brightness
        if not has_explicit and has_clamp:
            current = resolve_brightness(hass, entity_id)
            if current < 0.1:
                if resource_type == "grouped_light":
                    current, _, _ = _get_cached_group_attributes(bridge, resource_id)
                else:
                    current = _get_cached_brightness(bridge, resource_type, resource_id)
            entity_brightness = _clamp_brightness(current, min_brightness, max_brightness)

        color_temp_mirek = _resolve_color_temp(hass, entity_id, color_temp_kelvin) if has_ct else None
        color_xy = _resolve_color_xy(hass, entity_id, xy_color, hs_color, rgb_color) if has_color else None
        if entity_brightness is not None or color_temp_mirek is not None or color_xy is not None:
            await _send_set_attributes(
                bridge, resource_type, resource_id, entity_brightness, color_temp_mirek, color_xy
            )


async def _handle_get_attributes(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    result = {}
    entity_ids = await async_extract_entity_ids(call)
    for entity_id in entity_ids:
        if not entity_id.startswith("light."):
            continue
        bridge, resource_type, resource_id = resolve_entity(hass, entity_id)
        if not bridge or not resource_id:
            continue

        if resource_type == "grouped_light":
            brightness, color_temp_kelvin, color_xy = _get_cached_group_attributes(bridge, resource_id)
        else:
            # Brightness: cache/HA first, aiohue cache fallback
            brightness = resolve_brightness(hass, entity_id)
            if brightness < 0.1:
                brightness = _get_cached_brightness(bridge, resource_type, resource_id)
            # CT and color: always from aiohue cache (HA doesn't retain these when off)
            _, color_temp_kelvin, color_xy = _get_cached_light_attributes(bridge, resource_id)

        if color_xy:
            hs = color_xy_to_hs(*color_xy)
            rgb = color_xy_to_RGB(*color_xy)
        else:
            hs = rgb = None

        attrs: dict = {"brightness": round(brightness, 1)}
        if color_temp_kelvin is not None:
            attrs["color_temp_kelvin"] = color_temp_kelvin
        if color_xy is not None:
            attrs["xy_color"] = list(color_xy)
            attrs["rgb_color"] = list(rgb)
            attrs["hs_color"] = list(hs)
        result[entity_id] = attrs

    return result


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    # Register services for the Hue Smooth Dimmer.

    async def handle_raise(call: ServiceCall):
        await _handle_transition(hass, call, DIR_UP, DEFAULT_MAX_BRIGHTNESS)

    async def handle_lower(call: ServiceCall):
        await _handle_transition(hass, call, DIR_DOWN, DEFAULT_MIN_BRIGHTNESS)

    async def handle_stop(call: ServiceCall):
        await _handle_stop(hass, call)

    async def handle_set_attributes(call: ServiceCall):
        await _handle_set_attributes(hass, call)

    async def handle_get_attributes(call: ServiceCall) -> ServiceResponse:
        return await _handle_get_attributes(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_RAISE, handle_raise)
    hass.services.async_register(DOMAIN, SERVICE_LOWER, handle_lower)
    hass.services.async_register(DOMAIN, SERVICE_STOP, handle_stop)
    hass.services.async_register(DOMAIN, SERVICE_SET_ATTRIBUTES, handle_set_attributes)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_ATTRIBUTES, handle_get_attributes, supports_response=SupportsResponse.ONLY
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    for svc in [SERVICE_RAISE, SERVICE_LOWER, SERVICE_STOP, SERVICE_SET_ATTRIBUTES, SERVICE_GET_ATTRIBUTES]:
        hass.services.async_remove(DOMAIN, svc)
    return True
