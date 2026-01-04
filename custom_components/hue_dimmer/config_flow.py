from homeassistant import config_entries
from .const import DOMAIN

class HueDimmerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Philips Hue Smooth Dimmer."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step when the user clicks 'Add Integration'."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="Philips Hue Smooth Dimmer", data={})