import unittest

from arktika.product_schema import product_passport


class ProductSchemaTests(unittest.TestCase):
    def base(self):
        return dict(product_id='channel', algorithm_id='arktika.channel', algorithm_version='1',
                    maturity='implemented', quantity_kind='stored_value', quantity='DN', units='DN',
                    valid_time='2026-01-01T00:00:00Z', data_kind='observation',
                    grid={'crs':'EPSG:4326','width':256,'height':128},
                    source_grids=[{'crs':'EPSG:4326','width':1024,'height':512}], nodata='NaN',
                    quality_control={}, sources=[{'id':'source','sha256':'a'*64}],
                    calibration_version='1', calibration_status='unknown', limitations=[])

    def test_missing_uncertainty_is_not_silently_clear(self):
        passport=product_passport(**self.base())
        self.assertEqual(passport['uncertainty'],{'status':'not_assessed','method':None})

    def test_rejects_non_utc_time(self):
        values=self.base()
        values['valid_time']='2026-01-01T03:00:00+03:00'
        with self.assertRaisesRegex(ValueError,'UTC'):
            product_passport(**values)

    def test_rejects_invalid_maturity(self):
        values=self.base()
        values['maturity']='profile_tool'
        with self.assertRaisesRegex(ValueError,'готовности'):
            product_passport(**values)

    def test_rejects_empty_grid_dimensions(self):
        values=self.base()
        values['grid']['width']=0
        with self.assertRaisesRegex(ValueError,'сетке'):
            product_passport(**values)

    def test_archive_product_can_preserve_a_valid_period_without_inventing_one_time(self):
        values=self.base()
        values.pop('valid_time')
        values['valid_period']={'start':'2026-01-01T00:00:00Z','end':'2026-01-02T00:00:00Z'}
        passport=product_passport(**values)
        self.assertNotIn('valid_time',passport)
        self.assertEqual(values['valid_period'],passport['valid_period'])

    def test_rejects_invalid_or_reversed_valid_period(self):
        values=self.base()
        values.pop('valid_time')
        values['valid_period']={'start':'2026-01-02T00:00:00Z','end':'2026-01-01T00:00:00Z'}
        with self.assertRaisesRegex(ValueError,'раньше'):
            product_passport(**values)


if __name__ == '__main__':
    unittest.main()
