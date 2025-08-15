"""Config flow for the SOLID integration."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN


class SolidConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solid."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step of the config flow."""
        errors = {}

        if user_input is not None:
            if (
                not user_input.get("CLIENT_TOKEN")
                or not user_input.get("CLIENT_SECRET")
                or not user_input.get("OIDC")
                or not user_input.get("POD")
            ):
                errors["base"] = "missing_credentials"
            else:
                return self.async_create_entry(
                    title="Solid Integration",
                    data={
                        "POD": user_input["POD"],
                        "OIDC": user_input["OIDC"],
                        "CLIENT_TOKEN": user_input["CLIENT_TOKEN"],
                        "CLIENT_SECRET": user_input["CLIENT_SECRET"],
                        "SENSOR": user_input.get("SENSOR"),
                    },
                )

        entity_registry = er.async_get(self.hass)
        sensors = [
            entity.entity_id
            for entity in entity_registry.entities.values()
            if entity.domain == "sensor"
        ]

        data_schema = vol.Schema(
            {
                vol.Required(
                    "OIDC", default="https://tmdt-solid-community-server.de"
                ): cv.string,
                vol.Required(
                    "POD", default="https://tmdt-solid-community-server.de/test"
                ): cv.string,
                vol.Required(
                    "CLIENT_TOKEN", default="test_a7b7cc3a-a02c-4392-a0b7-9cfa1ca6ed07"
                ): cv.string,
                vol.Required(
                    "CLIENT_SECRET",
                    default="c4fc9e643b96b6b157f38895966635b2e304cdee9bae01a4b8ce3a871acd37d7447b362552b3163633138b99f20a829a2d36379edb2d8a770c86136a17376d4c",
                ): cv.string,
                vol.Required("SENSOR", default="sensor.date_time_iso"): vol.In(
                    sorted(sensors)
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "sensors": ", ".join(sensors) if sensors else "No sensors available"
            },
        )
