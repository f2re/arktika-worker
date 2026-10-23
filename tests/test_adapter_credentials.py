"""Проверка безопасной схемы реквизитов установленных адаптеров."""
import unittest

from arktika.reanalysis.adapters import (
    AdapterImplementation, AdapterMetadata, AdapterRegistry, CredentialField,
)


def adapter(provider="research_auth", fields=None, hosts=("data.example.org",)):
    return AdapterImplementation(
        metadata=AdapterMetadata(
            source_id="custom_reanalysis", name="Тестовый источник", transport_id="test",
            auth_provider=provider, dataset_ids=("dataset",), supported_variables=frozenset({"mslp"}),
            credential_fields=tuple(fields if fields is not None else (
                CredentialField("username", "Имя пользователя", "text", max_length=128),
                CredentialField("token", "Токен", "password", max_length=4096),
            )), credential_help="Доступ тестового источника.", allowed_hosts=hosts,
        ),
        planner=lambda request: request,
        fetcher=lambda plan, credential, folder, progress: ([], []),
        normalizer=lambda dataset, plan, area=None: dataset,
        catalogue_builder=lambda: {
            "id": "custom_reanalysis", "name": "Тестовый источник", "auth": provider,
            "start": 2020, "step_hours": 1, "resolution_km": 10,
            "variables": [{"id": "mslp", "title": "Давление", "unit": "Pa",
                           "vertical": "surface", "levels": [], "styles": ["fill"]}],
        },
    )


class AdapterCredentialMetadata(unittest.TestCase):
    def test_custom_provider_registered_and_descriptor_is_safe(self):
        registry = AdapterRegistry((adapter(),))
        descriptor = registry.get("custom_reanalysis").credential_request()
        self.assertEqual(descriptor, {
            "provider": "research_auth", "format": "fields",
            "help": "Доступ тестового источника.",
            "fields": [
                {"id": "username", "label": "Имя пользователя", "type": "text",
                 "required": True, "help": "", "max_length": 128},
                {"id": "token", "label": "Токен", "type": "password",
                 "required": True, "help": "", "max_length": 4096},
            ],
        })
        self.assertNotIn("secret", str(descriptor).lower())

    def test_unknown_or_unsafe_metadata_rejected(self):
        for candidate in (
            adapter("../../custom"),
            adapter(fields=(CredentialField("api-key", "Ключ"),)),
            adapter(fields=(CredentialField("token", "Ключ", "hidden"),)),
            adapter(hosts=("*.example.org",)),
            adapter(hosts=("data.example.org:443",)),
            adapter(fields=(CredentialField("token", "Ключ"), CredentialField("token", "Дубль"))),
            adapter(fields=(CredentialField("token", "Ключ", max_length=9000),)),
            adapter(fields=()),
        ):
            with self.subTest(provider=candidate.auth_provider), self.assertRaises(ValueError):
                AdapterRegistry((candidate,))

    def test_existing_builtin_provider_schemas_remain_explicit(self):
        from arktika.reanalysis.adapters import DEFAULT_REGISTRY
        cds = DEFAULT_REGISTRY.get("era5").credential_request()
        earthdata = DEFAULT_REGISTRY.get("merra2").credential_request()
        self.assertEqual(cds["provider"], "cds")
        self.assertEqual(cds["fields"][0]["id"], "token")
        self.assertEqual(earthdata["provider"], "earthdata")
        self.assertTrue(DEFAULT_REGISTRY.get("carra2").metadata.allowed_hosts)


if __name__ == "__main__":
    unittest.main()
