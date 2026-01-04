import time
import asyncio
import logging
from homeassistant.core import ServiceCall, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntry

from .const import (
    DOMAIN, 
    DEFAULT_SWEEP_TIME, 
    STALE_BRIGHTNESS_GUARD_SECONDS,
    DEFAULT_MAX_BRIGHTNESS,
    DEFAULT_MIN_BRIGHTNESS,
    SERVICE_RAISE,
    SERVICE_LOWER,
    SERVICE_STOP
)

_LOGGER = logging.getLogger(__name__)

# Tracker: { resource_id: { "time": float, "bright": float, "target": float, "dir": str, "sweep": float } }
STATE_TRACKER = {}

async def get_bridge_and_id(hass: HomeAssistant, entity_id: str):
    """Retrieves the Hue Bridge instance and Resource UUID, ensuring it is V2."""
    from homeassistant.components.hue.const import DOMAIN as HUE_DOMAIN
    
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if not entry or entry.platform != HUE_DOMAIN:
        _LOGGER.error("Entity %s is not a Philips Hue entity.", entity_id)
        return None, None

    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    bridge = getattr(config_entry, "runtime_data", None)
    
    if not bridge:
        _LOGGER.error("Hue bridge runtime_data not found for %s", entity_id)
        return None, None

    # V2 Bridge Check
    if getattr(bridge, "api_version", 1) < 2:
        _LOGGER.error("Hue Smooth Dimmer requires a V2 (Square) Bridge for %s", entity_id)
        return None, None

    resource_id = entry.unique_id
    if "-" in resource_id and ":" in resource_id:
        resource_id = resource_id.split(":")[-1]
    
    return bridge, resource_id

def resolve_current_brightness(resource_id, api_brightness):
    """
    Handles 'Target Snapping', a Philips Hue quirk where the API snaps its state/brightness reporting to a
    transition's end state when the transition starts (e.g. raise to 100% over 60s reports 100% immediately).
    When a transition is stopped mid flight, the API takes ~10 seconds to catch up and report correctly.
    
    The resolver detects the catch-up window and falls back on an internal brightness prediction if the API
    can't be trusted, to ensure rapid dim-stop-dim action sequences work smoothly.
    """
    state = STATE_TRACKER.get(resource_id)
    if not state:
        return api_brightness

    now = time.time()
    elapsed = now - state["time"]

    # CASE 1: Stationary (Direction is 'none')
    if state["dir"] == "none":
        # Check if the bridge is snapping to the target of the move we JUST stopped
        is_target_snap = abs(api_brightness - state["target"]) < 0.5
        
        if is_target_snap and elapsed < STALE_BRIGHTNESS_GUARD_SECONDS:
            _LOGGER.debug("TRACKER [%s]: Ignoring snap to aborted target %.1f%%. Staying at %.1f%%", 
                         resource_id, api_brightness, state["bright"])
            return state["bright"]
            
        # Detect manual/external overrides
        if not is_target_snap and abs(api_brightness - state["bright"]) >= 0.5:
            _LOGGER.info("External change detected for %s. Clearing tracker.", resource_id)
            STATE_TRACKER.pop(resource_id, None)
            return api_brightness
        
        # Guard window check for idle state
        return state["bright"] if elapsed < STALE_BRIGHTNESS_GUARD_SECONDS else api_brightness

    # CASE 2: Moving
    is_at_target = abs(api_brightness - state["target"]) < 0.5
    
    if is_at_target and elapsed < STALE_BRIGHTNESS_GUARD_SECONDS:
        change = (100.0 / state["sweep"]) * elapsed
        if state["dir"] == "up":
            predicted = min(state["bright"] + change, 100.0)
        else:
            predicted = max(state["bright"] - change, 0.0)
        
        _LOGGER.debug("TRACKER [%s]: Moving snap ignored. API: %.1f%%, Predicted: %.1f%%", 
                     resource_id, api_brightness, predicted)
        return predicted

    # Cleanup if the guard window has passed
    if elapsed > STALE_BRIGHTNESS_GUARD_SECONDS:
        STATE_TRACKER.pop(resource_id, None)

    return api_brightness

async def start_transition(bridge, resource_id, direction, sweep, limit):
    """Executes transition and stores state metadata for snap protection."""
    try:
        response = await bridge.api.request("get", f"clip/v2/resource/light/{resource_id}")
        real_data = response[0] if isinstance(response, list) else response
        api_bright = float(real_data.get("dimming", {}).get("brightness", 0.0))
    except Exception as e:
        _LOGGER.error("Failed to fetch state for %s: %s", resource_id, e)
        return

    current_bright = resolve_current_brightness(resource_id, api_bright)
    distance = abs(limit - current_bright)
    dur_ms = int(distance * sweep * 10)
    
    _LOGGER.info("CALC [%s]: %.1f%% -> %.1f%% | Dur: %dms", 
                 resource_id, current_bright, limit, dur_ms)

    if distance <= 0.2:
        return

    STATE_TRACKER[resource_id] = {
        "time": time.time(),
        "bright": current_bright,
        "target": limit,
        "dir": direction,
        "sweep": sweep
    }
    
    payload = {
        "dimming": {"brightness": limit},
        "dynamics": {"duration": dur_ms}
    }
    if direction == "up": 
        payload["on"] = {"on": True}
    elif direction == "down" and limit == 0.0:
        payload["on"] = {"on": False} # Turn off light after fading to 0% brightness

    await bridge.api.request("put", f"clip/v2/resource/light/{resource_id}", json=payload)

def _prune_tracker():
    """Cleanup helper to prevent memory leaks."""
    now = time.time()
    to_delete = [
        res_id for res_id, state in STATE_TRACKER.items()
        if (now - state["time"]) > STALE_BRIGHTNESS_GUARD_SECONDS
    ]
    for res_id in to_delete:
        del STATE_TRACKER[res_id]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Register services for the Hue Smooth Dimmer."""

    async def handle_raise(call: ServiceCall):
        _prune_tracker()
        sweep = float(call.data.get("sweep_time", DEFAULT_SWEEP_TIME))
        limit = float(call.data.get("limit", DEFAULT_MAX_BRIGHTNESS))
        
        for entity_id in call.data.get("entity_id", []):
            bridge, resource_id = await get_bridge_and_id(hass, entity_id)
            if bridge and resource_id:
                await start_transition(bridge, resource_id, "up", sweep, limit)

    async def handle_lower(call: ServiceCall):
        _prune_tracker()
        sweep = float(call.data.get("sweep_time", DEFAULT_SWEEP_TIME))
        limit = float(call.data.get("limit", DEFAULT_MIN_BRIGHTNESS))
        
        for entity_id in call.data.get("entity_id", []):
            bridge, resource_id = await get_bridge_and_id(hass, entity_id)
            if bridge and resource_id:
                await start_transition(bridge, resource_id, "down", sweep, limit)

    async def handle_stop(call: ServiceCall):
        _prune_tracker()
        for entity_id in call.data.get("entity_id", []):
            bridge, resource_id = await get_bridge_and_id(hass, entity_id)
            if not bridge or not resource_id:
                continue
            
            await bridge.api.request("put", f"clip/v2/resource/light/{resource_id}", 
                                      json={"dimming_delta": {"action": "stop"}})
            
            try:
                response = await bridge.api.request("get", f"clip/v2/resource/light/{resource_id}")
                api_bright = float(response[0].get("dimming", {}).get("brightness", 0.0))
            except Exception:
                api_bright = 0.0
                
            final_bright = resolve_current_brightness(resource_id, api_bright)
            old_state = STATE_TRACKER.get(resource_id, {})
            
            # STOP-SNAP DEFENSE: Store the aborted target
            STATE_TRACKER[resource_id] = {
                "time": time.time(),
                "bright": final_bright,
                "target": old_state.get("target", final_bright),
                "dir": "none",
                "sweep": 1.0 
            }
            _LOGGER.info("STOP [%s]: Halted at %.1f%% (Guarding against snap to %.1f%%)", 
                         resource_id, final_bright, STATE_TRACKER[resource_id]["target"])

    hass.services.async_register(DOMAIN, SERVICE_RAISE, handle_raise)
    hass.services.async_register(DOMAIN, SERVICE_LOWER, handle_lower)
    hass.services.async_register(DOMAIN, SERVICE_STOP, handle_stop)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    for svc in [SERVICE_RAISE, SERVICE_LOWER, SERVICE_STOP]:
        hass.services.async_remove(DOMAIN, svc)
    return True