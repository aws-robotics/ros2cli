# Copyright 2019 Amazon.com, Inc. or its affiliates. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ctypes
import threading

import pytest

import rclpy
from rclpy.executors import SingleThreadedExecutor
from ros2cli import cli
from std_msgs.msg import String

TEST_NODE = 'cli_echo_pub_test_node'
TEST_NAMESPACE = 'cli_echo_pub'
TEST_TOPIC = '/clitest/pub/chatter'
SPIN_TIMEOUT = 0.1  # Arbitrarily chosen short time to allow spin loop to exit on threading event


@pytest.fixture(scope='module')
def echo_pub_node():
    """Set up the global rclpy context and node for this test module."""
    context = rclpy.context.Context()
    rclpy.init(context=context)
    node = rclpy.create_node(TEST_NODE, namespace=TEST_NAMESPACE, context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    yield node, executor, context
    node.destroy_node()
    rclpy.shutdown(context=context)


class CLIRunThread(threading.Thread):
    """Encapsulates running the CLI entrypoint as a thread that can be forced to stop."""

    def __init__(self, event, command, capsys):
        super(CLIRunThread, self).__init__()
        self.daemon = True
        # Use this event to signal in case of _unexpected_ CLI main exit
        self._event = event
        self._command = command
        self._capsys = capsys
        self._has_quit = False
        self._captured_stdout = None
        self._captured_stderr = None

    def run(self):
        self._capsys.readouterr()
        result = cli.main(argv=self._command)
        self._captured_stdout, self._captured_stderr = self._capsys.readouterr()
        print('CLI thread exited with message: {}'.format(result))
        self._event.set()
        self._has_quit = True

    def force_quit(self):
        """Force this CLI infinite loop to exit by injecting a KeyboardInterrupt."""
        if self._has_quit:
            # Don't send exception a second time if the thread has already exited
            return
        tid = ctypes.c_long(self.ident)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            tid, ctypes.py_object(KeyboardInterrupt))
        assert res >= 1, 'Failed to raise exception to stop the CLI thread'

    @property
    def captured_stdout(self):
        return self._captured_stdout

    @property
    def captured_stderr(self):
        return self._captured_stderr


def test_pub_basic(echo_pub_node, capsys):
    """Run a local subscription, show that we receive one message from CLI `ros2 topic pub`."""
    node, executor, context = echo_pub_node

    # Create communications objects
    received_message_count = 0
    event = threading.Event()

    def message_callback(msg):
        """If we receive one message, the test has succeeded."""
        nonlocal received_message_count
        received_message_count += 1
        event.set()

    subscription = node.create_subscription(String, TEST_TOPIC, message_callback, qos_profile=10)
    assert subscription

    # Start CLI infinite loop
    pub_thread = CLIRunThread(
        event,
        ['topic', 'pub', TEST_TOPIC, 'std_msgs/String', 'data: hello'],
        capsys)
    pub_thread.start()

    # Start listening loop with early exit and timeout criteria
    def timeout():
        pub_thread.force_quit()
        timeout_timer.cancel()

    timeout_timer = node.create_timer(2, timeout)
    while context.ok() and not event.is_set():
        executor.spin_once(SPIN_TIMEOUT)

    # Cleanup
    pub_thread.force_quit()
    pub_thread.join()
    node.destroy_timer(timeout_timer)
    node.destroy_subscription(subscription)

    # Check results
    assert received_message_count, "Didn't receive any messages on the published topic"


def test_echo_basic(echo_pub_node, capsys):
    """Run a local publisher, check that `ros2 topic echo` receives at least one message."""
    node, executor, context = echo_pub_node
    test_topic = '/clitest/echo/chatter'

    # Create communications objects
    publisher = node.create_publisher(String, test_topic, qos_profile=10)
    event = threading.Event()

    # Start CLI infinite loop
    echo_thread = CLIRunThread(event, ['topic', 'echo', test_topic], capsys)
    echo_thread.start()

    # Start publishing loop with timeout (no early exit criteria)
    def timeout():
        echo_thread.force_quit()
        timeout_timer.cancel()

    def publish_message():
        publisher.publish(String(data='hello'))

    timeout_timer = node.create_timer(2, timeout)
    publish_timer = node.create_timer(0.5, publish_message)

    while context.ok() and not event.is_set():
        executor.spin_once(SPIN_TIMEOUT)

    # Cleanup
    echo_thread.force_quit()
    echo_thread.join()
    node.destroy_timer(timeout_timer)
    node.destroy_timer(publish_timer)
    node.destroy_publisher(publisher)

    # Check results
    assert echo_thread.captured_stdout, "Echo CLI didn't print any output"
    out = echo_thread.captured_stdout.split('\n')
    assert 'data: hello' in out, 'Echo CLI did not print expected message'
