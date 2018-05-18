import datetime
import logging
import numpy as np
import os
import unittest

import loudml.vendor
import loudml.errors as errors
from loudml.memdatasource import MemDataSource

logging.getLogger('tensorflow').disabled = True

from loudml.timeseries import TimeSeriesModel

FEATURES = [
    {
        'name': 'avg_foo',
        'metric': 'avg',
        'field': 'foo',
        'default': 0,
    },
]

class TestMemDataSource(unittest.TestCase):
    def setUp(self):
        self.source = MemDataSource()

        self.model = TimeSeriesModel(dict(
            name='test',
            offset=30,
            span=300,
            bucket_interval=3,
            interval=60,
            features=FEATURES,
            max_threshold=70,
            min_threshold=60,
        ))
        data = [
            # (foo, timestamp)
            (1, 0), # excluded
            (2, 1), (3, 2),
            # empty
            (4, 8),
            (5, 10), # excluded
        ]
        for entry in data:
            self.source.insert_times_data({
                'foo': entry[0],
                'timestamp': entry[1],
            })
        self.source.commit()

    def test_get_times_buckets(self):
        res = self.source.get_times_buckets(
            from_date=1,
            to_date=9,
            bucket_interval=3,
        )
        self.assertEqual(
            [[entry.data['foo'] for entry in bucket.data] for bucket in res],
            [[2, 3], [], [4]],
        )

    def test_get_times_data(self):
        res = self.source.get_times_data(self.model, from_date=1, to_date=9)

        self.assertEqual(
            [line[1] for line in res],
            [[2.5], [0], [4.0]],
        )
