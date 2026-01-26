# Philips Hue Smooth Dimmer

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Dim your Hue bulbs smoothly in "press to dim, release to stop" automations.

This integration eliminates the visual stuttering caused by "stepped" dimming loops, using native dimming methods in the Philips Hue V2 API.

## Key Benefits 🔅💡🔆

* **Silky Smooth:** No more jumpy brightness changes or overshoots in "press to dim" automations. Brightness transitions are continuous, predictable and polished, mirroring the behavior of a high-quality physical dimmer.
* **Instant setup:** The integration works with the lights you've already added to the core HA Hue integration. 
* **Network Friendly:** With less chatter between HA and your lights, your home network and Hue mesh remain responsive and clear.

---

## Requirements:
* **Hardware:** Philips Hue Bridge V2. Legacy V1 bridges are not supported.
* **[Philips Hue integration](https://www.home-assistant.io/integrations/hue)** installed.

## Installation

### Method 1: HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jasonmx&repository=philips-hue-smooth-dimmer&category=integration)

### Method 2: Manual
1. Copy the `hue_dimmer` folder to your `/config/custom_components/` directory.
2. **Restart Home Assistant.**
3. Go to **Settings > Devices & Services**, click **Add Integration** and search for "Philips Hue Smooth Dimmer".

---

## Services

After installation, you'll find 3 new services in the automation Actions list. You can also use them in Developer Tools -> Actions. 

### `hue_dimmer.raise`
Starts increasing the brightness.

| Field | Default | Description |
| :--- | :--- | :--- |
| `target` | (Required) | Hue light(s) or group(s) to control. |
| `sweep_time` | `5` | Seconds for a full 0-100% transition. |
| `limit` | `100` | Stop transition at this brightness (default 100%). |

### `hue_dimmer.lower`
Starts decreasing the brightness, and turns off at 0%.

| Field | Default | Description |
| :--- | :--- | :--- |
| `target` | (Required) | Hue light(s) or group(s) to control. |
| `sweep_time` | `5` | Seconds for a full 100-0% transition. |
| `limit` | `0` | Stop transition at this brightness (default 0%). Choose 0.2% or more to keep a light turned on. |

> [!TIP]
> Hue's minimum supported brightness is 0.2% for regular bulbs and 2.0% for Essential bulbs. Source: [Hueblog post](https://hueblog.com/2025/09/18/new-hue-bulbs-cannot-be-dimmed-any-lower/).

### `hue_dimmer.stop`
Stops an active transition.

---

## Example Usage

To dim your Hue lights smoothly with a two-button remote:

```yaml
actions:
  - choose:
      - conditions:
          - condition: trigger
            id:
              - long_press_left
        sequence:
          - action: hue_dimmer.lower
            target:
              entity_id: light.living_room
      - conditions:
          - condition: trigger
            id:
              - long_press_right
        sequence:
          - action: hue_dimmer.raise
            target:
              entity_id: light.living_room
      - conditions:
          - condition: trigger
            id:
              - release_left
              - release_right
        sequence:
          - action: hue_dimmer.stop
            target:
              entity_id: light.living_room
```

> [!TIP]
> To control multiple lights, target a **Hue Group** rather than multiple individual lights. This allows the Hue Bridge to send a single Zigbee broadcast, which dims all the lights together perfectly.
