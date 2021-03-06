# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import errno
import mock
import operator
import os
import unittest
import six.moves.cPickle as pickle
from array import array
from collections import defaultdict
from math import ceil
from tempfile import mkdtemp
from shutil import rmtree
import warnings

from six.moves import range

from swift.common import exceptions
from swift.common import ring
from swift.common.ring.builder import MAX_BALANCE, RingValidationWarning


class TestRingBuilder(unittest.TestCase):

    def setUp(self):
        self.testdir = mkdtemp()

    def tearDown(self):
        rmtree(self.testdir, ignore_errors=1)

    def _partition_counts(self, builder, key='id'):
        """
        Returns a dictionary mapping the given device key to (number of
        partitions assigned to to that key).
        """
        counts = defaultdict(int)
        for part2dev_id in builder._replica2part2dev:
            for dev_id in part2dev_id:
                counts[builder.devs[dev_id][key]] += 1
        return counts

    def _get_population_by_region(self, builder):
        """
        Returns a dictionary mapping region to number of partitions in that
        region.
        """
        return self._partition_counts(builder, key='region')

    def test_init(self):
        rb = ring.RingBuilder(8, 3, 1)
        self.assertEquals(rb.part_power, 8)
        self.assertEquals(rb.replicas, 3)
        self.assertEquals(rb.min_part_hours, 1)
        self.assertEquals(rb.parts, 2 ** 8)
        self.assertEquals(rb.devs, [])
        self.assertEquals(rb.devs_changed, False)
        self.assertEquals(rb.version, 0)

    def test_overlarge_part_powers(self):
        ring.RingBuilder(32, 3, 1)  # passes by not crashing
        self.assertRaises(ValueError, ring.RingBuilder, 33, 3, 1)

    def test_insufficient_replicas(self):
        ring.RingBuilder(8, 1.0, 1)  # passes by not crashing
        self.assertRaises(ValueError, ring.RingBuilder, 8, 0.999, 1)

    def test_negative_min_part_hours(self):
        ring.RingBuilder(8, 3, 0)  # passes by not crashing
        self.assertRaises(ValueError, ring.RingBuilder, 8, 3, -1)

    def test_deepcopy(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})

        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb1'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sdb1'})

        # more devices in zone #1
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sdc1'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sdd1'})
        rb.rebalance()
        rb_copy = copy.deepcopy(rb)

        self.assertEqual(rb.to_dict(), rb_copy.to_dict())
        self.assertTrue(rb.devs is not rb_copy.devs)
        self.assertTrue(rb._replica2part2dev is not rb_copy._replica2part2dev)
        self.assertTrue(rb._last_part_moves is not rb_copy._last_part_moves)
        self.assertTrue(rb._remove_devs is not rb_copy._remove_devs)
        self.assertTrue(rb._dispersion_graph is not rb_copy._dispersion_graph)

    def test_get_ring(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.remove_dev(1)
        rb.rebalance()
        r = rb.get_ring()
        self.assertTrue(isinstance(r, ring.RingData))
        r2 = rb.get_ring()
        self.assertTrue(r is r2)
        rb.rebalance()
        r3 = rb.get_ring()
        self.assertTrue(r3 is not r2)
        r4 = rb.get_ring()
        self.assertTrue(r3 is r4)

    def test_rebalance_with_seed(self):
        devs = [(0, 10000), (1, 10001), (2, 10002), (1, 10003)]
        ring_builders = []
        for n in range(3):
            rb = ring.RingBuilder(8, 3, 1)
            idx = 0
            for zone, port in devs:
                for d in ('sda1', 'sdb1'):
                    rb.add_dev({'id': idx, 'region': 0, 'zone': zone,
                                'ip': '127.0.0.1', 'port': port,
                                'device': d, 'weight': 1})
                    idx += 1
            ring_builders.append(rb)

        rb0 = ring_builders[0]
        rb1 = ring_builders[1]
        rb2 = ring_builders[2]

        r0 = rb0.get_ring()
        self.assertTrue(rb0.get_ring() is r0)

        rb0.rebalance()  # NO SEED
        rb1.rebalance(seed=10)
        rb2.rebalance(seed=10)

        r1 = rb1.get_ring()
        r2 = rb2.get_ring()

        self.assertFalse(rb0.get_ring() is r0)
        self.assertNotEquals(r0.to_dict(), r1.to_dict())
        self.assertEquals(r1.to_dict(), r2.to_dict())

    def test_rebalance_part_on_deleted_other_part_on_drained(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10005, 'device': 'sda1'})

        rb.rebalance(seed=1)
        # We want a partition where 1 replica is on a removed device, 1
        # replica is on a 0-weight device, and 1 on a normal device. To
        # guarantee we have one, we see where partition 123 is, then
        # manipulate its devices accordingly.
        zero_weight_dev_id = rb._replica2part2dev[1][123]
        delete_dev_id = rb._replica2part2dev[2][123]

        rb.set_dev_weight(zero_weight_dev_id, 0.0)
        rb.remove_dev(delete_dev_id)
        rb.rebalance()

    def test_set_replicas(self):
        rb = ring.RingBuilder(8, 3.2, 1)
        rb.devs_changed = False
        rb.set_replicas(3.25)
        self.assertTrue(rb.devs_changed)

        rb.devs_changed = False
        rb.set_replicas(3.2500001)
        self.assertFalse(rb.devs_changed)

    def test_add_dev(self):
        rb = ring.RingBuilder(8, 3, 1)
        dev = {'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
               'ip': '127.0.0.1', 'port': 10000}
        dev_id = rb.add_dev(dev)
        self.assertRaises(exceptions.DuplicateDeviceError, rb.add_dev, dev)
        self.assertEqual(dev_id, 0)
        rb = ring.RingBuilder(8, 3, 1)
        # test add new dev with no id
        dev_id = rb.add_dev({'zone': 0, 'region': 1, 'weight': 1,
                             'ip': '127.0.0.1', 'port': 6000})
        self.assertEquals(rb.devs[0]['id'], 0)
        self.assertEqual(dev_id, 0)
        # test add another dev with no id
        dev_id = rb.add_dev({'zone': 3, 'region': 2, 'weight': 1,
                             'ip': '127.0.0.1', 'port': 6000})
        self.assertEquals(rb.devs[1]['id'], 1)
        self.assertEqual(dev_id, 1)

    def test_set_dev_weight(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 128, 1: 128, 2: 256, 3: 256})
        rb.set_dev_weight(0, 0.75)
        rb.set_dev_weight(1, 0.25)
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 192, 1: 64, 2: 256, 3: 256})

    def test_remove_dev(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 192, 1: 192, 2: 192, 3: 192})
        rb.remove_dev(1)
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 256, 2: 256, 3: 256})

    def test_remove_a_lot(self):
        rb = ring.RingBuilder(3, 3, 1)
        rb.add_dev({'id': 0, 'device': 'd0', 'ip': '10.0.0.1',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 1})
        rb.add_dev({'id': 1, 'device': 'd1', 'ip': '10.0.0.2',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 2})
        rb.add_dev({'id': 2, 'device': 'd2', 'ip': '10.0.0.3',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 3})
        rb.add_dev({'id': 3, 'device': 'd3', 'ip': '10.0.0.1',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 1})
        rb.add_dev({'id': 4, 'device': 'd4', 'ip': '10.0.0.2',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 2})
        rb.add_dev({'id': 5, 'device': 'd5', 'ip': '10.0.0.3',
                    'port': 6002, 'weight': 1000.0, 'region': 0, 'zone': 3})
        rb.rebalance()
        rb.validate()

        # this has to put more than 1/3 of the partitions in the
        # cluster on removed devices in order to ensure that at least
        # one partition has multiple replicas that need to move.
        #
        # (for an N-replica ring, it's more than 1/N of the
        # partitions, of course)
        rb.remove_dev(3)
        rb.remove_dev(4)
        rb.remove_dev(5)

        rb.rebalance()
        rb.validate()

    def test_shuffled_gather(self):
        if self._shuffled_gather_helper() and \
                self._shuffled_gather_helper():
                raise AssertionError('It is highly likely the ring is no '
                                     'longer shuffling the set of partitions '
                                     'to reassign on a rebalance.')

    def _shuffled_gather_helper(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.rebalance()
        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.pretend_min_part_hours_passed()
        parts = rb._gather_reassign_parts()
        max_run = 0
        run = 0
        last_part = 0
        for part, _ in parts:
            if part > last_part:
                run += 1
            else:
                if run > max_run:
                    max_run = run
                run = 0
            last_part = part
        if run > max_run:
            max_run = run
        return max_run > len(parts) / 2

    def test_initial_balance(self):
        # 2 boxes, 2 drives each in zone 1
        # 1 box, 2 drives in zone 2
        #
        # This is balanceable, but there used to be some nondeterminism in
        # rebalance() that would sometimes give you an imbalanced ring.
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'region': 1, 'zone': 1, 'weight': 4000.0,
                    'ip': '10.1.1.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'region': 1, 'zone': 1, 'weight': 4000.0,
                    'ip': '10.1.1.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'region': 1, 'zone': 1, 'weight': 4000.0,
                    'ip': '10.1.1.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'region': 1, 'zone': 1, 'weight': 4000.0,
                    'ip': '10.1.1.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'region': 1, 'zone': 2, 'weight': 4000.0,
                    'ip': '10.1.1.3', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'region': 1, 'zone': 2, 'weight': 4000.0,
                    'ip': '10.1.1.3', 'port': 10000, 'device': 'sdb'})

        _, balance = rb.rebalance(seed=2)

        # maybe not *perfect*, but should be close
        self.assertTrue(balance <= 1)

    def test_multitier_partial(self):
        # Multitier test, nothing full
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 2, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 3, 'zone': 3, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})

        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            counts = defaultdict(lambda: defaultdict(int))
            for replica in range(rb.replicas):
                dev = rb.devs[rb._replica2part2dev[replica][part]]
                counts['region'][dev['region']] += 1
                counts['zone'][dev['zone']] += 1

            if any(c > 1 for c in counts['region'].values()):
                raise AssertionError(
                    "Partition %d not evenly region-distributed (got %r)" %
                    (part, counts['region']))
            if any(c > 1 for c in counts['zone'].values()):
                raise AssertionError(
                    "Partition %d not evenly zone-distributed (got %r)" %
                    (part, counts['zone']))

        # Multitier test, zones full, nodes not full
        rb = ring.RingBuilder(8, 6, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})

        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdd'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdf'})

        rb.add_dev({'id': 6, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sdg'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sdh'})
        rb.add_dev({'id': 8, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sdi'})

        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            counts = defaultdict(lambda: defaultdict(int))
            for replica in range(rb.replicas):
                dev = rb.devs[rb._replica2part2dev[replica][part]]
                counts['zone'][dev['zone']] += 1
                counts['dev_id'][dev['id']] += 1
            if counts['zone'] != {0: 2, 1: 2, 2: 2}:
                raise AssertionError(
                    "Partition %d not evenly distributed (got %r)" %
                    (part, counts['zone']))
            for dev_id, replica_count in counts['dev_id'].items():
                if replica_count > 1:
                    raise AssertionError(
                        "Partition %d is on device %d more than once (%r)" %
                        (part, dev_id, counts['dev_id']))

    def test_multitier_full(self):
        # Multitier test, #replicas == #devs
        rb = ring.RingBuilder(8, 6, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdd'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdf'})

        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            counts = defaultdict(lambda: defaultdict(int))
            for replica in range(rb.replicas):
                dev = rb.devs[rb._replica2part2dev[replica][part]]
                counts['zone'][dev['zone']] += 1
                counts['dev_id'][dev['id']] += 1
            if counts['zone'] != {0: 2, 1: 2, 2: 2}:
                raise AssertionError(
                    "Partition %d not evenly distributed (got %r)" %
                    (part, counts['zone']))
            for dev_id, replica_count in counts['dev_id'].items():
                if replica_count != 1:
                    raise AssertionError(
                        "Partition %d is on device %d %d times, not 1 (%r)" %
                        (part, dev_id, replica_count, counts['dev_id']))

    def test_multitier_overfull(self):
        # Multitier test, #replicas > #zones (to prove even distribution)
        rb = ring.RingBuilder(8, 8, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdg'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdd'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdh'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdf'})
        rb.add_dev({'id': 8, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdi'})

        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            counts = defaultdict(lambda: defaultdict(int))
            for replica in range(rb.replicas):
                dev = rb.devs[rb._replica2part2dev[replica][part]]
                counts['zone'][dev['zone']] += 1
                counts['dev_id'][dev['id']] += 1

            self.assertEquals(8, sum(counts['zone'].values()))
            for zone, replica_count in counts['zone'].items():
                if replica_count not in (2, 3):
                    raise AssertionError(
                        "Partition %d not evenly distributed (got %r)" %
                        (part, counts['zone']))
            for dev_id, replica_count in counts['dev_id'].items():
                if replica_count not in (1, 2):
                    raise AssertionError(
                        "Partition %d is on device %d %d times, "
                        "not 1 or 2 (%r)" %
                        (part, dev_id, replica_count, counts['dev_id']))

    def test_multitier_expansion_more_devices(self):
        rb = ring.RingBuilder(8, 6, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 8, 'region': 0, 'zone': 2, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})

        rb.rebalance()
        rb.validate()

        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdf'})
        rb.add_dev({'id': 9, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.add_dev({'id': 10, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde'})
        rb.add_dev({'id': 11, 'region': 0, 'zone': 2, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdf'})

        for _ in range(5):
            rb.pretend_min_part_hours_passed()
            rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            counts = dict(zone=defaultdict(int),
                          dev_id=defaultdict(int))
            for replica in range(rb.replicas):
                dev = rb.devs[rb._replica2part2dev[replica][part]]
                counts['zone'][dev['zone']] += 1
                counts['dev_id'][dev['id']] += 1

            self.assertEquals({0: 2, 1: 2, 2: 2}, dict(counts['zone']))
            # each part is assigned once to six unique devices
            self.assertEqual((counts['dev_id'].values()), [1] * 6)
            self.assertEqual(len(set(counts['dev_id'].keys())), 6)

    def test_multitier_part_moves_with_0_min_part_hours(self):
        rb = ring.RingBuilder(8, 3, 0)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde1'})
        rb.rebalance()
        rb.validate()

        # min_part_hours is 0, so we're clear to move 2 replicas to
        # new devs
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc1'})
        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            devs = set()
            for replica in range(rb.replicas):
                devs.add(rb._replica2part2dev[replica][part])

            if len(devs) != 3:
                raise AssertionError(
                    "Partition %d not on 3 devs (got %r)" % (part, devs))

    def test_multitier_part_moves_with_positive_min_part_hours(self):
        rb = ring.RingBuilder(8, 3, 99)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde1'})
        rb.rebalance()
        rb.validate()

        # min_part_hours is >0, so we'll only be able to move 1
        # replica to a new home
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc1'})
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            devs = set()
            for replica in range(rb.replicas):
                devs.add(rb._replica2part2dev[replica][part])
            if not any(rb.devs[dev_id]['zone'] == 1 for dev_id in devs):
                raise AssertionError(
                    "Partition %d did not move (got %r)" % (part, devs))

    def test_multitier_dont_move_too_many_replicas(self):
        rb = ring.RingBuilder(8, 3, 0)
        # there'll be at least one replica in z0 and z1
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})
        rb.rebalance()
        rb.validate()

        # only 1 replica should move
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 4, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdf1'})
        rb.rebalance()
        rb.validate()

        for part in range(rb.parts):
            zones = set()
            for replica in range(rb.replicas):
                zones.add(rb.devs[rb._replica2part2dev[replica][part]]['zone'])

            if len(zones) != 3:
                raise AssertionError(
                    "Partition %d not in 3 zones (got %r)" % (part, zones))
            if 0 not in zones or 1 not in zones:
                raise AssertionError(
                    "Partition %d not in zones 0 and 1 (got %r)" %
                    (part, zones))

    def test_rerebalance(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 256, 1: 256, 2: 256})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 192, 1: 192, 2: 192, 3: 192})
        rb.set_dev_weight(3, 100)
        rb.rebalance()
        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts[3], 256)

    def test_add_rebalance_add_rebalance_delete_rebalance(self):
        # Test for https://bugs.launchpad.net/swift/+bug/845952
        # min_part of 0 to allow for rapid rebalancing
        rb = ring.RingBuilder(8, 3, 0)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})

        rb.rebalance()
        rb.validate()

        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10005, 'device': 'sda1'})

        rb.rebalance()
        rb.validate()

        rb.remove_dev(1)

        # well now we have only one device in z0
        rb.set_overload(0.5)

        rb.rebalance()
        rb.validate()

    def test_remove_last_partition_from_zero_weight(self):
        rb = ring.RingBuilder(4, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 1, 'weight': 1.0,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})

        rb.add_dev({'id': 1, 'region': 0, 'zone': 2, 'weight': 1.0,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 1.0,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 3, 'weight': 1.0,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 3, 'weight': 1.0,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 3, 'weight': 1.0,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdc'})

        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 0.5,
                    'ip': '127.0.0.3', 'port': 10001, 'device': 'zero'})

        zero_weight_dev = 3

        rb.rebalance()

        # We want at least one partition with replicas only in zone 2 and 3
        # due to device weights. It would *like* to spread out into zone 1,
        # but can't, due to device weight.
        #
        # Also, we want such a partition to have a replica on device 3,
        # which we will then reduce to zero weight. This should cause the
        # removal of the replica from device 3.
        #
        # Getting this to happen by chance is hard, so let's just set up a
        # builder so that it's in the state we want. This is a synthetic
        # example; while the bug has happened on a real cluster, that
        # builder file had a part_power of 16, so its contents are much too
        # big to include here.
        rb._replica2part2dev = [
            #                            these are the relevant ones
            #                                   |  |  |
            #                                   v  v  v
            array('H', [2, 5, 6, 2, 5, 6, 2, 5, 6, 2, 5, 6, 2, 5, 6, 2]),
            array('H', [1, 4, 1, 4, 1, 4, 1, 4, 1, 4, 1, 4, 1, 4, 1, 4]),
            array('H', [0, 0, 0, 0, 0, 0, 0, 0, 3, 3, 3, 5, 6, 2, 5, 6])]

        rb.set_dev_weight(zero_weight_dev, 0.0)
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=1)

        node_counts = defaultdict(int)
        for part2dev_id in rb._replica2part2dev:
            for dev_id in part2dev_id:
                node_counts[dev_id] += 1
        self.assertEqual(node_counts[zero_weight_dev], 0)

        # it's as balanced as it gets, so nothing moves anymore
        rb.pretend_min_part_hours_passed()
        parts_moved, _balance = rb.rebalance(seed=1)
        self.assertEqual(parts_moved, 0)

    def test_region_fullness_with_balanceable_ring(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})

        rb.add_dev({'id': 2, 'region': 1, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})

        rb.add_dev({'id': 4, 'region': 2, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10005, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 2, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10006, 'device': 'sda1'})

        rb.add_dev({'id': 6, 'region': 3, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10007, 'device': 'sda1'})
        rb.add_dev({'id': 7, 'region': 3, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10008, 'device': 'sda1'})
        rb.rebalance(seed=2)

        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region,
                          {0: 192, 1: 192, 2: 192, 3: 192})

    def test_region_fullness_with_unbalanceable_ring(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})

        rb.add_dev({'id': 2, 'region': 1, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 1, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.rebalance(seed=2)

        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region, {0: 512, 1: 256})

    def test_adding_region_slowly_with_unbalanceable_ring(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc1'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 0, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd1'})

        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb1'})
        rb.add_dev({'id': 8, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdc1'})
        rb.add_dev({'id': 9, 'region': 0, 'zone': 1, 'weight': 0.5,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdd1'})
        rb.rebalance(seed=2)

        rb.add_dev({'id': 2, 'region': 1, 'zone': 0, 'weight': 0.25,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 1, 'zone': 1, 'weight': 0.25,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.pretend_min_part_hours_passed()
        changed_parts, _balance = rb.rebalance(seed=2)

        # there's not enough room in r1 for every partition to have a replica
        # in it, so only 86 assignments occur in r1 (that's ~1/5 of the total,
        # since r1 has 1/5 of the weight).
        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region, {0: 682, 1: 86})

        # only 86 parts *should* move (to the new region) but randomly some
        # parts will flop around devices in the original region too
        self.assertEqual(90, changed_parts)

        # and since there's not enough room, subsequent rebalances will not
        # cause additional assignments to r1
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=2)
        rb.validate()
        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region, {0: 682, 1: 86})

        # after you add more weight, more partition assignments move
        rb.set_dev_weight(2, 0.5)
        rb.set_dev_weight(3, 0.5)
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=2)
        rb.validate()
        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region, {0: 614, 1: 154})

        rb.set_dev_weight(2, 1.0)
        rb.set_dev_weight(3, 1.0)
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=2)
        rb.validate()
        population_by_region = self._get_population_by_region(rb)
        self.assertEquals(population_by_region, {0: 512, 1: 256})

    def test_avoid_tier_change_new_region(self):
        rb = ring.RingBuilder(8, 3, 1)
        for i in range(5):
            rb.add_dev({'id': i, 'region': 0, 'zone': 0, 'weight': 100,
                        'ip': '127.0.0.1', 'port': i, 'device': 'sda1'})
        rb.rebalance(seed=2)

        # Add a new device in new region to a balanced ring
        rb.add_dev({'id': 5, 'region': 1, 'zone': 0, 'weight': 0,
                    'ip': '127.0.0.5', 'port': 10000, 'device': 'sda1'})

        # Increase the weight of region 1 slowly
        moved_partitions = []
        for weight in range(0, 101, 10):
            rb.set_dev_weight(5, weight)
            rb.pretend_min_part_hours_passed()
            changed_parts, _balance = rb.rebalance(seed=2)
            rb.validate()
            moved_partitions.append(changed_parts)
            # Ensure that the second region has enough partitions
            # Otherwise there will be replicas at risk
            min_parts_for_r1 = ceil(weight / (500.0 + weight) * 768)
            parts_for_r1 = self._get_population_by_region(rb).get(1, 0)
            self.assertEqual(min_parts_for_r1, parts_for_r1)

        # Number of partitions moved on each rebalance
        # 10/510 * 768 ~ 15.06 -> move at least 15 partitions in first step
        ref = [0, 17, 16, 17, 13, 15, 13, 12, 11, 13, 13]
        self.assertEqual(ref, moved_partitions)

    def test_set_replicas_increase(self):
        rb = ring.RingBuilder(8, 2, 0)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.rebalance()
        rb.validate()

        rb.replicas = 2.1
        rb.rebalance()
        rb.validate()

        self.assertEqual([len(p2d) for p2d in rb._replica2part2dev],
                         [256, 256, 25])

        rb.replicas = 2.2
        rb.rebalance()
        rb.validate()
        self.assertEqual([len(p2d) for p2d in rb._replica2part2dev],
                         [256, 256, 51])

    def test_set_replicas_decrease(self):
        rb = ring.RingBuilder(4, 5, 0)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.rebalance()
        rb.validate()

        rb.replicas = 4.9
        rb.rebalance()
        rb.validate()

        self.assertEqual([len(p2d) for p2d in rb._replica2part2dev],
                         [16, 16, 16, 16, 14])

        # cross a couple of integer thresholds (4 and 3)
        rb.replicas = 2.5
        rb.rebalance()
        rb.validate()

        self.assertEqual([len(p2d) for p2d in rb._replica2part2dev],
                         [16, 16, 8])

    def test_fractional_replicas_rebalance(self):
        rb = ring.RingBuilder(8, 2.5, 0)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.rebalance()  # passes by not crashing
        rb.validate()   # also passes by not crashing
        self.assertEqual([len(p2d) for p2d in rb._replica2part2dev],
                         [256, 256, 128])

    def test_create_add_dev_add_replica_rebalance(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.set_replicas(4)
        rb.rebalance()  # this would crash since parts_wanted was not set
        rb.validate()

    def test_rebalance_post_upgrade(self):
        rb = ring.RingBuilder(8, 3, 1)
        # 5 devices: 5 is the smallest number that does not divide 3 * 2^8,
        # which forces some rounding to happen.
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde'})
        rb.rebalance()
        rb.validate()

        # Older versions of the ring builder code would round down when
        # computing parts_wanted, while the new code rounds up. Make sure we
        # can handle a ring built by the old method.
        #
        # This code mimics the old _set_parts_wanted.
        weight_of_one_part = rb.weight_of_one_part()
        for dev in rb._iter_devs():
            if not dev['weight']:
                dev['parts_wanted'] = -rb.parts * rb.replicas
            else:
                dev['parts_wanted'] = (
                    int(weight_of_one_part * dev['weight']) -
                    dev['parts'])

        rb.pretend_min_part_hours_passed()
        rb.rebalance()  # this crashes unless rebalance resets parts_wanted
        rb.validate()

    def test_add_replicas_then_rebalance_respects_weight(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})

        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 1, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 1, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdf'})
        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdg'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdh'})

        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 2, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdi'})
        rb.add_dev({'id': 9, 'region': 0, 'region': 0, 'zone': 2, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdj'})
        rb.add_dev({'id': 10, 'region': 0, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdk'})
        rb.add_dev({'id': 11, 'region': 0, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdl'})

        rb.rebalance(seed=1)

        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 96, 1: 96,
                                   2: 32, 3: 32,
                                   4: 96, 5: 96,
                                   6: 32, 7: 32,
                                   8: 96, 9: 96,
                                   10: 32, 11: 32})

        rb.replicas *= 2
        rb.rebalance(seed=1)

        r = rb.get_ring()
        counts = {}
        for part2dev_id in r._replica2part2dev_id:
            for dev_id in part2dev_id:
                counts[dev_id] = counts.get(dev_id, 0) + 1
        self.assertEquals(counts, {0: 192, 1: 192,
                                   2: 64, 3: 64,
                                   4: 192, 5: 192,
                                   6: 64, 7: 64,
                                   8: 192, 9: 192,
                                   10: 64, 11: 64})

    def test_overload(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sde'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdf'})

        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb'})
        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdg'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdh'})
        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdi'})

        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.2', 'port': 10002, 'device': 'sdc'})
        rb.add_dev({'id': 9, 'region': 0, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.2', 'port': 10002, 'device': 'sdj'})
        rb.add_dev({'id': 10, 'region': 0, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.2', 'port': 10002, 'device': 'sdk'})
        rb.add_dev({'id': 11, 'region': 0, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.2', 'port': 10002, 'device': 'sdl'})

        rb.rebalance(seed=12345)
        rb.validate()

        # sanity check: balance respects weights, so default
        part_counts = self._partition_counts(rb, key='zone')
        self.assertEqual(part_counts[0], 192)
        self.assertEqual(part_counts[1], 192)
        self.assertEqual(part_counts[2], 384)

        # Devices 0 and 1 take 10% more than their fair shares by weight since
        # overload is 10% (0.1).
        rb.set_overload(0.1)
        for _ in range(2):
            rb.pretend_min_part_hours_passed()
            rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='zone')
        self.assertEqual(part_counts[0], 212)
        self.assertEqual(part_counts[1], 212)
        self.assertEqual(part_counts[2], 344)

        # Now, devices 0 and 1 take 50% more than their fair shares by
        # weight.
        rb.set_overload(0.5)
        for _ in range(3):
            rb.pretend_min_part_hours_passed()
            rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='zone')
        self.assertEqual(part_counts[0], 256)
        self.assertEqual(part_counts[1], 256)
        self.assertEqual(part_counts[2], 256)

        # Devices 0 and 1 may take up to 75% over their fair share, but the
        # placement algorithm only wants to spread things out evenly between
        # all drives, so the devices stay at 50% more.
        rb.set_overload(0.75)
        for _ in range(3):
            rb.pretend_min_part_hours_passed()
            rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='zone')
        self.assertEqual(part_counts[0], 256)
        self.assertEqual(part_counts[1], 256)
        self.assertEqual(part_counts[2], 256)

    def test_unoverload(self):
        # Start off needing overload to balance, then add capacity until we
        # don't need overload any more and see that things still balance.
        # Overload doesn't prevent optimal balancing.
        rb = ring.RingBuilder(8, 3, 1)
        rb.set_overload(0.125)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})

        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 2,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 9, 'region': 0, 'region': 0, 'zone': 0, 'weight': 2,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 10, 'region': 0, 'region': 0, 'zone': 0, 'weight': 2,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 11, 'region': 0, 'region': 0, 'zone': 0, 'weight': 2,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdc'})
        rb.rebalance(seed=12345)

        # sanity check: our overload is big enough to balance things
        part_counts = self._partition_counts(rb, key='ip')
        self.assertEqual(part_counts['127.0.0.1'], 216)
        self.assertEqual(part_counts['127.0.0.2'], 216)
        self.assertEqual(part_counts['127.0.0.3'], 336)

        # Add some weight: balance improves
        for dev in rb.devs:
            if dev['ip'] in ('127.0.0.1', '127.0.0.2'):
                rb.set_dev_weight(dev['id'], 1.5)
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='ip')
        self.assertEqual(part_counts['127.0.0.1'], 236)
        self.assertEqual(part_counts['127.0.0.2'], 236)
        self.assertEqual(part_counts['127.0.0.3'], 296)

        # Even out the weights: balance becomes perfect
        for dev in rb.devs:
            if dev['ip'] in ('127.0.0.1', '127.0.0.2'):
                rb.set_dev_weight(dev['id'], 2)

        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='ip')
        self.assertEqual(part_counts['127.0.0.1'], 256)
        self.assertEqual(part_counts['127.0.0.2'], 256)
        self.assertEqual(part_counts['127.0.0.3'], 256)

        # Add a new server: balance stays optimal
        rb.add_dev({'id': 12, 'region': 0, 'region': 0, 'zone': 0,
                    'weight': 2,
                    'ip': '127.0.0.4', 'port': 10000, 'device': 'sdd'})
        rb.add_dev({'id': 13, 'region': 0, 'region': 0, 'zone': 0,
                    'weight': 2,
                    'ip': '127.0.0.4', 'port': 10000, 'device': 'sde'})
        rb.add_dev({'id': 14, 'region': 0, 'region': 0, 'zone': 0,
                    'weight': 2,
                    'ip': '127.0.0.4', 'port': 10000, 'device': 'sdf'})
        rb.add_dev({'id': 15, 'region': 0, 'region': 0, 'zone': 0,
                    'weight': 2,
                    'ip': '127.0.0.4', 'port': 10000, 'device': 'sdf'})

        # we're moving more than 1/3 of the replicas but fewer than 2/3, so
        # we have to do this twice
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=12345)
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb, key='ip')
        self.assertEqual(part_counts['127.0.0.1'], 192)
        self.assertEqual(part_counts['127.0.0.2'], 192)
        self.assertEqual(part_counts['127.0.0.3'], 192)
        self.assertEqual(part_counts['127.0.0.4'], 192)

    def test_overload_keeps_balanceable_things_balanced_initially(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 8,
                    'ip': '10.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 8,
                    'ip': '10.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.3', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.3', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.4', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.4', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.5', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 9, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.5', 'port': 10000, 'device': 'sdb'})

        rb.set_overload(99999)
        rb.rebalance(seed=12345)

        part_counts = self._partition_counts(rb)
        self.assertEqual(part_counts, {
            0: 128,
            1: 128,
            2: 64,
            3: 64,
            4: 64,
            5: 64,
            6: 64,
            7: 64,
            8: 64,
            9: 64,
        })

    def test_overload_keeps_balanceable_things_balanced_on_rebalance(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 8,
                    'ip': '10.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 8,
                    'ip': '10.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.3', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.3', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.4', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.4', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.5', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 9, 'region': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '10.0.0.5', 'port': 10000, 'device': 'sdb'})

        rb.set_overload(99999)

        rb.rebalance(seed=123)
        part_counts = self._partition_counts(rb)
        self.assertEqual(part_counts, {
            0: 128,
            1: 128,
            2: 64,
            3: 64,
            4: 64,
            5: 64,
            6: 64,
            7: 64,
            8: 64,
            9: 64,
        })

        # swap weights between 10.0.0.1 and 10.0.0.2
        rb.set_dev_weight(0, 4)
        rb.set_dev_weight(1, 4)
        rb.set_dev_weight(2, 8)
        rb.set_dev_weight(1, 8)

        rb.rebalance(seed=456)
        part_counts = self._partition_counts(rb)
        self.assertEqual(part_counts, {
            0: 128,
            1: 128,
            2: 64,
            3: 64,
            4: 64,
            5: 64,
            6: 64,
            7: 64,
            8: 64,
            9: 64,
        })

    def test_server_per_port(self):
        # 3 servers, 3 disks each, with each disk on its own port
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.1', 'port': 10000, 'device': 'sdx'})
        rb.add_dev({'id': 1, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.1', 'port': 10001, 'device': 'sdy'})

        rb.add_dev({'id': 3, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.2', 'port': 10000, 'device': 'sdx'})
        rb.add_dev({'id': 4, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.2', 'port': 10001, 'device': 'sdy'})

        rb.add_dev({'id': 6, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.3', 'port': 10000, 'device': 'sdx'})
        rb.add_dev({'id': 7, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.3', 'port': 10001, 'device': 'sdy'})

        rb.rebalance(seed=1)

        rb.add_dev({'id': 2, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.1', 'port': 10002, 'device': 'sdz'})
        rb.add_dev({'id': 5, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.2', 'port': 10002, 'device': 'sdz'})
        rb.add_dev({'id': 8, 'region': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '10.0.0.3', 'port': 10002, 'device': 'sdz'})

        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=1)

        poorly_dispersed = []
        for part in range(rb.parts):
            on_nodes = set()
            for replica in range(rb.replicas):
                dev_id = rb._replica2part2dev[replica][part]
                on_nodes.add(rb.devs[dev_id]['ip'])
            if len(on_nodes) < rb.replicas:
                poorly_dispersed.append(part)
        self.assertEqual(poorly_dispersed, [])

    def test_load(self):
        rb = ring.RingBuilder(8, 3, 1)
        devs = [{'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                 'ip': '127.0.0.0', 'port': 10000, 'device': 'sda1',
                 'meta': 'meta0'},
                {'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                 'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb1',
                 'meta': 'meta1'},
                {'id': 2, 'region': 0, 'zone': 2, 'weight': 2,
                 'ip': '127.0.0.2', 'port': 10002, 'device': 'sdc1',
                 'meta': 'meta2'},
                {'id': 3, 'region': 0, 'zone': 3, 'weight': 2,
                 'ip': '127.0.0.3', 'port': 10003, 'device': 'sdd1'}]
        for d in devs:
            rb.add_dev(d)
        rb.rebalance()

        real_pickle = pickle.load
        fake_open = mock.mock_open()

        io_error_not_found = IOError()
        io_error_not_found.errno = errno.ENOENT

        io_error_no_perm = IOError()
        io_error_no_perm.errno = errno.EPERM

        io_error_generic = IOError()
        io_error_generic.errno = errno.EOPNOTSUPP
        try:
            # test a legit builder
            fake_pickle = mock.Mock(return_value=rb)
            pickle.load = fake_pickle
            builder = ring.RingBuilder.load('fake.builder', open=fake_open)
            self.assertEquals(fake_pickle.call_count, 1)
            fake_open.assert_has_calls([mock.call('fake.builder', 'rb')])
            self.assertEquals(builder, rb)
            fake_pickle.reset_mock()

            # test old style builder
            fake_pickle.return_value = rb.to_dict()
            pickle.load = fake_pickle
            builder = ring.RingBuilder.load('fake.builder', open=fake_open)
            fake_open.assert_has_calls([mock.call('fake.builder', 'rb')])
            self.assertEquals(builder.devs, rb.devs)
            fake_pickle.reset_mock()

            # test old devs but no meta
            no_meta_builder = rb
            for dev in no_meta_builder.devs:
                del(dev['meta'])
            fake_pickle.return_value = no_meta_builder
            pickle.load = fake_pickle
            builder = ring.RingBuilder.load('fake.builder', open=fake_open)
            fake_open.assert_has_calls([mock.call('fake.builder', 'rb')])
            self.assertEquals(builder.devs, rb.devs)

            # test an empty builder
            fake_pickle.side_effect = EOFError
            pickle.load = fake_pickle
            self.assertRaises(exceptions.UnPicklingError,
                              ring.RingBuilder.load, 'fake.builder',
                              open=fake_open)

            # test a corrupted builder
            fake_pickle.side_effect = pickle.UnpicklingError
            pickle.load = fake_pickle
            self.assertRaises(exceptions.UnPicklingError,
                              ring.RingBuilder.load, 'fake.builder',
                              open=fake_open)

            # test some error
            fake_pickle.side_effect = AttributeError
            pickle.load = fake_pickle
            self.assertRaises(exceptions.UnPicklingError,
                              ring.RingBuilder.load, 'fake.builder',
                              open=fake_open)
        finally:
            pickle.load = real_pickle

        # test non existent builder file
        fake_open.side_effect = io_error_not_found
        self.assertRaises(exceptions.FileNotFoundError,
                          ring.RingBuilder.load, 'fake.builder',
                          open=fake_open)

        # test non accessible builder file
        fake_open.side_effect = io_error_no_perm
        self.assertRaises(exceptions.PermissionError,
                          ring.RingBuilder.load, 'fake.builder',
                          open=fake_open)

        # test an error other then ENOENT and ENOPERM
        fake_open.side_effect = io_error_generic
        self.assertRaises(IOError,
                          ring.RingBuilder.load, 'fake.builder',
                          open=fake_open)

    def test_save_load(self):
        rb = ring.RingBuilder(8, 3, 1)
        devs = [{'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                 'ip': '127.0.0.0', 'port': 10000,
                 'replication_ip': '127.0.0.0', 'replication_port': 10000,
                 'device': 'sda1', 'meta': 'meta0'},
                {'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                 'ip': '127.0.0.1', 'port': 10001,
                 'replication_ip': '127.0.0.1', 'replication_port': 10001,
                 'device': 'sdb1', 'meta': 'meta1'},
                {'id': 2, 'region': 0, 'zone': 2, 'weight': 2,
                 'ip': '127.0.0.2', 'port': 10002,
                 'replication_ip': '127.0.0.2', 'replication_port': 10002,
                 'device': 'sdc1', 'meta': 'meta2'},
                {'id': 3, 'region': 0, 'zone': 3, 'weight': 2,
                 'ip': '127.0.0.3', 'port': 10003,
                 'replication_ip': '127.0.0.3', 'replication_port': 10003,
                 'device': 'sdd1', 'meta': ''}]
        rb.set_overload(3.14159)
        for d in devs:
            rb.add_dev(d)
        rb.rebalance()
        builder_file = os.path.join(self.testdir, 'test_save.builder')
        rb.save(builder_file)
        loaded_rb = ring.RingBuilder.load(builder_file)
        self.maxDiff = None
        self.assertEquals(loaded_rb.to_dict(), rb.to_dict())
        self.assertEquals(loaded_rb.overload, 3.14159)

    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch('swift.common.ring.builder.pickle.dump', autospec=True)
    def test_save(self, mock_pickle_dump, mock_open):
        mock_open.return_value = mock_fh = mock.MagicMock()
        rb = ring.RingBuilder(8, 3, 1)
        devs = [{'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                 'ip': '127.0.0.0', 'port': 10000, 'device': 'sda1',
                 'meta': 'meta0'},
                {'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                 'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb1',
                 'meta': 'meta1'},
                {'id': 2, 'region': 0, 'zone': 2, 'weight': 2,
                 'ip': '127.0.0.2', 'port': 10002, 'device': 'sdc1',
                 'meta': 'meta2'},
                {'id': 3, 'region': 0, 'zone': 3, 'weight': 2,
                 'ip': '127.0.0.3', 'port': 10003, 'device': 'sdd1'}]
        for d in devs:
            rb.add_dev(d)
        rb.rebalance()
        rb.save('some.builder')
        mock_open.assert_called_once_with('some.builder', 'wb')
        mock_pickle_dump.assert_called_once_with(rb.to_dict(),
                                                 mock_fh.__enter__(),
                                                 protocol=2)

    def test_search_devs(self):
        rb = ring.RingBuilder(8, 3, 1)
        devs = [{'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                 'ip': '127.0.0.0', 'port': 10000, 'device': 'sda1',
                 'meta': 'meta0'},
                {'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                 'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb1',
                 'meta': 'meta1'},
                {'id': 2, 'region': 1, 'zone': 2, 'weight': 2,
                 'ip': '127.0.0.2', 'port': 10002, 'device': 'sdc1',
                 'meta': 'meta2'},
                {'id': 3, 'region': 1, 'zone': 3, 'weight': 2,
                 'ip': '127.0.0.3', 'port': 10003, 'device': 'sdd1',
                 'meta': 'meta3'},
                {'id': 4, 'region': 2, 'zone': 4, 'weight': 1,
                 'ip': '127.0.0.4', 'port': 10004, 'device': 'sde1',
                 'meta': 'meta4', 'replication_ip': '127.0.0.10',
                 'replication_port': 20000},
                {'id': 5, 'region': 2, 'zone': 5, 'weight': 2,
                 'ip': '127.0.0.5', 'port': 10005, 'device': 'sdf1',
                 'meta': 'meta5', 'replication_ip': '127.0.0.11',
                 'replication_port': 20001},
                {'id': 6, 'region': 2, 'zone': 6, 'weight': 2,
                 'ip': '127.0.0.6', 'port': 10006, 'device': 'sdg1',
                 'meta': 'meta6', 'replication_ip': '127.0.0.12',
                 'replication_port': 20002}]
        for d in devs:
            rb.add_dev(d)
        rb.rebalance()
        res = rb.search_devs({'region': 0})
        self.assertEquals(res, [devs[0], devs[1]])
        res = rb.search_devs({'region': 1})
        self.assertEquals(res, [devs[2], devs[3]])
        res = rb.search_devs({'region': 1, 'zone': 2})
        self.assertEquals(res, [devs[2]])
        res = rb.search_devs({'id': 1})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'zone': 1})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'ip': '127.0.0.1'})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'ip': '127.0.0.1', 'port': 10001})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'port': 10001})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'replication_ip': '127.0.0.10'})
        self.assertEquals(res, [devs[4]])
        res = rb.search_devs({'replication_ip': '127.0.0.10',
                              'replication_port': 20000})
        self.assertEquals(res, [devs[4]])
        res = rb.search_devs({'replication_port': 20000})
        self.assertEquals(res, [devs[4]])
        res = rb.search_devs({'device': 'sdb1'})
        self.assertEquals(res, [devs[1]])
        res = rb.search_devs({'meta': 'meta1'})
        self.assertEquals(res, [devs[1]])

    def test_validate(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 6, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 8, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 9, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 10, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 11, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 12, 'region': 0, 'zone': 2, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10002, 'device': 'sda1'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 3, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 13, 'region': 0, 'zone': 3, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 14, 'region': 0, 'zone': 3, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})
        rb.add_dev({'id': 15, 'region': 0, 'zone': 3, 'weight': 2,
                    'ip': '127.0.0.1', 'port': 10003, 'device': 'sda1'})

        # Degenerate case: devices added but not rebalanced yet
        self.assertRaises(exceptions.RingValidationError, rb.validate)

        rb.rebalance()
        counts = self._partition_counts(rb, key='zone')
        self.assertEquals(counts, {0: 128, 1: 128, 2: 256, 3: 256})

        dev_usage, worst = rb.validate()
        self.assertTrue(dev_usage is None)
        self.assertTrue(worst is None)

        dev_usage, worst = rb.validate(stats=True)
        self.assertEquals(list(dev_usage), [32, 32, 64, 64,
                                            32, 32, 32,  # added zone0
                                            32, 32, 32,  # added zone1
                                            64, 64, 64,  # added zone2
                                            64, 64, 64,  # added zone3
                                            ])
        self.assertEquals(int(worst), 0)

        rb.set_dev_weight(2, 0)
        rb.rebalance()
        self.assertEquals(rb.validate(stats=True)[1], MAX_BALANCE)

        # Test not all partitions doubly accounted for
        rb.devs[1]['parts'] -= 1
        self.assertRaises(exceptions.RingValidationError, rb.validate)
        rb.devs[1]['parts'] += 1

        # Test non-numeric port
        rb.devs[1]['port'] = '10001'
        self.assertRaises(exceptions.RingValidationError, rb.validate)
        rb.devs[1]['port'] = 10001

        # Test partition on nonexistent device
        rb.pretend_min_part_hours_passed()
        orig_dev_id = rb._replica2part2dev[0][0]
        rb._replica2part2dev[0][0] = len(rb.devs)
        self.assertRaises(exceptions.RingValidationError, rb.validate)
        rb._replica2part2dev[0][0] = orig_dev_id

        # Tests that validate can handle 'holes' in .devs
        rb.remove_dev(2)
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        rb.validate(stats=True)

        # Test partition assigned to a hole
        if rb.devs[2]:
            rb.remove_dev(2)
        rb.pretend_min_part_hours_passed()
        orig_dev_id = rb._replica2part2dev[0][0]
        rb._replica2part2dev[0][0] = 2
        self.assertRaises(exceptions.RingValidationError, rb.validate)
        rb._replica2part2dev[0][0] = orig_dev_id

        # Validate that zero weight devices with no partitions don't count on
        # the 'worst' value.
        self.assertNotEquals(rb.validate(stats=True)[1], MAX_BALANCE)
        rb.add_dev({'id': 16, 'region': 0, 'zone': 0, 'weight': 0,
                    'ip': '127.0.0.1', 'port': 10004, 'device': 'sda1'})
        rb.pretend_min_part_hours_passed()
        rb.rebalance()
        self.assertNotEquals(rb.validate(stats=True)[1], MAX_BALANCE)

    def test_validate_partial_replica(self):
        rb = ring.RingBuilder(8, 2.5, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdc'})
        rb.rebalance()
        rb.validate()  # sanity
        self.assertEqual(len(rb._replica2part2dev[0]), 256)
        self.assertEqual(len(rb._replica2part2dev[1]), 256)
        self.assertEqual(len(rb._replica2part2dev[2]), 128)

        # now swap partial replica part maps
        rb._replica2part2dev[1], rb._replica2part2dev[2] = \
            rb._replica2part2dev[2], rb._replica2part2dev[1]
        self.assertRaises(exceptions.RingValidationError, rb.validate)

    def test_validate_duplicate_part_assignment(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sdc'})
        rb.rebalance()
        rb.validate()  # sanity
        # now double up a device assignment
        rb._replica2part2dev[1][200] = rb._replica2part2dev[2][200]

        class SubStringMatcher(object):
            def __init__(self, substr):
                self.substr = substr

            def __eq__(self, other):
                return self.substr in other

        with warnings.catch_warnings():
            # we're firing the warning twice in this test and resetwarnings
            # doesn't work - https://bugs.python.org/issue4180
            warnings.simplefilter('always')

            # by default things will work, but log a warning
            with mock.patch('sys.stderr') as mock_stderr:
                rb.validate()
            expected = SubStringMatcher(
                'RingValidationWarning: The partition 200 has been assigned '
                'to duplicate devices')
            # ... but the warning is written to stderr
            self.assertEqual(mock_stderr.method_calls,
                             [mock.call.write(expected)])
            # if you make warnings errors it blows up
            with warnings.catch_warnings():
                warnings.filterwarnings('error')
                self.assertRaises(RingValidationWarning, rb.validate)

    def test_get_part_devices(self):
        rb = ring.RingBuilder(8, 3, 1)
        self.assertEqual(rb.get_part_devices(0), [])

        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.rebalance()

        part_devs = sorted(rb.get_part_devices(0),
                           key=operator.itemgetter('id'))
        self.assertEqual(part_devs, [rb.devs[0], rb.devs[1], rb.devs[2]])

    def test_get_part_devices_partial_replicas(self):
        rb = ring.RingBuilder(8, 2.5, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda1'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10001, 'device': 'sda1'})
        rb.rebalance(seed=9)

        # note: partition 255 will only have 2 replicas
        part_devs = sorted(rb.get_part_devices(255),
                           key=operator.itemgetter('id'))
        self.assertEqual(part_devs, [rb.devs[0], rb.devs[1]])

    def test_dispersion_with_zero_weight_devices(self):
        rb = ring.RingBuilder(8, 3.0, 0)
        # add two devices to a single server in a single zone
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        # and a zero weight device
        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 0,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.rebalance()
        self.assertEqual(rb.dispersion, 0.0)
        self.assertEqual(rb._dispersion_graph, {
            (0,): [0, 0, 0, 256],
            (0, 0): [0, 0, 0, 256],
            (0, 0, '127.0.0.1'): [0, 0, 0, 256],
            (0, 0, '127.0.0.1', 0): [0, 256, 0, 0],
            (0, 0, '127.0.0.1', 1): [0, 256, 0, 0],
            (0, 0, '127.0.0.1', 2): [0, 256, 0, 0],
        })

    def test_dispersion_with_zero_weight_devices_with_parts(self):
        rb = ring.RingBuilder(8, 3.0, 1)
        # add four devices to a single server in a single zone
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})
        rb.rebalance(seed=1)
        self.assertEqual(rb.dispersion, 0.0)
        self.assertEqual(rb._dispersion_graph, {
            (0,): [0, 0, 0, 256],
            (0, 0): [0, 0, 0, 256],
            (0, 0, '127.0.0.1'): [0, 0, 0, 256],
            (0, 0, '127.0.0.1', 0): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 1): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 2): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 3): [64, 192, 0, 0],
        })
        # now mark a device 2 for decom
        rb.set_dev_weight(2, 0.0)
        # we'll rebalance but can't move any parts
        rb.rebalance(seed=1)
        # zero weight tier has one copy of 1/4 part-replica
        self.assertEqual(rb.dispersion, 75.0)
        self.assertEqual(rb._dispersion_graph, {
            (0,): [0, 0, 0, 256],
            (0, 0): [0, 0, 0, 256],
            (0, 0, '127.0.0.1'): [0, 0, 0, 256],
            (0, 0, '127.0.0.1', 0): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 1): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 2): [64, 192, 0, 0],
            (0, 0, '127.0.0.1', 3): [64, 192, 0, 0],
        })
        # unlock the stuck parts
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=3)
        self.assertEqual(rb.dispersion, 0.0)
        self.assertEqual(rb._dispersion_graph, {
            (0,): [0, 0, 0, 256],
            (0, 0): [0, 0, 0, 256],
            (0, 0, '127.0.0.1'): [0, 0, 0, 256],
            (0, 0, '127.0.0.1', 0): [0, 256, 0, 0],
            (0, 0, '127.0.0.1', 1): [0, 256, 0, 0],
            (0, 0, '127.0.0.1', 3): [0, 256, 0, 0],
        })

    def test_effective_overload(self):
        rb = ring.RingBuilder(8, 3, 1)
        # z0
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})
        # z1
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 4, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        # z2
        rb.add_dev({'id': 6, 'region': 0, 'zone': 2, 'weight': 100,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 2, 'weight': 100,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        # this ring requires overload
        required = rb.get_required_overload()
        self.assertGreater(required, 0.1)

        # and we'll use a little bit
        rb.set_overload(0.1)

        rb.rebalance(seed=7)
        rb.validate()

        # but with-out enough overload we're not dispersed
        self.assertGreater(rb.dispersion, 0)

        # add the other dev to z2
        rb.add_dev({'id': 8, 'region': 0, 'zone': 2, 'weight': 100,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdc'})
        # but also fail another device in the same!
        rb.remove_dev(6)

        # we still require overload
        required = rb.get_required_overload()
        self.assertGreater(required, 0.1)

        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=7)
        rb.validate()

        # ... and without enough we're full dispersed
        self.assertGreater(rb.dispersion, 0)

        # ok, let's fix z2's weight for real
        rb.add_dev({'id': 6, 'region': 0, 'zone': 2, 'weight': 100,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})

        # ... technically, we no longer require overload
        self.assertEqual(rb.get_required_overload(), 0.0)

        # so let's rebalance w/o resetting min_part_hours
        rb.rebalance(seed=7)
        rb.validate()

        # ok, we didn't quite disperse
        self.assertGreater(rb.dispersion, 0)

        # ... but let's unlock some parts
        rb.pretend_min_part_hours_passed()
        rb.rebalance(seed=7)
        rb.validate()

        # ... and that got it!
        self.assertEqual(rb.dispersion, 0)

    def strawman_test(self):
        """
        This test demonstrates a trivial failure of part-replica placement.

        If you turn warnings into errors this will fail.

        i.e.

            export PYTHONWARNINGS=error:::swift.common.ring.builder

        N.B. try not to get *too* hung up on doing something silly to make
        this particular case pass w/o warnings - it's trivial to write up a
        dozen more.
        """
        rb = ring.RingBuilder(8, 3, 1)
        # z0
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        # z1
        rb.add_dev({'id': 1, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        # z2
        rb.add_dev({'id': 2, 'region': 0, 'zone': 2, 'weight': 200,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})

        with warnings.catch_warnings(record=True) as w:
            rb.rebalance(seed=7)
            rb.validate()
        self.assertEqual(len(w), 65)


class TestGetRequiredOverload(unittest.TestCase):
    def assertApproximately(self, a, b, error=1e-6):
        self.assertTrue(abs(a - b) < error,
                        "%f and %f differ by more than %f" % (a, b, error))

    def test_none_needed(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})
        rb.add_dev({'id': 2, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 0, 'weight': 1,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})

        # 4 equal-weight devs and 3 replicas: this can be balanced without
        # resorting to overload at all
        self.assertApproximately(rb.get_required_overload(), 0)

        # 3 equal-weight devs and 3 replicas: this can also be balanced
        rb.remove_dev(3)
        self.assertApproximately(rb.get_required_overload(), 0)

    def test_small_zone(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 4,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 4,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 4,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 4,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdc'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 3,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdd'})

        # Zone 2 has 7/8 of the capacity of the other two zones, so an
        # overload of 1/7 will allow things to balance out.
        self.assertApproximately(rb.get_required_overload(), 1.0 / 7)

    def test_big_zone(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 60,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 60,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 6, 'region': 0, 'zone': 3, 'weight': 60,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 3, 'weight': 60,
                    'ip': '127.0.0.3', 'port': 10000, 'device': 'sdb'})

        # Zone 1 has weight 200, while zones 2, 3, and 4 together have only
        # 360. The small zones would need to go from 360 to 400 to balance
        # out zone 1, for an overload of 40/360 = 1/9.
        self.assertApproximately(rb.get_required_overload(), 1.0 / 9)

    def test_enormous_zone(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 1000,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 1000,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 60,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 60,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 6, 'region': 0, 'zone': 3, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 3, 'weight': 60,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        # Zone 1 has weight 2000, while zones 2, 3, and 4 together have only
        # 360. The small zones would need to go from 360 to 4000 to balance
        # out zone 1, for an overload of 3640/360.
        self.assertApproximately(rb.get_required_overload(), 3640.0 / 360)

    def test_two_big_two_small(self):
        rb = ring.RingBuilder(8, 3, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 100,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 4, 'region': 0, 'zone': 2, 'weight': 45,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 5, 'region': 0, 'zone': 2, 'weight': 45,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 6, 'region': 0, 'zone': 3, 'weight': 35,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 7, 'region': 0, 'zone': 3, 'weight': 35,
                    'ip': '127.0.0.2', 'port': 10000, 'device': 'sdb'})

        # Zones 1 and 2 each have weight 200, while zones 3 and 4 together
        # have only 160. The small zones would need to go from 160 to 200 to
        # balance out the big zones, for an overload of 40/160 = 1/4.
        self.assertApproximately(rb.get_required_overload(), 1.0 / 4)

    def test_multiple_replicas_each(self):
        rb = ring.RingBuilder(8, 7, 1)
        rb.add_dev({'id': 0, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 1, 'region': 0, 'zone': 0, 'weight': 100,
                    'ip': '127.0.0.0', 'port': 10000, 'device': 'sdb'})

        rb.add_dev({'id': 2, 'region': 0, 'zone': 1, 'weight': 70,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sda'})
        rb.add_dev({'id': 3, 'region': 0, 'zone': 1, 'weight': 70,
                    'ip': '127.0.0.1', 'port': 10000, 'device': 'sdb'})

        # Zone 0 has more than 4/7 of the weight, so we'll need to bring
        # zone 1 up to a total of 150 so it can take 3 replicas, so the
        # overload should be 10/140.
        self.assertApproximately(rb.get_required_overload(), 10.0 / 140)


if __name__ == '__main__':
    unittest.main()
