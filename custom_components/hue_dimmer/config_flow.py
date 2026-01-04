from homeassistant import config_entries
from .const import DOMAIN

class HueSmoothDimmerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hue Smooth Dimmer."""
    VERSION = 1

    async def async_step_user(self, user_input=None):

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        # We bypass show_form and go straight to creation
        return self.async_create_entry(
            title="Hue Smooth Dimmer", 
            data={}
        )