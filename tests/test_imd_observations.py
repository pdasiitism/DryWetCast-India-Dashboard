from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import imd_observations as observations


class ObservationTests(unittest.TestCase):
    def test_fallback_preserves_imd_grid_date_and_rainfall_values(self):
        expected = np.arange(129 * 135, dtype='<f4').reshape(129, 135) / 100
        def download(command, **kwargs):
            self.assertIn('rain=22112026', command)
            Path(command[-1]).write_bytes(expected.tobytes())
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(observations.imd, 'load', side_effect=observations.imd.DataNotAvailableError('empty reply')), \
                patch.object(observations.subprocess, 'run', side_effect=download):
            result = observations.load_rainfall('2026-11-22', '2026-11-22', Path(directory)).get_xarray()['rain']
            self.assertEqual(result.dims, ('time', 'lat', 'lon'))
            self.assertEqual(str(result.time.values[0])[:10], '2026-11-22')
            np.testing.assert_allclose(result.values[0], expected)
            self.assertEqual(float(result.lat.values[0]), 6.5)
            self.assertEqual(float(result.lon.values[0]), 66.5)

    def test_incomplete_payload_is_not_accepted_as_observations(self):
        def download(command, **kwargs):
            Path(command[-1]).write_bytes(b'incomplete')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(observations.subprocess, 'run', side_effect=download):
            with self.assertRaises(observations.imd.DownloadError):
                observations.download_day(date(2026, 11, 22), Path(directory))
            self.assertFalse(list(Path(directory).glob('*.grd')))
            self.assertFalse(list(Path(directory).glob('*.part')))

    def test_empty_reply_remains_unavailable(self):
        def download(command, **kwargs):
            Path(command[-1]).write_bytes(b'')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(observations.subprocess, 'run', side_effect=download):
            with self.assertRaises(observations.imd.DataNotAvailableError):
                observations.download_day(date(2026, 11, 22), Path(directory))


if __name__ == '__main__':
    unittest.main()
