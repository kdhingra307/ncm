from collections import namedtuple
import json
import logging
import time

from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk
import jsonschema
import kafka
import prometheus_client

import mjolnir.kafka

log = logging.getLogger(__name__)
MemoizeEntry = namedtuple('MemoizeEntry', ('value', 'valid_until'))


class Metric(object):
    """A Namespace for our metrics"""
    # Metrics we record in prometheus
    _INVALID_RECORDS = prometheus_client.Counter(
        'mjolnir_bulk_invalid_records_total',
        "Number of requests that could not be processed", ['reason'])
    FAIL_VALIDATE = _INVALID_RECORDS.labels(reason='fail_validate')
    MISSING_INDEX = _INVALID_RECORDS.labels(reason='missing_index')
    SUBMIT_BATCH = prometheus_client.Summary(
        'mjolnir_bulk_submit_batch_seconds',
        'Time taken to submit a batch from kafka to elasticsearch')
    RECORDS_PROCESSED = prometheus_client.Counter(
        'mjolnir_bulk_records_total',
        'Number of kafka records processed')
    _BULK_ACTION_RESULT = prometheus_client.Counter(
        'mjolnir_bulk_action_total',
        'Number of bulk action somethings', ['result'])
    ACTION_RESULTS = {
        'updated': _BULK_ACTION_RESULT.labels(result='updated'),
        'created': _BULK_ACTION_RESULT.labels(result='created'),
        'noop': _BULK_ACTION_RESULT.labels(result='noop'),
    }
    OK_UNKNOWN = _BULK_ACTION_RESULT.labels(result='ok_unknown')
    MISSING = _BULK_ACTION_RESULT.labels(result='missing')
    FAILED = _BULK_ACTION_RESULT.labels(result='failed')


# Fields we accept updates for, found in the _source field of incoming
# messages, and their configuration for the noop plugin.
FIELD_CONFIG = {
    'popularity_score': 'within 20%',
}

# jsonschema of incoming requests
VALIDATOR = jsonschema.Draft4Validator({
    "type": "object",
    "additionalProperties": False,
    "required": ["_index", "_id", "_source"],
    "properties": {
        "_index": {"type": "string"},
        "_id": {"type": ["integer", "string"]},
        "_source": {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {field: {"type": ["number", "string"]} for field in FIELD_CONFIG.keys()}
        }
    }
})


def expand_action(message):
    """Transform an update request into an es bulk update"""
    action = {
        'update': {
            '_index': message['_index'],
            '_type': 'page',
            '_id': message['_id'],
        }
    }

    noop_handlers = {field: FIELD_CONFIG[field] for field in message['_source'].keys()}
    source = {
        'script': {
            'inline': 'super_detect_noop',
            'lang': 'native',
            'params': {
                'handlers': noop_handlers,
                'source': message['_source'],
            }
        }
    }

    return action, source


def stream_to_es(cluster, records):
    # This will throw exceptions for any error connecting to
    # elasticsearch (perhaps a rolling restart?). In that case the
    # daemon will shut down and be restarted by systemd. Rebalancing
    # will assign the partition to another daemon that hopefully isn't
    # having connection issues.
    for ok, result in streaming_bulk(
        client=cluster,
        actions=records,
        raise_on_error=False,
        expand_action_callback=expand_action,
    ):
        action, result = result.popitem()
        status = result.get('status', 500)
        if ok:
            if 'result' in result and result['result'] in Metric.ACTION_RESULTS:
                Metric.ACTION_RESULTS[result['result']].inc()
            else:
                Metric.OK_UNKNOWN.inc()
        elif status == 404:
            # 404 are quite common so we log them separately. The analytics
            # side doesn't know the namespace mappings and attempts to send all
            # updates to <wiki>_content, letting the docs that don't exist fail
            Metric.MISSING.inc()
        else:
            Metric.FAILED.inc()
            log.warning('Failed elasticsearch %s request: %s', action, str(result)[:512])


def ttl_memoize(f, **kwargs):
    TTL = 300
    cache = {}

    def memoized(*args):
        now = time.time()
        if args in cache:
            entry = cache[args]
            if entry.valid_until > now:
                return entry.value
        value = f(*args, **kwargs)
        cache[args] = MemoizeEntry(value, now + TTL)
        return value

    return memoized


def available_indices(cluster):
    """Returns the set of addressable indices and aliases."""
    indices = set()
    for index_name, data in cluster.indices.get_alias().items():
        indices.add(index_name)
        indices.update(data['aliases'].keys())
    return indices


def split_records_by_cluster(indices_on_clusters, poll_response):
    """Split a poll response from kafka into per-es-cluster batches"""
    split = [[] for _ in indices_on_clusters]
    for records in poll_response.values():
        for record in records:
            try:
                value = json.loads(record.value.decode('utf-8'))
            except ValueError:
                log.warning('Invalid message: %s', record.value[:128])
                continue

            errors = list(VALIDATOR.iter_errors(value))
            if errors:
                Metric.FAIL_VALIDATE.inc()
                log.warning('\n'.join(map(str, errors)))
                continue

            for i, indices in enumerate(indices_on_clusters):
                if value['_index'] in indices:
                    split[i].append(value)
                    break
            else:
                Metric.MISSING_INDEX.inc()
                log.warning('Could not find cluster for index %s', value['_index'])
    return split


def make_es_clusters(bootstrap_hosts):
    clusters = [Elasticsearch(host) for host in bootstrap_hosts.split(',')]
    seen = set()
    for cluster in clusters:
        info = cluster.info()
        if info['cluster_uuid'] in seen:
            raise ValueError(
                'Cluster %s (uuid %s) seen from more than one bootstrap host',
                info['cluster_name'], info['cluster_uuid'])
        seen.add(info['cluster_uuid'])
        log.info('Connected to elasticsearch %s', info['cluster_name'])
    return clusters


def run(brokers, es_clusters, topics, group_id, prometheus_port):
    prometheus_client.start_http_server(prometheus_port)
    es_clusters = make_es_clusters(es_clusters)
    all_available_indices_memo = ttl_memoize(lambda: [available_indices(c) for c in es_clusters])
    # consumer.metrics() exposes a bunch of things we could record, but not sure which
    # would be useful.
    consumer = kafka.KafkaConsumer(
        bootstrap_servers=brokers,
        group_id=group_id,
        # Commits are manually performed for each batch returned by poll()
        # after they have been processed by elasticsearch.
        enable_auto_commit=False,
        # If we lose the offset safest thing is to replay from
        # the beginning. In WMF this is typically 7 days, the
        # same lifetime as offsets.
        auto_offset_reset='earliest',
        api_version=mjolnir.kafka.BROKER_VERSION,
        # Our expected records are tiny and compress well. Accept
        # large batches. Increased from default of 500. This is
        # still only ~250kb serialized and decompressed.
        max_poll_records=2000,
    )

    log.info('Subscribing to: %s', ', '.join(topics))
    consumer.subscribe(topics)
    try:
        offset_commit_interval_sec = 60
        last_commit = 0
        offsets = {}
        while True:
            now = time.monotonic()
            if offsets and now - last_commit > offset_commit_interval_sec:
                consumer.commit_async(offsets)
                last_commit = now
                offsets = {}

            batch = consumer.poll(timeout_ms=60000)
            # Did the poll time out?
            if not batch:
                continue
            Metric.RECORDS_PROCESSED.inc(sum(len(x) for x in batch.values()))
            # Figure out what cluster everything goes to
            split = split_records_by_cluster(
                all_available_indices_memo(), batch)
            # Send to the cluster
            with Metric.SUBMIT_BATCH.time():
                for cluster, records in zip(es_clusters, split):
                    if records:
                        stream_to_es(cluster, records)
            # Tell kafka we did the work
            for tp, records in batch.items():
                offsets[tp] = kafka.OffsetAndMetadata(records[-1].offset + 1, '')
    finally:
        if offsets:
            consumer.commit(offsets)
        consumer.close()
