import unittest

from arktika.product_schema import product_passport, validate_product_passport


class ProductSchemaTests(unittest.TestCase):
    def base(self):
        return dict(product_id='channel', algorithm_id='arktika.channel', algorithm_version='1',
                    maturity='implemented', quantity_kind='stored_value', quantity='DN', units='DN',
                    valid_time='2026-01-01T00:00:00Z', data_kind='observation',
                    grid={'crs':'EPSG:4326','width':256,'height':128,
                          'transform':[0.1,0,-180,0,-0.1,90]},
                    source_grids=[{'crs':'EPSG:4326','width':1024,'height':512,
                                   'transform':[0.025,0,-180,0,-0.025,90]}], nodata='NaN',
                    quality_control={}, sources=[{'id':'source','sha256':'a'*64}],
                    calibration_version='1', calibration_status='unknown', limitations=[])

    def test_missing_uncertainty_is_not_silently_clear(self):
        passport=product_passport(**self.base())
        self.assertEqual(passport['uncertainty'],{'status':'not_assessed','method':None})

    def test_serialized_passport_is_revalidated_without_dropping_extra_metadata(self):
        passport=product_passport(**self.base())
        passport['research_note']='extra product metadata'
        checked=validate_product_passport(passport)
        self.assertEqual(checked['algorithm'],passport['algorithm'])
        self.assertEqual(checked['valid_time'],passport['valid_time'])

    def test_serialized_passport_rejects_unknown_or_missing_contract(self):
        passport=product_passport(**self.base())
        del passport['units']
        with self.assertRaisesRegex(ValueError,'неполон'):
            validate_product_passport(passport)

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

    def test_rejects_grid_without_georeferencing(self):
        values=self.base()
        values['grid'].pop('transform')
        with self.assertRaisesRegex(ValueError,'affine-привязку'):
            product_passport(**values)

    def test_accepts_curvilinear_grid_only_with_coordinate_digest(self):
        values=self.base()
        geolocation={'grid_type':'curvilinear_latitude_longitude','coordinate_sha256':'a'*64,
                     'geolocation':'latitude_longitude_arrays'}
        values['grid'].pop('transform')
        values['grid'].update(geolocation)
        values['source_grids']=[dict(values['grid'])]
        passport=product_passport(**values)
        self.assertEqual(passport['grid']['coordinate_sha256'],'a'*64)

    def test_rejects_invalid_source_grid_geometry(self):
        values=self.base()
        values['source_grids']=[{'crs':'EPSG:4326','width':1024,'height':512}]
        with self.assertRaisesRegex(ValueError,'исходной сетке'):
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
