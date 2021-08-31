import base64
import requests
import json
import logging
import random

from itertools import cycle
from functools import partial


class ProxyError(Exception):
    pass


DEFAULT_HOST = 'localhost:8082'


class Client(object):
    def __init__(self, host=DEFAULT_HOST, api_key=None):
        self.host = host
        self.api_key = api_key
        

    def _request(self, method, *endpoint, **kwargs):
        logging.info('requesting %s %s %s', method, endpoint, kwargs)
        host = self.host
        if self.api_key is not None:
            response = requests.request(method, '{}/{}'.format(host, '/'.join(endpoint)), params=self.get_api_key(), **kwargs)
        else:
            response = requests.request(method, '{}/{}'.format(host, '/'.join(endpoint)), **kwargs)
        logging.info('response from proxy %d: %s', response.status_code, response.text)
        if 200 <= response.status_code < 300:
            return response
        else:
            raise ProxyError(response.json()['message'])

    def create_consumer(self, group, fmt, name=None, auto_commit=True, offset_reset=None):
        assert fmt in {'binary', 'json', 'avro'}
        assert offset_reset in {None, 'smallest', 'largest'}
        response = self._request('POST', 'consumers', group, json={
            "name": name,
            "format": fmt,
            "auto.offset.reset": offset_reset,
            "auto.commit.enable": json.dumps(auto_commit),
        })
        return Consumer(self.host, group, response.json()['instance_id'], fmt)

    def get_consumer(self, group, fmt, name):
        host = self.host
        return Consumer(self.host, group, name, fmt)
    
    def get_api_key(self):
        assert self.api_key is not None
        return {'key': self.api_key}


class _Producer(Client):
    def __init__(self, host=DEFAULT_HOST, api_key=None):
        super(_Producer, self).__init__(host, api_key)
        self._produce_headers = {
            'Content-Type': 'application/vnd.kafka.{}.v2+json'.format(self._format)
        }
        if self._format == 'binary':
            self._encoder = lambda value: base64.b64encode(value.encode('utf-8')).decode('utf-8')
        else:
            self._encoder = lambda value: value

    def produce(self, topic, *records):
        payload = self._gen_payload(records)
        return self._request('POST', 'topics', topic, json=payload, headers=self._produce_headers).json()


class BinaryProducer(_Producer):
    _format = 'binary'

    def _gen_payload(self, records):
        return {'records': [dict(value=self._encoder(r.pop('value')), key=self._encoder(r.pop('key'))) for r in records]}


class JsonProducer(_Producer):
    _format = 'json'

    def _gen_payload(self, records):
        return {'records': records}


class AvroProducer(_Producer):
    _format = 'avro'

    def __init__(self, key_schema, value_schema, host=DEFAULT_HOST):
        super(AvroProducer, self).__init__(host)
        self.key_schema = json.dumps(key_schema)
        self.value_schema = json.dumps(value_schema)

    def _gen_payload(self, records):
        return {'records': list(records), 'key_schema': self.key_schema, 'value_schema': self.value_schema}


class Consumer(Client):
    def __init__(self, host, group, name, fmt, api_key=None):
        super(Consumer, self).__init__(host, api_key)
        self.group = group
        self.name = name
        self.base_uri = 'consumers/{}/instances/{}'.format(self.group, self.name)
        self._consume_headers = {
            'Accept': 'application/vnd.kafka.{}.v2+json'.format(fmt)
        }
        if fmt == 'binary':
            self._decoder = lambda value: base64.b64decode(value)
        else:
            self._decoder = lambda value: value

    def messages(self, topic):
        while True:
            messages = self._request('GET', self.base_uri, 'topics', topic, headers=self._consume_headers).json()
            for m in messages:
                m['value'] = self._decoder(m['value'])
                yield m

    def commit():
        self._request('POST', self.base_uri, 'offsets')

    def close(self):
        self._request('DELETE', self.base_uri)

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        self.close()
