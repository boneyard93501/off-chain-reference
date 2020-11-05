# Copyright (c) The Libra Core Contributors
# SPDX-License-Identifier: Apache-2.0

from ..protocol import VASPPairChannel, make_protocol_error, \
    DependencyException, LOCK_EXPIRED, LOCK_AVAILABLE
from ..protocol_messages import CommandRequestObject, CommandResponseObject, \
    OffChainProtocolError, OffChainException
from ..errors import OffChainErrorCode
from ..sample.sample_command import SampleCommand
from ..command_processor import CommandProcessor
from ..utils import JSONSerializable, JSONFlag
from ..storage import StorableFactory
from ..crypto import OffChainInvalidSignature

from copy import deepcopy
import random
from unittest.mock import MagicMock
import pytest
import json


class RandomRun(object):
    def __init__(self, server, client, commands, seed='fixed seed'):
        # MESSAGE QUEUES
        self.to_server_requests = []
        self.to_client_response = []
        self.to_client_requests = []
        self.to_server_response = []

        self.server = server
        self.client = client

        self.commands = commands
        self.number = len(commands)
        random.seed(seed)

        self.DROP = True
        self.VERBOSE = False

        self.rejected = 0

    def run(self):
        to_server_requests = self.to_server_requests
        to_client_response = self.to_client_response
        to_client_requests = self.to_client_requests
        to_server_response = self.to_server_response
        server = self.server
        client = self.client
        commands = self.commands

        while True:

            # Inject a command every round
            if random.random() > 0.99:
                if len(commands) > 0:
                    c = commands.pop(0)
                    try:
                        if random.random() > 0.5:
                            req = client.sequence_command_local(c)
                            to_server_requests += [req]
                        else:
                            req = server.sequence_command_local(c)
                            to_client_requests += [req]
                    except DependencyException:
                        self.rejected += 1

            # Random drop
            while self.DROP and random.random() > 0.3:
                kill_list = random.choice([to_server_requests,
                                           to_client_requests,
                                           to_client_response,
                                           to_server_response])
                del kill_list[-1:]

            Case = [False, False, False, False, False]
            Case[random.randint(0, len(Case) - 1)] = True
            Case[random.randint(0, len(Case) - 1)] = True

            # Make progress by delivering a random queue
            if Case[0] and len(to_server_requests) > 0:
                client_request = to_server_requests.pop(0)
                resp = server.handle_request(client_request)
                to_client_response += [resp]

            if Case[1] and len(to_client_requests) > 0:
                server_request = to_client_requests.pop(0)
                resp = client.handle_request(server_request)
                to_server_response += [resp]

            if Case[2] and len(to_client_response) > 0:
                rep = to_client_response.pop(0)
                # assert req.client_sequence_number is not None
                try:
                    client.handle_response(rep)
                except OffChainProtocolError:
                    pass
                except OffChainException:
                    raise

            if Case[3] and len(to_server_response) > 0:
                rep = to_server_response.pop(0)
                try:
                    server.handle_response(rep)
                except OffChainProtocolError:
                    pass
                except OffChainException:
                    raise

            # Retransmit
            if Case[4] and random.random() > 0.10:
                cr = client.get_retransmit()
                to_server_requests += cr
                sr = server.get_retransmit()
                to_client_requests += sr

            if self.VERBOSE:
                print([to_server_requests,
                       to_client_requests,
                       to_client_response,
                       to_server_response])

                print([server.would_retransmit(),
                       client.would_retransmit(),
                       len(server.committed_commands),
                       len(client.committed_commands)])

            if not server.would_retransmit() and not client.would_retransmit() \
                    and len(server.committed_commands) + self.rejected == self.number \
                    and len(client.committed_commands) + self.rejected == self.number:
                break

    def checks(self, NUMBER):
        client = self.client
        server = self.server

        client_exec_cid = client.committed_commands.keys()
        server_exec_cid = server.committed_commands.keys()
        client_seq = [client.committed_commands[c].command.item() for c in client_exec_cid]
        server_seq = [server.committed_commands[c].command.item() for c in server_exec_cid]

        assert len(client_seq) == NUMBER - self.rejected
        assert set(client_seq) == set(server_seq)


def test_create_channel_to_myself(three_addresses, vasp):
    a0, _, _ = three_addresses
    command_processor = MagicMock(spec=CommandProcessor)
    store = MagicMock()
    with pytest.raises(OffChainException):
        channel = VASPPairChannel(a0, a0, vasp, store, command_processor)


def test_client_server_role_definition(three_addresses, vasp):
    a0, a1, a2 = three_addresses
    command_processor = MagicMock(spec=CommandProcessor)
    store = MagicMock()

    channel = VASPPairChannel(a0, a1, vasp, store, command_processor)
    assert channel.is_server()
    assert not channel.is_client()

    channel = VASPPairChannel(a1, a0, vasp, store, command_processor)
    assert not channel.is_server()
    assert channel.is_client()

    # Lower address is server (xor bit = 1)
    channel = VASPPairChannel(a0, a2, vasp, store, command_processor)
    assert not channel.is_server()
    assert channel.is_client()

    channel = VASPPairChannel(a2, a0, vasp, store, command_processor)
    assert channel.is_server()
    assert not channel.is_client()


def test_protocol_server_client_benign(two_channels):
    server, client = two_channels

    # Create a server request for a command
    request = server.sequence_command_local(SampleCommand('Hello'))
    assert isinstance(request, CommandRequestObject)
    assert len(server.committed_commands) == 0
    assert len(server.my_pending_requests) == 1

    # Pass the request to the client
    assert len(client.committed_commands) == 0
    assert len(client.my_pending_requests) == 0
    reply = client.handle_request(request)
    assert isinstance(reply, CommandResponseObject)
    assert len(client.committed_commands) == 1
    assert len(client.my_pending_requests) == 0
    assert reply.status == 'success'

    # Pass the reply back to the server
    succ = server.handle_response(reply)
    assert succ
    assert len(server.committed_commands) == 1
    assert len(server.my_pending_requests) == 0

    assert client.committed_commands[request.cid].command.item() == 'Hello'


def test_protocol_server_conflicting_sequence(two_channels):
    server, client = two_channels

    # Create a server request for a command
    request = server.sequence_command_local(SampleCommand('Hello'))

    # Modilfy message to be a conflicting sequence number
    request_conflict = deepcopy(request)
    request_conflict.command = SampleCommand("Conflict")

    # Pass the request to the client
    reply = client.handle_request(request)
    reply_conflict = client.handle_request(request_conflict)

    # We only sequence one command.
    assert reply.status == 'success'

    # The response to the second command is a failure
    assert reply_conflict.status == 'failure'
    assert reply_conflict.error.code == OffChainErrorCode.conflict

    # Pass the reply back to the server
    assert len(server.committed_commands) == 0
    with pytest.raises(OffChainProtocolError):
        server.handle_response(reply_conflict)

    succ = server.handle_response(reply)
    assert succ
    assert len(server.committed_commands) == 1


def test_protocol_client_server_benign(two_channels):
    server, client = two_channels

    # Create a client request for a command
    request = client.sequence_command_local(SampleCommand('Hello'))
    assert isinstance(request, CommandRequestObject)
    assert len(client.my_pending_requests) == 1
    assert len(client.committed_commands) == 0

    # Send to server
    reply = server.handle_request(request)
    assert isinstance(reply, CommandResponseObject)
    assert len(server.committed_commands) == 1

    # Pass response back to client
    succ = client.handle_response(reply)
    assert succ
    assert len(client.committed_commands) == 1

    assert client.committed_commands[request.cid].response is not None
    assert client.committed_commands[request.cid].command.item() == 'Hello'


def test_protocol_server_client_interleaved_benign(two_channels):
    server, client = two_channels

    client_request = client.sequence_command_local(SampleCommand('Hello'))
    server_request = server.sequence_command_local(SampleCommand('World'))

    # The server waits until all own requests are done
    server_reply = server.handle_request(client_request)
    assert server_reply.status == 'success'

    client_reply = client.handle_request(server_request)
    server.handle_response(client_reply)
    server_reply = server.handle_request(client_request)
    client.handle_response(server_reply)

    assert len(client.my_pending_requests) == 0
    assert len(server.my_pending_requests) == 0
    assert len(client.committed_commands) == 2
    assert len(server.committed_commands) == 2

    assert client.committed_commands[client_request.cid].response is not None
    assert client.committed_commands[client_request.cid].command.item() == 'Hello'
    assert server.committed_commands[client_request.cid].response is not None
    assert server.committed_commands[client_request.cid].command.item() == 'Hello'

    assert client.committed_commands[server_request.cid].response is not None
    assert client.committed_commands[server_request.cid].command.item() == 'World'
    assert server.committed_commands[server_request.cid].response is not None
    assert server.committed_commands[server_request.cid].command.item() == 'World'


def test_protocol_server_client_handled_previously_seen_messages(two_channels):
    server, client = two_channels

    client_request = client.sequence_command_local(SampleCommand('Hello'))
    server_request = server.sequence_command_local(SampleCommand('World'))

    client_reply = client.handle_request(server_request)
    server_reply = server.handle_request(client_request)
    assert server_reply.status == 'success'
    assert client_reply.status == 'success'

    # Handle seen requests
    client_reply = client.handle_request(server_request)
    server_reply = server.handle_request(client_request)
    assert server_reply.status == 'success'
    assert client_reply.status == 'success'

    assert server.handle_response(client_reply)
    assert client.handle_response(server_reply)

    # Handle seen responses
    assert server.handle_response(client_reply)
    assert client.handle_response(server_reply)

    assert len(client.my_pending_requests) == 0
    assert len(server.my_pending_requests) == 0
    assert len(client.committed_commands) == 2
    assert len(server.committed_commands) == 2

    assert client.committed_commands[client_request.cid].response is not None
    assert client.committed_commands[client_request.cid].command.item() == 'Hello'
    assert server.committed_commands[client_request.cid].response is not None
    assert server.committed_commands[client_request.cid].command.item() == 'Hello'

    assert client.committed_commands[server_request.cid].response is not None
    assert client.committed_commands[server_request.cid].command.item() == 'World'
    assert server.committed_commands[server_request.cid].response is not None
    assert server.committed_commands[server_request.cid].command.item() == 'World'


async def test_protocol_conflict1(two_channels):
    server, client = two_channels

    msg = client.sequence_command_local(SampleCommand('Hello'))
    msg = (await client.package_request(msg)).content

    msg2 = (await server.parse_handle_request(msg)).content

    # Since this is not yet confirmed, reject the command
    with pytest.raises(DependencyException):
        client.sequence_command_local(SampleCommand('World1', deps=['Hello']))

    msg3 = server.sequence_command_local(SampleCommand('World2', deps=['Hello']))
    msg3 = (await server.package_request(msg3)).content

    # Since this is not yet confirmed, make it wait
    msg4 = (await client.parse_handle_request(msg3)).content
    with pytest.raises(OffChainProtocolError):
        succ = await server.parse_handle_response(msg4)

    # Now add the response that creates 'hello'
    assert await client.parse_handle_response(msg2)  # success

async def test_protocol_bad_signature(two_channels):
    server, client = two_channels

    msg = 'XRandomXJunk' # client.package_request(msg).content
    assert (await server.parse_handle_request(msg)).raw.is_failure()

    msg = '.Random.Junk' # client.package_request(msg).content
    assert (await server.parse_handle_request(msg)).raw.is_failure()

async def test_protocol_conflict2(two_channels):
    server, client = two_channels

    msg = client.sequence_command_local(SampleCommand('Hello'))
    msg = (await client.package_request(msg)).content

    msg2 = (await server.parse_handle_request(msg)).content
    assert await client.parse_handle_response(msg2)  # success

    # Two concurrent requests
    creq = client.sequence_command_local(SampleCommand('cW', deps=['Hello']))
    creq = (await client.package_request(creq)).content
    sreq = server.sequence_command_local(SampleCommand('sW', deps=['Hello']))
    sreq = (await server.package_request(sreq)).content

    # Server gets client request
    sresp = (await server.parse_handle_request(creq)).content
    # Client is told to wait
    with pytest.raises(OffChainProtocolError):
        _ = await client.parse_handle_response(sresp)

    # Client gets server request
    cresp = (await client.parse_handle_request(sreq)).content
    assert await server.parse_handle_response(cresp)  # Success
    assert 'Hello' in server.object_locks
    assert server.object_locks['Hello'] == LOCK_EXPIRED

    # Now try again the client request
    sresp = (await server.parse_handle_request(creq)).content
    assert not await client.parse_handle_response(sresp)


def test_protocol_server_client_interleaved_swapped_reply(two_channels):
    server, client = two_channels

    client_request = client.sequence_command_local(SampleCommand('Hello'))
    server_request = server.sequence_command_local(SampleCommand('World'))

    server_reply = server.handle_request(client_request)
    assert server_reply.status == 'success'

    client_reply = client.handle_request(server_request)

    server.handle_response(client_reply)
    server_reply = server.handle_request(client_request)

    client.handle_response(server_reply)

    assert len(client.committed_commands) == 2
    assert len(server.committed_commands) == 2

    assert client.committed_commands[client_request.cid].command.item() == 'Hello'
    assert server.committed_commands[client_request.cid].command.item() == 'Hello'
    assert client.committed_commands[server_request.cid].command.item() == 'World'
    assert server.committed_commands[server_request.cid].command.item() == 'World'


def test_random_interleave_no_drop(two_channels):
    server, client = two_channels

    NUMBER = 20
    commands = list(range(NUMBER))
    commands = [SampleCommand(c) for c in commands]

    R = RandomRun(server, client, commands, seed='drop')
    R.DROP = False
    R.run()

    R.checks(NUMBER)


def test_random_interleave_and_drop(two_channels):
    server, client = two_channels

    NUMBER = 20
    commands = list(range(NUMBER))
    commands = [SampleCommand(c) for c in commands]

    R = RandomRun(server, client, commands, seed='drop')
    R.run()
    R.checks(NUMBER)


def test_random_interleave_and_drop_and_invalid(two_channels):
    server, client = two_channels

    NUMBER = 20
    commands = list(range(NUMBER))
    commands = [SampleCommand(c) for c in commands]
    for c in commands:
        c.always_happy = False

    R = RandomRun(server, client, commands, seed='drop')
    R.run()
    R.checks(NUMBER)

    client = R.client
    server = R.server

    client_exec_cid = client.committed_commands.keys()
    server_exec_cid = server.committed_commands.keys()
    client_seq = [client.committed_commands[c].command.item() for c in client_exec_cid]
    server_seq = [server.committed_commands[c].command.item() for c in server_exec_cid]

    server_store_keys = server.object_locks.keys()
    client_store_keys = client.object_locks.keys()
    assert set(server_store_keys) == set(client_store_keys)


def test_dependencies(two_channels):
    server, client = two_channels

    # Commands with dependencies
    cmd = [(0, []),
           (1, [0]),
           (2, []),
           (3, []),
           (4, [0]),
           (5, []),
           (6, [2]),
           (7, []),
           (8, [1]),
           (9, [4]),
           ]

    NUMBER = len(cmd)
    commands = [SampleCommand(c, deps) for c, deps in cmd]

    R = RandomRun(server, client, commands, seed='deps')
    R.run()
    R.checks(NUMBER)

    client = R.client
    server = R.server

    client_exec_cid = client.committed_commands.keys()
    mapcmd = set([client.committed_commands[c].command.item() for c in client_exec_cid])

    # Only one of the items with common dependency commits
    assert len(mapcmd & {'1', '4'}) == 1
    assert len(mapcmd & {'8', '9'}) == 1
    # All items commit (except those with common deps)
    assert len(mapcmd) == 8


def test_json_serlialize():
    # Test Commands (to ensure correct debug)
    cmd = SampleCommand(1, [2, 3])
    cmd2 = SampleCommand(10, [2, 3])
    data = cmd.get_json_data_dict(JSONFlag.NET)
    cmd2 = SampleCommand.from_json_data_dict(data, JSONFlag.NET)
    assert cmd == cmd2

    # Test Request, Response
    req0 = CommandRequestObject(cmd)
    req2 = CommandRequestObject(cmd2)
    req0.cid = '10'
    req0.status = 'success'

    data = req0.get_json_data_dict(JSONFlag.STORE)
    assert data is not None
    req1 = CommandRequestObject.from_json_data_dict(data, JSONFlag.STORE)
    assert req0 == req1
    assert req1 != req2

    req0.response = make_protocol_error(req0, OffChainErrorCode.test_error_code)
    data_err = req0.get_json_data_dict(JSONFlag.STORE)
    assert data_err is not None
    assert data_err['response'] is not None
    req_err = CommandRequestObject.from_json_data_dict(
        data_err, JSONFlag.STORE)
    assert req0 == req_err


def test_VASProot(three_addresses, vasp):
    a0, a1, a2 = three_addresses

    # Check our own address is good
    assert vasp.get_vasp_address() == a0
    # Calling twice gives the same instance (use 'is')
    assert vasp.get_channel(a1) is vasp.get_channel(a1)
    # Different VASPs have different objects
    assert vasp.get_channel(a1) is not vasp.get_channel(a2)
    assert vasp.get_channel(a2).is_client()


def test_VASProot_diff_object(vasp, three_addresses):
    a0, _, b1 = three_addresses
    b2 = deepcopy(b1)

    # Check our own address is good
    assert vasp.get_vasp_address() == a0
    # Calling twice gives the same instance (use 'is')
    assert vasp.get_channel(b1) is vasp.get_channel(b2)


def test_real_address(three_addresses):
    from os import urandom
    A, _, B = three_addresses
    Ap = deepcopy(A)
    assert B.greater_than_or_equal(A)
    assert not A.greater_than_or_equal(B)
    assert A.greater_than_or_equal(A)
    assert A.greater_than_or_equal(Ap)
    assert A.equal(A)
    assert A.equal(Ap)
    assert not A.equal(B)
    assert not B.equal(Ap)
    assert A.last_bit() ^ B.last_bit() == 1
    assert A.last_bit() ^ A.last_bit() == 0


def test_sample_command():
    store = {}
    cmd1 = SampleCommand('hello')
    store['hello'] = cmd1.get_object('hello', store)
    cmd2 = SampleCommand('World', deps=['hello'])
    obj = cmd2.get_object('World', store)

    data = obj.get_json_data_dict(JSONFlag.STORE)
    obj2 = JSONSerializable.parse(data, JSONFlag.STORE)
    assert obj2.version == obj.version
    assert obj2.previous_version == obj.previous_version


async def test_parse_handle_request_to_future(signed_json_request, channel, key):
    response = await channel.parse_handle_request(signed_json_request)
    res = await key.verify_message(response.content)

    res = json.loads(res)
    assert res['status'] == 'success'


async def test_parse_handle_request_to_future_out_of_order(
    json_request, channel, key
):
    json_request['cid'] = '100'
    json_request = await key.sign_message(json.dumps(json_request))
    fut = await channel.parse_handle_request(json_request)
    res = await key.verify_message(fut.content)
    res = json.loads(res)
    assert res['status']== 'success'


async def test_parse_handle_response_to_future_parsing_error(json_response, channel,
                                                       command, key):
    _ = channel.sequence_command_local(command)
    json_response['cid'] = '"'  # Trigger a parsing error.
    json_response = await key.sign_message(json.dumps(json_response))
    with pytest.raises(Exception):
        _ = await channel.parse_handle_response(json_response)


def test_role(channel):
    assert channel.role() == 'Client'


def test_pending_retransmit_number(channel):
    assert channel.pending_retransmit_number() == 0


async def test_get_dep_locks(two_channels):
    server, client = two_channels

    msg = client.sequence_command_local(SampleCommand('Hello'))
    msg = (await client.package_request(msg)).content
    msg2 = (await server.parse_handle_request(msg)).content
    assert await client.parse_handle_response(msg2)  # success

    msg = client.sequence_command_local(SampleCommand('World'))
    msg = (await client.package_request(msg)).content
    msg2 = (await server.parse_handle_request(msg)).content
    assert await client.parse_handle_response(msg2)  # success

    assert client.object_locks['Hello'] == LOCK_AVAILABLE
    assert client.object_locks['World'] == LOCK_AVAILABLE
    assert server.object_locks['Hello'] == LOCK_AVAILABLE
    assert server.object_locks['World'] == LOCK_AVAILABLE

    assert len(client.object_locks) == 2
    assert len(server.object_locks) == 2

    request_has_missing_deps = CommandRequestObject(SampleCommand('foo', deps=['not_exist1', 'not_exist2']))
    c_missing, c_used, c_locked = client.get_dep_locks(request_has_missing_deps)
    assert set(c_missing) == {"not_exist1", "not_exist2"}
    assert not c_used
    assert not c_locked
    with pytest.raises(DependencyException):
        client.sequence_command_local(request_has_missing_deps.command)

    cw1_request = CommandRequestObject(SampleCommand('cW1', deps=['Hello', 'World']))
    c_missing, c_used, c_locked = client.get_dep_locks(cw1_request)
    assert not c_missing
    assert not c_used
    assert not c_locked

    creq = client.sequence_command_local(SampleCommand('cW2', deps=['Hello', 'World']))
    creq = (await client.package_request(creq)).content
    assert len(client.object_locks) == 2
    assert client.object_locks['Hello'] == 'cW2'
    assert client.object_locks['World'] == 'cW2'
    assert 'cW2' not in client.object_locks

    c_missing, c_used, c_locked = client.get_dep_locks(cw1_request)
    assert not c_missing
    assert not c_used
    assert set(c_locked) == {'Hello', 'World'}
    with pytest.raises(DependencyException):
        client.sequence_command_local(cw1_request.command)

    sw1_request = CommandRequestObject(SampleCommand('sW1', deps=['Hello']))

    # Server gets client request
    sresp = (await server.parse_handle_request(creq)).content
    assert server.object_locks['Hello'] == LOCK_EXPIRED
    assert server.object_locks['World'] == LOCK_EXPIRED
    assert server.object_locks['cW2'] == LOCK_AVAILABLE

    s_missing, s_used, s_locked = server.get_dep_locks(sw1_request)
    assert not s_missing
    assert s_used == ['Hello']
    assert not s_locked
    with pytest.raises(DependencyException):
        server.sequence_command_local(sw1_request.command)

    assert await client.parse_handle_response(sresp)
    assert client.object_locks['Hello'] == LOCK_EXPIRED
    assert client.object_locks['World'] == LOCK_EXPIRED
    assert server.object_locks['cW2'] == LOCK_AVAILABLE

    sw2_request = CommandRequestObject(SampleCommand('sW1', deps=['Hello', 'cW2', 'not_exist3']))
    s_missing, s_used, s_locked = server.get_dep_locks(sw2_request)
    assert s_missing == ['not_exist3']
    assert s_used == ['Hello']
    assert not s_locked
    with pytest.raises(DependencyException):
        server.sequence_command_local(sw2_request.command)
