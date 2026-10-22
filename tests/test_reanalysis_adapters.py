"""Software-only checks for the immutable source adapter registry."""
import dataclasses
import unittest

from arktika.reanalysis.adapters import (
    ADAPTERS,
    AdapterMetadata,
    get_adapter,
    require_supported,
    supports_variable,
)


class ReanalysisAdapterRegistryTests(unittest.TestCase):
    def test_registers_current_sources_with_provider_metadata(self):
        self.assertEqual(set(ADAPTERS), {"era5", "carra2", "merra2"})
        self.assertEqual(get_adapter("era5").transport_id, "cds")
        self.assertEqual(get_adapter("carra2").auth_provider, "cds")
        merra = get_adapter("merra2")
        self.assertEqual(merra.transport_id, "earthdata_cmr_opendap")
        self.assertEqual(merra.auth_provider, "earthdata")
        self.assertEqual(set(merra.dataset_ids), {"M2I3NPASM", "M2I1NXASM"})

    def test_supported_fields_match_existing_catalog_capabilities(self):
        self.assertTrue(supports_variable("era5", "omega"))
        self.assertFalse(supports_variable("carra2", "omega"))
        self.assertFalse(supports_variable("merra2", "rh"))
        self.assertTrue(supports_variable("carra2", "wind"))
        self.assertFalse(supports_variable("invented-source", "t"))
        self.assertFalse(supports_variable("era5", "invented-field"))

    def test_unknown_source_has_safe_error(self):
        with self.assertRaisesRegex(ValueError, "Неизвестный источник реанализа"):
            get_adapter("secret-or-unknown")

    def test_unsupported_field_has_safe_error(self):
        with self.assertRaisesRegex(ValueError, "не поддерживается источником"):
            require_supported("carra2", "omega")

    def test_metadata_and_registry_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            get_adapter("era5").name = "renamed"
        with self.assertRaises(TypeError):
            ADAPTERS["new-source"] = AdapterMetadata(
                source_id="new-source",
                name="unverified",
                transport_id="none",
                auth_provider="none",
                dataset_ids=(),
                supported_variables=frozenset(),
            )
        with self.assertRaises(AttributeError):
            get_adapter("era5").supported_variables.add("invented")

    def test_adapter_metadata_contains_no_credentials(self):
        for adapter in ADAPTERS.values():
            self.assertFalse(hasattr(adapter, "token"))
            self.assertFalse(hasattr(adapter, "password"))


if __name__ == "__main__":
    unittest.main()
