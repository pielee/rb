import time

from weakref import ref as weakref
from itertools import izip

from redis import StrictRedis
from redis.exceptions import ConnectionError, TimeoutError

from rb.promise import Promise
from rb.poll import poll


AUTO_BATCH_COMMANDS = {
    'GET': ('MGET', True),
    'SET': ('MSET', False),
}


def assert_open(client):
    if client.closed:
        raise ValueError('I/O operation on closed file')


def merge_batch(command_name, arg_promise_tuples):
    batch_command, list_response = AUTO_BATCH_COMMANDS[command_name]

    if len(arg_promise_tuples) == 1:
        args, promise = arg_promise_tuples[0]
        return command_name, args, promise

    promise = Promise()

    @promise.done
    def on_success(value):
        if list_response:
            for item, (_, promise) in izip(value, arg_promise_tuples):
                promise.resolve(item)
        else:
            for _, promise in arg_promise_tuples:
                promise.resolve(value)

    args = []
    for individual_args, _ in arg_promise_tuples:
        args.extend(individual_args)

    return batch_command, args, promise


def auto_batch_commands(commands):
    """Given a pipeline of commands this attempts to merge the commands
    into more efficient ones if that is possible.
    """
    pending_batch = None

    for command_name, args, promise in commands:
        # This command cannot be batched, return it as such.
        if command_name not in AUTO_BATCH_COMMANDS:
            if pending_batch:
                yield merge_batch(*pending_batch)
                None
            yield command_name, args, promise
            continue

        if pending_batch and pending_batch[0] == command_name:
            pending_batch[1].append((args, promise))
        else:
            if pending_batch:
                yield merge_batch(*pending_batch)
            pending_batch = (command_name, [(args, promise)])

    if pending_batch:
        yield merge_batch(*pending_batch)


class CommandBuffer(object):
    """The command buffer is an internal construct """

    def __init__(self, host_id, connection, auto_batch=True):
        self.host_id = host_id
        self.connection = connection
        self.commands = []
        self.pending_responses = []
        self.auto_batch = auto_batch

        # Ensure we're connected.  Without this, we won't have a socket
        # we can select over.
        connection.connect()

    @property
    def closed(self):
        """Indicates if the command buffer is closed."""
        return self.connection is None or self.connection._sock is None

    def fileno(self):
        """Returns the file number of the underlying connection's socket
        to be able to select over it.
        """
        assert_open(self)
        return self.connection._sock.fileno()

    def enqueue_command(self, command_name, args):
        """Enqueue a new command into this pipeline."""
        assert_open(self)
        promise = Promise()
        self.commands.append((command_name, args, promise))
        return promise

    def send_pending_requests(self):
        """Sends all pending requests into the connection."""
        assert_open(self)

        unsent_commands = self.commands
        if not unsent_commands:
            return
        self.commands = []

        if self.auto_batch:
            unsent_commands = auto_batch_commands(unsent_commands)

        buf = []
        for command_name, args, promise in unsent_commands:
            buf.append((command_name,) + tuple(args))
            self.pending_responses.append((command_name, promise))

        cmds = self.connection.pack_commands(buf)
        self.connection.send_packed_command(cmds)

    def wait_for_responses(self, client):
        """Waits for all responses to come back and resolves the
        eventual results.
        """
        assert_open(self)

        pending = self.pending_responses
        self.pending_responses = []
        for command_name, promise in pending:
            value = client.parse_response(
                self.connection, command_name)
            promise.resolve(value)


class RoutingPool(object):
    """The routing pool works together with the routing client to
    internally dispatch through the cluster's router to the correct
    internal connection pool.
    """

    def __init__(self, cluster):
        self.cluster = cluster

    def get_connection(self, command_name, shard_hint=None):
        host_id = shard_hint
        if host_id is None:
            raise RuntimeError('The routing pool requires the host id '
                               'as shard hint')

        real_pool = self.cluster.get_pool_for_host(host_id)
        con = real_pool.get_connection(command_name)
        con.__creating_pool = weakref(real_pool)
        return con

    def release(self, connection):
        # The real pool is referenced by the connection through an
        # internal weakref.  If the weakref is broken it means the
        # pool is already gone and we do not need to release the
        # connection.
        try:
            real_pool = connection.__creating_pool()
        except (AttributeError, TypeError):
            real_pool = None

        if real_pool is not None:
            real_pool.release(connection)

    def disconnect(self):
        self.cluster.disconnect_pools()

    def reset(self):
        pass


class BaseClient(StrictRedis):
    pass


class RoutingBaseClient(BaseClient):

    def __init__(self, connection_pool, auto_batch=True):
        BaseClient.__init__(self, connection_pool=connection_pool)
        self.auto_batch = auto_batch

    def pubsub(self, **kwargs):
        raise NotImplementedError('Pubsub is unsupported.')

    def pipeline(self, transaction=True, shard_hint=None):
        raise NotImplementedError('Manual pipelines are unsupported. rb '
                                  'automatically pipelines commands.')

    def lock(self, *args, **kwargs):
        raise NotImplementedError('Locking is not supported.')


class MappingClient(RoutingBaseClient):
    """The routing client uses the cluster's router to target an individual
    node automatically based on the key of the redis command executed.

    For the parameters see :meth:`Cluster.map`.
    """

    def __init__(self, connection_pool, max_concurrency=None,
                 auto_batch=True):
        RoutingBaseClient.__init__(self, connection_pool=connection_pool,
                                   auto_batch=auto_batch)
        # careful.  If you introduce any other variables here, then make
        # sure that FanoutClient.target still works correctly!
        self._max_concurrency = max_concurrency
        self._command_buffer_poll = poll()

    # Standard redis methods

    def execute_command(self, *args):
        router = self.connection_pool.cluster.get_router()
        host_id = router.get_host_for_command(args[0], args[1:])
        buf = self._get_command_buffer(host_id, args[0])
        return buf.enqueue_command(args[0], args[1:])

    # Custom Internal API

    def _get_command_buffer(self, host_id, command_name):
        """Returns the command buffer for the given command and arguments."""
        buf = self._command_buffer_poll.get(host_id)
        if buf is not None:
            return buf

        while len(self._command_buffer_poll) >= self._max_concurrency:
            self._try_to_clear_outstanding_requests()

        connection = self.connection_pool.get_connection(
            command_name, shard_hint=host_id)
        buf = CommandBuffer(host_id, connection, self.auto_batch)
        self._command_buffer_poll.register(host_id, buf)
        return buf

    def _release_command_buffer(self, command_buffer):
        """This is called by the command buffer when it closes."""
        if command_buffer.closed:
            return

        self._command_buffer_poll.unregister(command_buffer.host_id)
        self.connection_pool.release(command_buffer.connection)
        command_buffer.connection = None

    def _try_to_clear_outstanding_requests(self, timeout=1.0):
        """Tries to clear some outstanding requests in the given timeout
        to reduce the concurrency pressure.
        """
        if not self._command_buffer_poll:
            return

        for command_buffer in self._command_buffer_poll:
            command_buffer.send_pending_requests()

        for command_buffer in self._command_buffer_poll.poll(timeout):
            command_buffer.wait_for_responses(self)
            self._release_command_buffer(command_buffer)

    # Custom Public API

    def join(self, timeout=None):
        """Waits for all outstanding responses to come back or the timeout
        to be hit.
        """
        remaining = timeout

        for command_buffer in self._command_buffer_poll:
            command_buffer.send_pending_requests()

        while self._command_buffer_poll and (remaining is None or
                                             remaining > 0):
            now = time.time()
            rv = self._command_buffer_poll.poll(remaining)
            if remaining is not None:
                remaining -= (time.time() - now)
            for command_buffer in rv:
                command_buffer.wait_for_responses(self)
                self._release_command_buffer(command_buffer)

    def cancel(self):
        """Cancels all outstanding requests."""
        for command_buffer in self._command_buffer_poll:
            self._release_command_buffer(command_buffer)


class FanoutClient(MappingClient):
    """This works similar to the :class:`MappingClient` but instead of
    using the router to target hosts, it sends the commands to all manually
    specified hosts.

    The results are accumulated in a dictionary keyed by the `host_id`.

    For the parameters see :meth:`Cluster.fanout`.
    """

    def __init__(self, hosts, connection_pool, max_concurrency=None,
                 auto_batch=True):
        MappingClient.__init__(self, connection_pool, max_concurrency,
                               auto_batch=auto_batch)
        self._target_hosts = hosts
        self.__is_retargeted = False

    def target(self, hosts):
        """Temporarily retarget the client for one call.  This is useful
        when having to deal with a subset of hosts for one call.
        """
        if self.__is_retargeted:
            raise TypeError('Cannot use target more than once.')
        rv = FanoutClient(hosts, connection_pool=self.connection_pool,
                          max_concurrency=self._max_concurrency)
        rv._command_buffer_poll = self._command_buffer_poll
        rv._target_hosts = hosts
        rv.__is_retargeted = True
        return rv

    def execute_command(self, *args):
        promises = {}

        hosts = self._target_hosts
        if hosts == 'all':
            hosts = self.connection_pool.cluster.hosts.keys()
        elif hosts is None:
            raise RuntimeError('Fanout client was not targeted to hosts.')

        for host_id in hosts:
            buf = self._get_command_buffer(host_id, args[0])
            promises[host_id] = buf.enqueue_command(args[0], args[1:])
        return Promise.all(promises)


class RoutingClient(RoutingBaseClient):
    """A client that can route to individual targets.

    For the parameters see :meth:`Cluster.get_routing_client`.
    """

    def __init__(self, cluster, auto_batch=True):
        RoutingBaseClient.__init__(self, connection_pool=RoutingPool(cluster),
                                   auto_batch=auto_batch)

    # Standard redis methods

    def execute_command(self, *args, **options):
        pool = self.connection_pool
        command_name = args[0]
        command_args = args[1:]
        router = self.connection_pool.cluster.get_router()
        host_id = router.get_host_for_command(command_name, command_args)
        connection = pool.get_connection(command_name, shard_hint=host_id)
        try:
            connection.send_command(*args)
            return self.parse_response(connection, command_name, **options)
        except (ConnectionError, TimeoutError) as e:
            connection.disconnect()
            if not connection.retry_on_timeout and isinstance(e, TimeoutError):
                raise
            connection.send_command(*args)
            return self.parse_response(connection, command_name, **options)
        finally:
            pool.release(connection)

    # Custom Public API

    def get_mapping_client(self, max_concurrency=64, auto_batch=None):
        """Returns a thread unsafe mapping client.  This client works
        similar to a redis pipeline and returns eventual result objects.
        It needs to be joined on to work properly.  Instead of using this
        directly you shold use the :meth:`map` context manager which
        automatically joins.

        Returns an instance of :class:`MappingClient`.
        """
        if auto_batch is None:
            auto_batch = self.auto_batch
        return MappingClient(connection_pool=self.connection_pool,
                             max_concurrency=max_concurrency,
                             auto_batch=auto_batch)

    def get_fanout_client(self, hosts, max_concurrency=64,
                          auto_batch=None):
        """Returns a thread unsafe fanout client.

        Returns an instance of :class:`FanoutClient`.
        """
        if auto_batch is None:
            auto_batch = self.auto_batch
        return FanoutClient(hosts, connection_pool=self.connection_pool,
                            max_concurrency=max_concurrency,
                            auto_batch=auto_batch)

    def map(self, timeout=None, max_concurrency=64, auto_batch=None):
        """Returns a context manager for a map operation.  This runs
        multiple queries in parallel and then joins in the end to collect
        all results.

        In the context manager the client available is a
        :class:`MappingClient`.  Example usage::

            results = {}
            with cluster.map() as client:
                for key in keys_to_fetch:
                    results[key] = client.get(key)
            for key, promise in results.iteritems():
                print '%s => %s' % (key, promise.value)
        """
        return MapManager(self.get_mapping_client(max_concurrency, auto_batch),
                          timeout=timeout)

    def fanout(self, hosts=None, timeout=None, max_concurrency=64,
               auto_batch=None):
        """Returns a context manager for a map operation that fans out to
        manually specified hosts instead of using the routing system.  This
        can for instance be used to empty the database on all hosts.  The
        context manager returns a :class:`FanoutClient`.  Example usage::

            with cluster.fanout(hosts=[0, 1, 2, 3]) as client:
                results = client.info()
            for host_id, info in results.value.iteritems():
                print '%s -> %s' % (host_id, info['is'])

        The promise returned accumulates all results in a dictionary keyed
        by the `host_id`.

        The `hosts` parameter is a list of `host_id`\s or alternatively the
        string ``'all'`` to send the commands to all hosts.

        The fanout APi needs to be used with a lot of care as it can cause
        a lot of damage when keys are written to hosts that do not expect
        them.
        """
        return MapManager(self.get_fanout_client(hosts, max_concurrency,
                                                 auto_batch),
                          timeout=timeout)


class LocalClient(BaseClient):
    """The local client is just a convenient method to target one specific
    host.
    """

    def __init__(self, cluster, connection_pool=None, **kwargs):
        if connection_pool is None:
            raise TypeError('The local client needs a connection pool')
        BaseClient.__init__(self, cluster, connection_pool=connection_pool,
                            **kwargs)


class MapManager(object):
    """Helps with mapping."""

    def __init__(self, mapping_client, timeout):
        self.mapping_client = mapping_client
        self.timeout = timeout
        self.entered = None

    def __enter__(self):
        self.entered = time.time()
        return self.mapping_client

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            self.mapping_client.cancel()
        else:
            timeout = self.timeout
            if timeout is not None:
                timeout = max(1, timeout - (time.time() - self.started))
            self.mapping_client.join(timeout=timeout)
