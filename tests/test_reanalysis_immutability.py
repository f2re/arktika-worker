"""Синтетические проверки: нормализация не изменяет исходные массивы."""
import unittest

import numpy as np

from test_reanalysis import HAS_FIELDS, dataset, plan


@unittest.skipUnless(HAS_FIELDS, 'Optional field dependencies unavailable')
class ImmutableInputs(unittest.TestCase):
    def test_conversion_accepts_readonly_arrays_and_preserves_inputs(self):
        import xarray as xr
        from arktika.reanalysis.normalize import convert
        cases = (
            ('t', 'K', 273.15, 0.), ('t2m', 'K', 273.15, 0.),
            ('sst', 'K', 273.15, 0.), ('mslp', 'Pa', 100000., 1000.),
            ('z', 'm2 s-2', 9806.65, 1000.), ('q', 'kg kg-1', .002, 2.),
            ('rh', '1', .5, 50.), ('ice', '1', .5, 50.),
            ('wind', 'm s-1', 10., 10.), ('omega', 'Pa s-1', 1., 1.),
        )
        for kind, units, value, expected in cases:
            for readonly in (False, True):
                with self.subTest(kind=kind, readonly=readonly):
                    source = np.array([value], dtype=np.float64)
                    source.setflags(write=not readonly)
                    variable = xr.DataArray(source, dims='x', attrs={'units': units})
                    for _ in range(2):
                        result = convert(variable, kind)
                        self.assertAlmostEqual(float(result[0]), expected, places=4)
                        self.assertEqual(result.dtype, np.dtype('float32'))
                        self.assertEqual(float(source[0]), value)

    def test_nonfinite_mask_does_not_modify_source(self):
        import xarray as xr
        from arktika.reanalysis.normalize import convert
        for readonly in (False, True):
            with self.subTest(readonly=readonly):
                source = np.array([4., np.inf, -np.inf, np.nan])
                source.setflags(write=not readonly)
                result = convert(xr.DataArray(source, attrs={'units': 'degC'}), 't')
                self.assertEqual(result[0], 4.)
                self.assertTrue(np.isnan(result[1:]).all())
                self.assertTrue(np.isposinf(source[1]))
                self.assertTrue(np.isneginf(source[2]))

    def pressure_case(self, readonly):
        from arktika.reanalysis.normalize import normalize
        ds = dataset().rename({'pressure_level': 'plev'}).assign_coords(
            plev=('plev', [85000.], {'units': 'Pa'}))
        # Keep a non-index CF coordinate to exercise both writable and readonly
        # backends independently of the installed pandas/xarray index policy.
        ds = ds.drop_indexes('plev')
        ds.plev.values.setflags(write=not readonly)
        for _ in range(2):
            result = normalize(ds, plan())
            self.assertTrue(np.isfinite(result.value).any())
            self.assertEqual(float(result.pressure_level), 850.)
            self.assertEqual(float(ds.plev.values[0]), 85000.)
            self.assertEqual(ds.plev.attrs['units'], 'Pa')

    def test_pressure_readonly(self):
        self.pressure_case(True)

    def test_pressure_writable_is_not_changed(self):
        self.pressure_case(False)


if __name__ == '__main__':
    unittest.main()
