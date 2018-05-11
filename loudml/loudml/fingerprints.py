"""
Loud ML fingerprints module
"""

import copy
import json
import logging
import math
import sys
import os

from itertools import repeat

assert sys.version.startswith('3')

_MAX_INT = sys.maxsize

import numpy as np

float_formatter = lambda x: "%.2f" % x
np.set_printoptions(formatter={'float_kind':float_formatter})

from voluptuous import (
    ALLOW_EXTRA,
    All,
    Any,
    Length,
    Range,
    Required,
    Optional,
    Boolean,
    Schema,
)

from . import (
    errors,
    schemas,
    som,
)

from .misc import (
    make_ts,
    parse_timedelta,
    ts_to_str,
    build_agg_name,
    Pool,
    chunks,
)
from .model import (
    Model,
    Feature,
)

class Aggregation:
    """
    Document aggregation that outputs features
    """

    SCHEMA = Schema({
        Required('measurement'): schemas.key,
        Required('features'): All([Feature.SCHEMA], Length(min=1)),
        'match_all': Any(None, Schema([
            {Required(schemas.key): Any(
                bool,
                int,
                float,
                All(str, Length(max=256)),
            )},
        ])),
    })

    def __init__(
        self,
        measurement=None,
        features=None,
        match_all=None,
    ):
        self.validate(locals())

        self.measurement = measurement
        self.features = [Feature(**feature) for feature in features]
        self.match_all = match_all

    @classmethod
    def validate(cls, args):
        del args['self']
        schemas.validate(cls.SCHEMA, args)


class FingerprintsPrediction:
    def __init__(self, fingerprints, from_ts, to_ts):
        self.from_ts = from_ts
        self.to_ts = to_ts
        self.fingerprints = fingerprints
        self.changed = None
        self.anomalies = None

    def format(self):
        fps = {
            fingerprint['key']: fingerprint
            for fingerprint in self.fingerprints
        }

        result = {
            'from_date': ts_to_str(self.from_ts),
            'to_date': ts_to_str(self.to_ts),
            'fingerprints': fps,
        }
        if self.changed is not None:
            result['changed'] = self.changed
        if self.anomalies is not None:
            result['anomalies'] = self.anomalies
        return result

    def __str__(self):
        return json.dumps(self.format(), indent=4)


def predict_scores(args):
    model, source, key, date_range = args
    model.load()
    prediction = model.predict(
        source,
        date_range[0],
        date_range[1],
        key,
    )
    model.detect_anomalies(prediction)
    model.unload()
    return prediction


class FingerprintsModel(Model):
    """
    Fingerprints model
    """

    TYPE = 'fingerprints'

    SCHEMA = Model.SCHEMA.extend({
        Required('key'): All(schemas.key, Length(max=256)),
        Required('max_keys'): All(int, Range(min=1)),
        Required('width'): All(int, Range(min=1)),
        Required('height'): All(int, Range(min=1)),
        Required('interval'): schemas.TimeDelta(min=0, min_included=False),
        Required('span'): schemas.TimeDelta(min=0, min_included=False),
        Optional('daytime_interval'): schemas.TimeDelta(min=0, min_included=False),
        Required('aggregations'): All([Aggregation.SCHEMA], Length(min=1)),
        'offset': schemas.TimeDelta(min=0),
        'timestamp_field': schemas.key,
    })

    def __init__(self, settings, state=None):
        super().__init__(settings, state)

        self.key = settings['key']
        self.max_keys = settings['max_keys']
        self.w = settings['width']
        self.h = settings['height']
        self.interval = parse_timedelta(settings['interval']).total_seconds()
        interval = self.settings.get('daytime_interval')
        if interval is None:
            self.daytime_interval = 0
        else:
            self.daytime_interval = parse_timedelta(interval).total_seconds()
        self.span = parse_timedelta(settings['span']).total_seconds()
        self.offset = parse_timedelta(settings.get('offset', 0)).total_seconds()
        self.timestamp_field = settings.get('timestamp_field', 'timestamp')

        self.aggs = [Aggregation(**agg) for agg in settings['aggregations']]
        self.features=[]
        for agg in self.aggs:
            self.features.extend(agg.features)

        if state is not None:
            self._state = state
            self._means = np.array(state['means'])
            self._stds = np.array(state['stds'])

        self._som_model = None

    @property
    def type(self):
        return self.TYPE

    @property
    def nb_quadrants(self):
        if self.daytime_interval == 0:
            return 1
        else:
            return int(24*3600 / self.daytime_interval)

    @property
    def nb_dimensions(self):
        return self.nb_quadrants * self.nb_features

    @property
    def state(self):
        if self._state is None:
            return None

        # XXX As we add 'means' and 'stds', we need a copy to avoid
        # modifying self._state in-place
        state = copy.deepcopy(self._state)
        state['means'] = self._means.tolist()
        state['stds'] = self._stds.tolist()
        return state

    @property
    def is_trained(self):
        return self._state is not None and 'ckpt' in self._state

    @property
    def feature_names(self):
        return [feature.name for feature in self.features]

    def format_quadrants(self, time_buckets, agg):
        # init: all zeros except the mins
        res = np.zeros(self.nb_quadrants * len(agg.features))
        counts = np.zeros(self.nb_quadrants * len(agg.features))
        sums = np.zeros(self.nb_quadrants * len(agg.features))
        sum_of_squares = np.zeros(self.nb_quadrants * len(agg.features))

        for quad_num in range(self.nb_quadrants):
            for feat_num, feature in enumerate(agg.features):
                quad_pos = quad_num * len(agg.features)
                _pos = quad_pos + feat_num
                if feature.metric == 'min':
                    res[_pos] = _MAX_INT

        for l in time_buckets:
            ts = make_ts(l['key_as_string'])
            quad_num = int((int(ts) / self.daytime_interval)) % self.nb_quadrants
            quad_pos = quad_num * len(agg.features)

            for feat_num, feature in enumerate(agg.features):
                _pos = quad_pos + feat_num
                s = l[build_agg_name(agg.measurement, feature.field)]
                _count = float(s['count'])
                if _count != 0:
                    _min = float(s['min'])
                    _max = float(s['max'])
                    _avg = float(s['avg'])
                    _sum = float(s['sum'])
                    _sum_of_squares = float(s['sum_of_squares'])
                    _variance = float(s['variance'])
                    _std_deviation = float(s['std_deviation'])

                    counts[_pos] = counts[_pos] + _count
                    sums[_pos] = sums[_pos] + _sum
                    sum_of_squares[_pos] = sum_of_squares[_pos] + _sum_of_squares
                    if feature.metric == 'count':
                        res[_pos] = res[_pos] + _count
                    elif feature.metric == 'min':
                        res[_pos] = min(res[_pos], _min)
                    elif feature.metric == 'max':
                        res[_pos] = max(res[_pos], _max)
                    elif feature.metric == 'avg':
                        # avg computed in the end
                        res[_pos] = res[_pos] + _sum
                    elif feature.metric == 'sum':
                        res[_pos] = res[_pos] + _sum
                    elif feature.metric == 'stddev':
                        # std computed in the end
                        res[_pos] = res[_pos] + _sum_of_squares

        for quad_num in range(self.nb_quadrants):
            for feat_num, feature in enumerate(agg.features):
                quad_pos = quad_num * len(agg.features)
                _pos = quad_pos + feat_num
                if feature.metric == 'min' and res[_pos] == _MAX_INT:
                    res[_pos] = 0

            for feat_num, feature in enumerate(agg.features):
                _pos = quad_pos + feat_num
                _count = counts[_pos]
                _sum = sums[_pos]
                _sum_of_squares = sum_of_squares[_pos]

                if _count > 0:
                    if feature.metric == 'avg':
                        res[_pos] = _sum / _count
                    elif feature.metric == 'stddev':
                        _variance = math.sqrt(_sum_of_squares / _count - (_sum/_count) ** 2)
                        res[_pos] = _variance

        return res

    def _train_on_dataset(
        self,
        dataset,
        num_epochs=100,
        limit=-1,
    ):
        # Apply data standardization to each feature individually
        # https://en.wikipedia.org/wiki/Feature_scaling
        self._means = np.mean(dataset, axis=0)
        self._stds = np.std(dataset, axis=0)
        # force std=1.0 (normal distribution) if std is null
        self._stds[self._stds == 0] = 1.0
        zY = np.nan_to_num((dataset - self._means) / self._stds)

        # Hyperparameters
        data_dimens = self.nb_dimensions
        self._som_model = som.SOM(self.w, self.h, data_dimens, num_epochs)

        # Start Training
        self._som_model.train(zY, truncate=limit)

        # Map vectors to their closest neurons
        return self._som_model.map_vects(zY)

    def set_fingerprint(
        self,
        key,
        fp,
    ):
        fps = {
            fingerprint['key']: fingerprint
            for fingerprint in self._state['fingerprints']
        }
        fps[key] = fp
        self._state['fingerprints'] = [val for key, val in fps.items()]
 
    def add_fingerprint(
        self,
        fp,
    ):
        fp['_fingerprint'] = [0] * len(fp['_fingerprint'])
        fp['fingerprint'] = [0] * len(fp['fingerprint'])
        self._state['fingerprints'].append(fp)

    def _norm_features(
        self,
        x,
        from_ts,
        to_ts,
    ):
        if self.is_trained:
            training_from_ts = make_ts(self._state['from_date'])
            training_to_ts = make_ts(self._state['to_date'])
        else:
            training_from_ts = from_ts
            training_to_ts = to_ts

        training_time_range = training_to_ts - training_from_ts
        time_range = to_ts - from_ts
        norm_time_range = np.empty(shape=(self.nb_quadrants, self.nb_features), dtype=float)
        norm_time_range[:] = np.nan

        for quad_num in range(self.nb_quadrants):
            for i, feature in enumerate(self.features):
                if feature.metric == 'count':
                    norm_time_range[quad_num, i] = training_time_range / time_range
                elif feature.metric == 'sum':
                    norm_time_range[quad_num, i] = training_time_range / time_range
                else:
                    norm_time_range[quad_num, i] = 1.0

        norm_time_range = np.ravel(norm_time_range)
        norm_x = np.nan_to_num((x*norm_time_range - self._means) / self._stds) 
        return norm_x 

    def _build_fingerprints(
        self,
        dataset,
        mapped,
        keys,
        from_ts,
        to_ts,
    ):
        fingerprints = []

        for i, x in enumerate(mapped):
            key = keys[i]
            _fingerprint = self._norm_features(dataset[i], from_ts, to_ts)
            fingerprints.append({
                'key': key,
                'time_range': (int(from_ts), int(to_ts)),
                'fingerprint': dataset[i].tolist(),
                '_fingerprint': _fingerprint.tolist(),
                'location': (mapped[i][0].item(), mapped[i][1].item()),
            })

        return fingerprints

    def _make_dataset(self, dicts):
        keys = set()
        for d in dicts:
            keys = keys.union(d.keys())

        nb_keys = len(keys)
        dimens = self.nb_dimensions
        dataset = np.zeros((nb_keys, dimens), dtype=float)

        for i, key in enumerate(keys):
            col = 0
            row = np.zeros((1, dimens), dtype=float)
            for agg_num, agg in enumerate(self.aggs):
                features_len = len(agg.features)
                if key in dicts[agg_num]:
                    features = dicts[agg_num][key]
                    for quad_num in range(self.nb_quadrants):
                        quad_pos = quad_num * features_len
                        row_pos = quad_num * self.nb_features + col
                        row[0][row_pos:row_pos+features_len] = features[quad_pos:quad_pos+features_len]
                col = col + features_len
            dataset[i] = row

        return list(keys), dataset

    def train(
        self,
        datasource,
        from_date,
        to_date="now",
        num_epochs=100,
        limit=-1,
    ):
        self._som_model = None
        self._means = None
        self._stds = None

        from_ts = make_ts(from_date)
        to_ts = make_ts(to_date)

        from_str = ts_to_str(from_ts)
        to_str = ts_to_str(to_ts)

        logging.info(
            "train(%s) range=[%s, %s] epochs=%d limit=%d)",
            self.name,
            from_str,
            to_str,
            num_epochs,
            limit,
        )

        # Fill dataset
        features_dicts=[]
        for agg_num, agg in enumerate(self.aggs):
            data = datasource.get_quadrant_data(self, agg, from_ts, to_ts)
            features = dict()
            for key, val in data:
                features[key] = self.format_quadrants(val, agg)
            features_dicts.append(features)

        keys, dataset = self._make_dataset(features_dicts)

        if len(keys) == 0:
            raise errors.NoData("no data found for time range {}-{}".format(
                from_str,
                to_str,
            ))

        logging.info("found %d keys", len(keys))

        mapped = self._train_on_dataset(
            dataset,
            num_epochs,
            limit,
        )

        model_ckpt, model_index, model_meta = som.serialize_model(self._som_model)
        fingerprints = self._build_fingerprints(
            dataset,
            mapped,
            keys,
            from_ts,
            to_ts,
        )

        self._state = {
            'ckpt': model_ckpt, # TF CKPT data encoded in base64
            'index': model_index,
            'meta': model_meta,
            'fingerprints': fingerprints,
            'from_date': ts_to_str(from_ts),
            'to_date': ts_to_str(to_ts),
        }

    def unload(self):
        del self._som_model
        self._som_model = None

    def load(self):
        if not self.is_trained:
            return errors.ModelNotTrained()

        self._som_model = som.load_model(
            self._state['ckpt'],
            self._state['index'],
            self._state['meta'],
            self.w,
            self.h,
            self.nb_dimensions,
        )

    @property
    def preview(self):
        trained = self.is_trained

        state = {
            'trained': self.is_trained
        }

        return {
            'settings': self.settings,
            'state': state,
        }

    def _map_dataset(self, dataset, from_ts, to_ts):
        zY = self._norm_features(dataset, from_ts, to_ts)
        mapped = self._som_model.map_vects(zY)
        return mapped

    def predict(
        self,
        datasource,
        from_date,
        to_date,
    ):
        from_ts = make_ts(from_date)
        to_ts = make_ts(to_date)

        # Fixup range to be sure that it is a multiple of interval
        from_ts = math.floor(from_ts / self.interval) * self.interval
        to_ts = math.ceil(to_ts / self.interval) * self.interval

        from_str = ts_to_str(from_ts)
        to_str = ts_to_str(to_ts)

        logging.info("predict(%s) range=[%s, %s]",
                     self.name, from_str, to_str)

        self.load()

        # Fill dataset
        features_dicts=[]
        for agg_num, agg in enumerate(self.aggs):
            data = datasource.get_quadrant_data(self, agg, from_ts, to_ts)
            features = dict()
            for key, val in data:
                features[key] = self.format_quadrants(val, agg)
            features_dicts.append(features)

        keys, dataset = self._make_dataset(features_dicts)

        if len(keys) == 0:
            raise errors.NoData("no data found for time range {}-{}".format(
                from_str,
                to_str,
            ))

        logging.info("found %d keys", len(keys))

        mapped = self._map_dataset(
            dataset,
            from_ts,
            to_ts,
        )

        fingerprints = self._build_fingerprints(
            dataset,
            mapped,
            keys,
            from_ts,
            to_ts,
        )

        return FingerprintsPrediction(
            from_ts=from_ts,
            to_ts=to_ts,
            fingerprints=fingerprints,
        )


    def predict_ranges(
        self,
        datasource,
        date_ranges,
        key_val=None,
    ):
        self.load()

        for from_date, to_date in date_ranges:
            from_ts = make_ts(from_date)
            to_ts = make_ts(to_date)
    
            # Fixup range to be sure that it is a multiple of interval
            from_ts = math.floor(from_ts / self.interval) * self.interval
            to_ts = math.ceil(to_ts / self.interval) * self.interval
    
            from_str = ts_to_str(from_ts)
            to_str = ts_to_str(to_ts)
    
            logging.info("predict(%s) range=[%s, %s]",
                         self.name, from_str, to_str)
    
            # Fill dataset
            features_dicts=[]
            for agg_num, agg in enumerate(self.aggs):
                data = datasource.get_quadrant_data(self, agg, from_ts, to_ts, key_val)
                data = {key:val for key, val in data}

                features = dict()
                for key, val in data.items():
                    features[key] = self.format_quadrants(val, agg)
                features_dicts.append(features)
    
            keys, dataset = self._make_dataset(features_dicts)
            if len(keys) == 0:
                logging.warning(errors.NoData("no data found for time range {}-{}".format(
                    from_str,
                    to_str,
                )))
                yield FingerprintsPrediction(
                    from_ts=from_ts,
                    to_ts=to_ts,
                    fingerprints=[],
                )
                continue
    
            logging.info("found %d keys", len(keys))
    
            mapped = self._map_dataset(
                dataset,
                from_ts,
                to_ts,
            )
    
            fingerprints = self._build_fingerprints(
                dataset,
                mapped,
                keys,
                from_ts,
                to_ts,
            )
    
            yield FingerprintsPrediction(
                from_ts=from_ts,
                to_ts=to_ts,
                fingerprints=fingerprints,
            )

    def predict_ranges_and_scores(
        self,
        datasource,
        date_ranges,
        key_val=None,
        cpu_count=os.cpu_count(),
    ):
        pool = Pool()
        for _date_ranges in chunks(date_ranges, size=cpu_count):
            local_ranges = list(_date_ranges)
            local_args = zip(repeat(self, len(local_ranges)), \
                             repeat(datasource, len(local_ranges)), \
                             repeat(key_val, len(local_ranges)), \
                             local_ranges)
            res = pool.map(predict_scores, local_args)
            for prediction in sorted(res, key=lambda x: x.from_ts):
                yield prediction

        pool.close()

    def show(self, show_summary=False):
        exn = self.load()
        if exn:
            raise(exn)

        som_model = self._som_model
        fingerprints = self._state['fingerprints']
        centroids = som_model.centroids()
        result = {
            'fingerprints': fingerprints
        }
        counts = np.zeros(shape=(self.h, self.w), dtype=int)
        for fingerprint in fingerprints:
            x, y = fingerprint['location']
            counts[x,y] += 1

        if show_summary == True:
            return '\n'.join([''.join(['{:3}'.format(cnt) for cnt in row]) for row in counts])

        grid = []
        for x in range(self.h):
            for y in range(self.w):
                cnt = counts[x,y]
                grid.append({
                    'location': (x, y),
                    'count': cnt,
                    '_fingerprint': centroids[som_model.location(x,y)].tolist(),
                })

        result['grid'] = grid
        return result

    def detect_anomalies(self, prediction):
        """
        Detect anomalies on observed data by comparing them to the values
        predicted by the model
        """

        self.load()

        fps = {
            fingerprint['key']: fingerprint
            for fingerprint in self._state['fingerprints']
        }

        prediction.changed = []
        prediction.anomalies = []

        low_highs = [feature.anomaly_type for feature in self.features]

        for fp_pred in prediction.fingerprints:
            key = fp_pred['key']
            fp = fps.get(key)

            if fp is None:
                # signature = initial. We haven't seen this key during training
                prediction.changed.append(key)
                fp_pred['stats'] = {
                    'scores': [],
                    'score': 0.0,
                    'anomaly': False,
                }
                continue

            scores = self._som_model.get_scores(
                fp['location'],
                fp_pred['location'],
                low_highs,
            )
            logging.info("scores for {} = {}".format(key, scores))
            max_arg = np.nanargmax(scores)
            score = scores[max_arg]

            stats = {
                'scores': scores.tolist(),
                'score': score.item(),
            }

            if score >= self.max_threshold:
                # TODO have a Model.logger to prefix all logs with model name
                logging.warning("detected anomaly for %s (score = %.1f)",
                                key, score)
                prediction.anomalies.append(key)
                stats['anomaly'] = True
            else:
                stats['anomaly'] = False

            fp_pred['stats'] = stats
