# Copyright 2016-2019 The Van Valen Lab at the California Institute of
# Technology (Caltech), with support from the Paul Allen Family Foundation,
# Google, & National Institutes of Health (NIH) under Grant U24CA224309-01.
# All rights reserved.
#
# Licensed under a modified Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.github.com/vanvalenlab/kiosk-redis-janitor/LICENSE
#
# The Work provided may be used for non-commercial academic purposes only.
# For any other use of the Work, including commercial use, please contact:
# vanvalenlab@gmail.com
#
# Neither the name of Caltech nor the names of its contributors may be used
# to endorse or promote products derived from this software without specific
# prior written permission.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Janitor Class"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import timeit
import datetime
import logging

import pytz
import dateutil.parser
import kubernetes.client


class RedisJanitor(object):
    """Clean up keys in the redis queue"""

    def __init__(self,
                 redis_client,
                 queue,
                 namespace='default',
                 backoff=3,
                 stale_time=600,  # 10 minutes
                 restart_failures=False,
                 failure_stale_seconds=60,
                 pod_refresh_interval=10,):
        self.redis_client = redis_client
        self.logger = logging.getLogger(str(self.__class__.__name__))
        self.backoff = backoff
        self.queue = str(queue).lower()
        self.namespace = namespace
        self.stale_time = int(stale_time)
        self.restart_failures = restart_failures
        self.failure_stale_seconds = failure_stale_seconds
        self.pod_refresh_interval = int(pod_refresh_interval)
        self.cleaning_queue = None  # update this in clean()

        # empty initializers, update them with _update_pods
        self.pods = {}
        self.pods_updated_at = None

        # attributes for managing pod state
        self.whitelisted_pods = ['zip-consumer']
        self.valid_pod_phases = {'Running', 'Pending'}

        self.total_repairs = 0
        self.processing_queue = 'processing-{}'.format(self.queue)

    def get_core_v1_client(self):
        """Returns Kubernetes API Client for CoreV1Api"""
        kubernetes.config.load_incluster_config()
        return kubernetes.client.CoreV1Api()

    def kill_pod(self, pod_name, namespace):
        # delete the pod
        t = timeit.default_timer()
        try:
            kube_client = self.get_core_v1_client()
            response = kube_client.delete_namespaced_pod(
                pod_name, namespace, grace_period_seconds=0)
        except kubernetes.client.rest.ApiException as err:
            self.logger.error('`delete_namespaced_pod` encountered %s: %s. '
                              'Failed to kill pod `%s.%s`',
                              type(err).__name__, err, namespace, pod_name)
            return False
        self.logger.debug('Killed pod `%s` in namespace `%s` in %s seconds.',
                          pod_name, namespace, timeit.default_timer() - t)
        return response

    def list_pod_for_all_namespaces(self):
        t = timeit.default_timer()
        try:
            kube_client = self.get_core_v1_client()
            response = kube_client.list_pod_for_all_namespaces()
        except kubernetes.client.rest.ApiException as err:
            self.logger.error('`list_pod_for_all_namespaces` encountered '
                              '%s: %s.', type(err).__name__, err)
            return []
        self.logger.debug('Found %s pods in %s seconds.',
                          len(response.items), timeit.default_timer() - t)
        return response.items

    def list_namespaced_pod(self):
        t = timeit.default_timer()
        try:
            kube_client = self.get_core_v1_client()
            response = kube_client.list_namespaced_pod(self.namespace)
        except kubernetes.client.rest.ApiException as err:
            self.logger.error('`list_namespaced_pod %s` encountered %s: %s',
                              self.namespace, type(err).__name__, err)
            return []
        self.logger.debug('Found %s pods in namespace `%s` in %s seconds.',
                          len(response.items), self.namespace,
                          timeit.default_timer() - t)
        return response.items

    def is_whitelisted(self, pod_name):
        """Ignore missing pods that are whitelisted"""
        pod_name = str(pod_name)
        return any(pod_name.startswith(x) for x in self.whitelisted_pods)

    def remove_key_from_queue(self, redis_key):
        start = timeit.default_timer()
        self.logger.info('Removing key `%s` from queue `%s`.',
                         redis_key, self.processing_queue)
        res = self.redis_client.lrem(self.processing_queue, 1, redis_key)
        self.logger.info('Removed key `%s` from queue `%s` in %s seconds.',
                         redis_key, self.processing_queue,
                         timeit.default_timer() - start)
        return res

    def restart_redis_key(self, redis_key, new_status='new'):
        start = timeit.default_timer()
        self.logger.info('Restarting key `%s`.', redis_key)
        # reset key status
        self.redis_client.hmset(redis_key, {
            'status': new_status,
            'updated_at': datetime.datetime.now(pytz.UTC).isoformat(),
        })
        self.remove_key_from_queue(redis_key)  # remove from processing queue
        self.redis_client.lpush(self.queue, redis_key)  # push to work queue
        self.logger.info('Restarted key `%s` in %s seconds.',
                         redis_key, timeit.default_timer() - start)

    def _update_pods(self):
        """Refresh pod data and update timestamp"""
        namespaced_pods = self.list_pod_for_all_namespaces()
        self.pods = {pod.metadata.name: pod for pod in namespaced_pods}
        self.pods_updated_at = datetime.datetime.now(pytz.UTC)

    def update_pods(self):
        """Calls `_update_pods` if longer than `pod_refresh_interval`"""
        if self.pods_updated_at is None:
            self._update_pods()
        elif not isinstance(self.pods_updated_at, datetime.datetime):
            raise ValueError('`update_pods` expected `pods_updated_at` to be'
                             ' a `datetime.datetime` instance got %s.' %
                             type(self.pods_updated_at).__name__)
        else:
            diff = self.pods_updated_at - datetime.datetime.now(pytz.UTC)
            if diff.total_seconds() > self.pod_refresh_interval:
                self._update_pods()

    def is_stale_update_time(self, updated_time, stale_time=None):
        stale_time = stale_time if stale_time else self.stale_time
        # TODO: `dateutil` deprecated by python 3.7 `fromisoformat`
        # updated_time = datetime.datetime.fromisoformat(updated_time)
        if not updated_time:
            return False
        if not stale_time > 0:
            return False
        if isinstance(updated_time, str):
            updated_time = dateutil.parser.parse(updated_time)
        current_time = datetime.datetime.now(pytz.UTC)
        update_diff = current_time - updated_time
        return update_diff.total_seconds() >= stale_time

    def clean_key(self, key):
        hvals = self.redis_client.hgetall(key)
        self.update_pods()

        key_status = hvals.get('status')

        if not self.is_stale_update_time(hvals.get('updated_at')):
            return False

        # key is stale, must be repaired somehow
        self.logger.info('Key `%s` has been in queue `%s` with status `%s` for'
                         ' longer than `%s` seconds.', key,
                         self.cleaning_queue, key_status, self.stale_time)

        if key_status in {'done', 'failed'}:
            # job is finished, no need to restart the key
            self.remove_key_from_queue(key)
            return True

        # key is in-progress. check `updated_by` for new status value
        if self.is_whitelisted(hvals.get('updated_by')):
            new_status = key_status
        else:
            new_status = 'new'

        # if the job is finished, no need to restart the key
        self.restart_redis_key(key, new_status)
        return True

    def get_processing_keys(self, count=100):
        match = '{}:*'.format(self.processing_queue)
        processing_keys = self.redis_client.scan_iter(match=match, count=count)
        return processing_keys

    def clean(self):
        cleaned = 0

        for q in self.get_processing_keys(count=100):
            self.cleaning_queue = q  # just for logging
            for key in self.redis_client.lrange(q, 0, -1):
                is_key_cleaned = self.clean_key(key)
                cleaned = cleaned + int(is_key_cleaned)

        if cleaned:  # loop is finished, summary log
            self.total_repairs += cleaned
            self.logger.info('Repaired %s keys (%s total).',
                             cleaned, self.total_repairs)
