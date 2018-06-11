# coding: utf-8

# This file is part of fedora_messaging.
# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
from __future__ import absolute_import, unicode_literals

import unittest

import mock
from pika import exceptions as pika_errs
from jsonschema.exceptions import ValidationError as JSONValidationError

from fedora_messaging import _session, config, message
from fedora_messaging.exceptions import (
    PublishReturned, ConnectionException, Nack, Drop,
    HaltConsumer)


class PublisherSessionTests(unittest.TestCase):

    def setUp(self):
        self.publisher = _session.PublisherSession()
        self.publisher._connection = mock.Mock()
        self.publisher._channel = mock.Mock()
        self.message = mock.Mock()
        self.message.headers = {}
        self.message.topic = "test.topic"
        self.message.body = "test body"
        self.message.schema_version = 1

    def test_publisher_init(self):
        publisher = _session.PublisherSession()
        self.assertEqual(publisher._parameters.host, "localhost")
        self.assertEqual(publisher._parameters.port, 5672)
        self.assertEqual(publisher._parameters.virtual_host, "/")
        self.assertEqual(publisher._parameters.ssl, False)
        # Now test with a custom URL
        publisher = _session.PublisherSession(
            "amqps://username:password@rabbit.example.com/vhost",
            "test_exchange",
        )
        self.assertEqual(publisher._parameters.host, "rabbit.example.com")
        self.assertEqual(publisher._parameters.port, 5671)
        self.assertEqual(publisher._parameters.virtual_host, "vhost")
        self.assertEqual(publisher._parameters.ssl, True)
        self.assertEqual(publisher._exchange, "test_exchange")

    def test_publish(self):
        # Check that the publication works properly.
        self.publisher.publish(self.message)
        self.message.validate.assert_called_once()
        self.publisher._channel.publish.assert_called_once()
        publish_call = self.publisher._channel.publish.call_args_list[0][0]
        self.assertEqual(publish_call[0], None)
        self.assertEqual(publish_call[1], b"test.topic")
        self.assertEqual(publish_call[2], b'"test body"')
        properties = publish_call[3]
        self.assertEqual(properties.content_type, "application/json")
        self.assertEqual(properties.content_encoding, "utf-8")
        self.assertEqual(properties.delivery_mode, 2)
        self.assertDictEqual(properties.headers, {
            'fedora_messaging_schema': "mock.mock:Mock",
            'fedora_messaging_schema_version': 1,
        })

    def test_publish_rejected(self):
        # Check that the correct exception is raised when the publication is
        # rejected.
        self.publisher._channel.publish.side_effect = \
            pika_errs.NackError([self.message])
        self.assertRaises(
            PublishReturned, self.publisher.publish, self.message)
        self.publisher._channel.publish.side_effect = \
            pika_errs.UnroutableError([self.message])
        self.assertRaises(
            PublishReturned, self.publisher.publish, self.message)

    def test_publish_generic_error(self):
        # Check that the correct exception is raised when the publication has
        # failed for an unknown reason, and that the connection is closed.
        self.publisher._connection.is_open = False
        self.publisher._channel.publish.side_effect = \
            pika_errs.AMQPError()
        self.assertRaises(
            ConnectionException, self.publisher.publish, self.message)
        self.publisher._connection.is_open = True
        self.assertRaises(
            ConnectionException, self.publisher.publish, self.message)
        self.publisher._connection.close.assert_called_once()

    def test_connect_and_publish_not_connnected(self):
        self.publisher._connection = None
        self.publisher._channel = None
        connection_class_mock = mock.Mock()
        connection_mock = mock.Mock()
        channel_mock = mock.Mock()
        connection_class_mock.return_value = connection_mock
        connection_mock.channel.return_value = channel_mock
        with mock.patch(
                "fedora_messaging._session.pika.BlockingConnection",
                connection_class_mock):
            self.publisher._connect_and_publish(
                None, self.message, "properties")
        connection_class_mock.assert_called_with(self.publisher._parameters)
        channel_mock.confirm_delivery.assert_called_once()
        channel_mock.publish.assert_called_with(
            None, b"test.topic", b'"test body"', "properties",
        )

    def test_publish_disconnected(self):
        # The publisher must try to re-establish a connection on publish.
        self.publisher._channel.publish.side_effect = \
            pika_errs.ConnectionClosed()
        connection_class_mock = mock.Mock()
        connection_mock = mock.Mock()
        channel_mock = mock.Mock()
        connection_class_mock.return_value = connection_mock
        connection_mock.channel.return_value = channel_mock
        with mock.patch(
                "fedora_messaging._session.pika.BlockingConnection",
                connection_class_mock):
            self.publisher.publish(self.message)
        # Check that the connection was reestablished
        connection_class_mock.assert_called_with(self.publisher._parameters)
        channel_mock.confirm_delivery.assert_called_once()
        self.assertEqual(self.publisher._connection, connection_mock)
        self.assertEqual(self.publisher._channel, channel_mock)
        channel_mock.publish.assert_called_once()

    def test_publish_reconnect_failed(self):
        # The publisher must try to re-establish a connection on publish, and
        # close the connection if it can't be established.
        self.publisher._channel.publish.side_effect = \
            pika_errs.ChannelClosed()
        connection_class_mock = mock.Mock()
        connection_mock = mock.Mock()
        connection_class_mock.return_value = connection_mock
        connection_mock.channel.side_effect = pika_errs.AMQPConnectionError()
        with mock.patch(
                "fedora_messaging._session.pika.BlockingConnection",
                connection_class_mock):
            self.assertRaises(
                ConnectionException, self.publisher.publish, self.message)
        # Check that the connection was reestablished
        connection_class_mock.assert_called_with(self.publisher._parameters)
        self.assertEqual(self.publisher._connection, connection_mock)
        connection_mock.close.assert_called_once()


class ConsumerSessionTests(unittest.TestCase):

    def setUp(self):
        self.consumer = _session.ConsumerSession()

    def tearDown(self):
        self.consumer._shutdown()

    def test_consume(self):
        # Test the consume function.
        def stop_consumer():
            # Necessary to exit the while loop
            self.consumer._running = False
        connection = mock.Mock()
        connection.ioloop.start.side_effect = stop_consumer
        with mock.patch(
                "fedora_messaging._session.pika.SelectConnection",
                lambda *a, **kw: connection):
            # Callback is a callable
            def callback(m):
                return
            self.consumer.consume(callback)
            self.assertEqual(self.consumer._consumer_callback, callback)
            connection.ioloop.start.assert_called_once()
            # Callback is a class
            self.consumer.consume(mock.Mock)
            self.assertTrue(isinstance(
                self.consumer._consumer_callback, mock.Mock))
            # Configuration defaults
            self.consumer.consume(callback)
            self.assertEqual(
                self.consumer._bindings, config.DEFAULTS["bindings"])
            self.assertEqual(
                self.consumer._queues, config.DEFAULTS["queues"])
            self.assertEqual(
                self.consumer._exchanges, config.DEFAULTS["exchanges"])
            # Configuration overrides
            test_value = [{"test": "test"}]
            self.consumer.consume(
                callback,
                bindings=test_value,
                queues=test_value,
                exchanges=test_value,
            )
            self.assertEqual(self.consumer._bindings, test_value)
            self.assertEqual(self.consumer._queues, test_value)
            self.assertEqual(self.consumer._exchanges, test_value)

    def test_declare(self):
        # Test that the exchanges, queues and bindings are properly
        # declared.
        self.consumer._channel = mock.Mock()
        self.consumer._exchanges = {
            "testexchange": {
                "type": "type",
                "durable": "durable",
                "auto_delete": "auto_delete",
                "arguments": "arguments",
            }
        }
        self.consumer._queues = {
            "testqueue": {
                "durable": "durable",
                "auto_delete": "auto_delete",
                "exclusive": "exclusive",
                "arguments": "arguments",
            },
        }
        self.consumer._bindings = [{
            "queue": "testqueue",
            "exchange": "testexchange",
            "routing_keys": ["testrk"],
        }]
        # Declare exchanges and queues
        self.consumer._on_qosok(None)
        self.consumer._channel.exchange_declare.assert_called_with(
            self.consumer._on_exchange_declareok,
            "testexchange",
            exchange_type="type",
            durable="durable",
            auto_delete="auto_delete",
            arguments="arguments",
        )
        self.consumer._channel.queue_declare.assert_called_with(
            self.consumer._on_queue_declareok,
            queue="testqueue",
            durable="durable",
            auto_delete="auto_delete",
            exclusive="exclusive",
            arguments="arguments",
        )
        # Declare bindings
        frame = mock.Mock()
        frame.method.queue = "testqueue"
        self.consumer._on_queue_declareok(frame)
        self.consumer._channel.queue_bind.assert_called_with(
            None, "testqueue", "testexchange", "testrk",
        )
        self.consumer._channel.basic_consume.assert_called_with(
            self.consumer._on_message, "testqueue",
        )


class ConsumerSessionMessageTests(unittest.TestCase):

    def setUp(self):
        message._class_registry["FakeMessageClass"] = FakeMessageClass
        self.consumer = _session.ConsumerSession()
        self.callback = self.consumer._consumer_callback = mock.Mock()
        self.channel = mock.Mock()
        self.consumer._connection = mock.Mock()
        self.consumer._running = True
        self.frame = mock.Mock()
        self.frame.delivery_tag = "testtag"
        self.frame.routing_key = "test.topic"
        self.properties = mock.Mock()
        self.properties.headers = {
            "fedora_messaging_schema": "FakeMessageClass"
        }
        self.properties.content_encoding = "utf-8"

    def tearDown(self):
        self.consumer._shutdown()

    def test_message(self):
        body = b'"test body"'
        self.consumer._on_message(self.channel, self.frame, self.properties, body)
        self.consumer._consumer_callback.assert_called_once()
        msg = self.consumer._consumer_callback.call_args_list[0][0][0]
        msg.validate.assert_called_once()
        self.channel.basic_ack.assert_called_with(delivery_tag="testtag")
        self.assertEqual(msg.body, "test body")

    def test_message_encoding(self):
        body = '"test body unicode é à ç"'.encode("utf-8")
        self.properties.content_encoding = None
        self.consumer._on_message(self.channel, self.frame, self.properties, body)
        self.consumer._consumer_callback.assert_called_once()
        msg = self.consumer._consumer_callback.call_args_list[0][0][0]
        self.assertEqual(msg.body, "test body unicode é à ç")

    def test_message_wrong_encoding(self):
        body = '"test body unicode é à ç"'.encode("utf-8")
        self.properties.content_encoding = "ascii"
        self.consumer._on_message(
            self.channel, self.frame, self.properties, body)
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=False)
        self.consumer._consumer_callback.assert_not_called()

    def test_message_not_json(self):
        body = b"plain string"
        self.consumer._on_message(
            self.channel, self.frame, self.properties, body)
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=False)
        self.consumer._consumer_callback.assert_not_called()

    def test_message_validation_failed(self):
        body = b'"test body"'
        with mock.patch(__name__ + ".FakeMessageClass.VALIDATE_OK", False):
            self.consumer._on_message(
                self.channel, self.frame, self.properties, body)
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=False)
        self.consumer._consumer_callback.assert_not_called()

    def test_message_nack(self):
        self.consumer._consumer_callback.side_effect = Nack()
        self.consumer._on_message(
            self.channel, self.frame, self.properties, b'"body"')
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=True)

    def test_message_drop(self):
        self.consumer._consumer_callback.side_effect = Drop()
        self.consumer._on_message(
            self.channel, self.frame, self.properties, b'"body"')
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=False)

    def test_message_halt(self):
        self.consumer._consumer_callback.side_effect = HaltConsumer()
        self.consumer._on_message(
            self.channel, self.frame, self.properties, b'"body"')
        self.channel.basic_nack.assert_called_with(
            delivery_tag="testtag", requeue=True)
        self.assertFalse(self.consumer._running)
        self.consumer._connection.close.assert_called_once()

    def test_message_exception(self):
        error = ValueError()
        self.consumer._consumer_callback.side_effect = error
        with self.assertRaises(HaltConsumer) as cm:
            self.consumer._on_message(
                self.channel, self.frame, self.properties, b'"body"',
            )
        self.assertEqual(cm.exception.exit_code, 1)
        self.assertEqual(cm.exception.reason, error)
        self.channel.basic_nack.assert_called_with(
            delivery_tag=0, multiple=True, requeue=True)
        self.assertFalse(self.consumer._running)
        self.consumer._connection.close.assert_called_once()


class FakeMessageClass(message.Message):

    VALIDATE_OK = True

    def __init__(self, *args, **kwargs):
        super(FakeMessageClass, self).__init__(*args, **kwargs)
        self.validate = mock.Mock()
        if not self.VALIDATE_OK:
            self.validate.side_effect = JSONValidationError(None)