'''
The hipster way to force SC onto the stdlib's "async": 'infection mode'.

'''
from typing import Optional, Iterable, Union
import asyncio
import builtins
import itertools
import importlib

import pytest
import trio
import tractor
from tractor import (
    to_asyncio,
    RemoteActorError,
)
from tractor.trionics import BroadcastReceiver


async def sleep_and_err(
    sleep_for: float = 0.1,

    # just signature placeholders for compat with
    # ``to_asyncio.open_channel_from()``
    to_trio: Optional[trio.MemorySendChannel] = None,
    from_trio: Optional[asyncio.Queue] = None,

):
    if to_trio:
        to_trio.send_nowait('start')

    await asyncio.sleep(sleep_for)
    assert 0


async def sleep_forever():
    await asyncio.sleep(float('inf'))


async def trio_cancels_single_aio_task():

    # spawn an ``asyncio`` task to run a func and return result
    with trio.move_on_after(.2):
        await tractor.to_asyncio.run_task(sleep_forever)


def test_trio_cancels_aio_on_actor_side(arb_addr):
    '''
    Spawn an infected actor that is cancelled by the ``trio`` side
    task using std cancel scope apis.

    '''
    async def main():
        async with tractor.open_nursery(
            arbiter_addr=arb_addr
        ) as n:
            await n.run_in_actor(
                trio_cancels_single_aio_task,
                infect_asyncio=True,
            )

    trio.run(main)


async def asyncio_actor(

    target: str,
    expect_err: Optional[Exception] = None

) -> None:

    assert tractor.current_actor().is_infected_aio()
    target = globals()[target]

    if '.' in expect_err:
        modpath, _, name = expect_err.rpartition('.')
        mod = importlib.import_module(modpath)
        error_type = getattr(mod, name)

    else:  # toplevel builtin error type
        error_type = builtins.__dict__.get(expect_err)

    try:
        # spawn an ``asyncio`` task to run a func and return result
        await tractor.to_asyncio.run_task(target)

    except BaseException as err:
        if expect_err:
            assert isinstance(err, error_type)

        raise


def test_aio_simple_error(arb_addr):
    '''
    Verify a simple remote asyncio error propagates back through trio
    to the parent actor.


    '''
    async def main():
        async with tractor.open_nursery(
            arbiter_addr=arb_addr
        ) as n:
            await n.run_in_actor(
                asyncio_actor,
                target='sleep_and_err',
                expect_err='AssertionError',
                infect_asyncio=True,
            )

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    err = excinfo.value
    assert isinstance(err, RemoteActorError)
    assert err.type == AssertionError


def test_tractor_cancels_aio(arb_addr):
    '''
    Verify we can cancel a spawned asyncio task gracefully.

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                asyncio_actor,
                target='sleep_forever',
                expect_err='trio.Cancelled',
                infect_asyncio=True,
            )
            # cancel the entire remote runtime
            await portal.cancel_actor()

    trio.run(main)


def test_trio_cancels_aio(arb_addr):
    '''
    Much like the above test with ``tractor.Portal.cancel_actor()``
    except we just use a standard ``trio`` cancellation api.

    '''
    async def main():

        with trio.move_on_after(1):
            # cancel the nursery shortly after boot

            async with tractor.open_nursery() as n:
                await n.run_in_actor(
                    asyncio_actor,
                    target='sleep_forever',
                    expect_err='trio.Cancelled',
                    infect_asyncio=True,
                )

    trio.run(main)


@tractor.context
async def trio_ctx(
    ctx: tractor.Context,
):

    await ctx.started('start')

    # this will block until the ``asyncio`` task sends a "first"
    # message.
    with trio.fail_after(2):
        async with (
            tractor.to_asyncio.open_channel_from(
                sleep_and_err,
            ) as (first, chan),

            trio.open_nursery() as n,
        ):

            assert first == 'start'

            # spawn another asyncio task for the cuck of it.
            n.start_soon(
                tractor.to_asyncio.run_task,
                sleep_forever,
            )
            await trio.sleep_forever()


@pytest.mark.parametrize(
    'parent_cancels', [False, True],
    ids='parent_actor_cancels_child={}'.format
)
def test_context_spawns_aio_task_that_errors(
    arb_addr,
    parent_cancels: bool,
):
    '''
    Verify that spawning a task via an intertask channel ctx mngr that
    errors correctly propagates the error back from the `asyncio`-side
    task.

    '''
    async def main():

        with trio.fail_after(2):
            async with tractor.open_nursery() as n:
                p = await n.start_actor(
                    'aio_daemon',
                    enable_modules=[__name__],
                    infect_asyncio=True,
                    # debug_mode=True,
                    loglevel='cancel',
                )
                async with p.open_context(
                    trio_ctx,
                ) as (ctx, first):

                    assert first == 'start'

                    if parent_cancels:
                        await p.cancel_actor()

                    await trio.sleep_forever()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    err = excinfo.value
    assert isinstance(err, RemoteActorError)
    if parent_cancels:
        assert err.type == trio.Cancelled
    else:
        assert err.type == AssertionError


async def aio_cancel():
    ''''
    Cancel urself boi.

    '''
    await asyncio.sleep(0.5)
    task = asyncio.current_task()

    # cancel and enter sleep
    task.cancel()
    await sleep_forever()


def test_aio_cancelled_from_aio_causes_trio_cancelled(arb_addr):

    async def main():
        async with tractor.open_nursery() as n:
            await n.run_in_actor(
                asyncio_actor,
                target='aio_cancel',
                expect_err='tractor.to_asyncio.AsyncioCancelled',
                infect_asyncio=True,
            )

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # ensure boxed error is correct
    assert excinfo.value.type == to_asyncio.AsyncioCancelled


# TODO: verify open_channel_from will fail on this..
async def no_to_trio_in_args():
    pass


async def push_from_aio_task(

    sequence: Iterable,
    to_trio: trio.abc.SendChannel,
    expect_cancel: False,
    fail_early: bool,

) -> None:

    try:
        # sync caller ctx manager
        to_trio.send_nowait(True)

        for i in sequence:
            print(f'asyncio sending {i}')
            to_trio.send_nowait(i)
            await asyncio.sleep(0.001)

            if i == 50 and fail_early:
                raise Exception

        print('asyncio streamer complete!')

    except asyncio.CancelledError:
        if not expect_cancel:
            pytest.fail("aio task was cancelled unexpectedly")
        raise
    else:
        if expect_cancel:
            pytest.fail("aio task wasn't cancelled as expected!?")


async def stream_from_aio(

    exit_early: bool = False,
    raise_err: bool = False,
    aio_raise_err: bool = False,
    fan_out: bool = False,

) -> None:
    seq = range(100)
    expect = list(seq)

    try:
        pulled = []

        async with to_asyncio.open_channel_from(
            push_from_aio_task,
            sequence=seq,
            expect_cancel=raise_err or exit_early,
            fail_early=aio_raise_err,
        ) as (first, chan):

            assert first is True

            async def consume(
                chan: Union[
                    to_asyncio.LinkedTaskChannel,
                    BroadcastReceiver,
                ],
            ):
                async for value in chan:
                    print(f'trio received {value}')
                    pulled.append(value)

                    if value == 50:
                        if raise_err:
                            raise Exception
                        elif exit_early:
                            break

            if fan_out:
                # start second task that get's the same stream value set.
                async with (

                    # NOTE: this has to come first to avoid
                    # the channel being closed before the nursery
                    # tasks are joined..
                    chan.subscribe() as br,

                    trio.open_nursery() as n,
                ):
                    n.start_soon(consume, br)
                    await consume(chan)

            else:
                await consume(chan)
    finally:

        if (
            not raise_err and
            not exit_early and
            not aio_raise_err
        ):
            if fan_out:
                # we get double the pulled values in the
                # ``.subscribe()`` fan out case.
                doubled = list(itertools.chain(*zip(expect, expect)))
                expect = doubled[:len(pulled)]
                assert list(sorted(pulled)) == expect

            else:
                assert pulled == expect
        else:
            assert not fan_out
            assert pulled == expect[:51]

        print('trio guest mode task completed!')


@pytest.mark.parametrize(
    'fan_out', [False, True],
    ids='fan_out_w_chan_subscribe={}'.format
)
def test_basic_interloop_channel_stream(arb_addr, fan_out):
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                stream_from_aio,
                infect_asyncio=True,
                fan_out=fan_out,
            )
            await portal.result()

    trio.run(main)


# TODO: parametrize the above test and avoid the duplication here?
def test_trio_error_cancels_intertask_chan(arb_addr):
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                stream_from_aio,
                raise_err=True,
                infect_asyncio=True,
            )
            # should trigger remote actor error
            await portal.result()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # ensure boxed error is correct
    assert excinfo.value.type == Exception


def test_trio_closes_early_and_channel_exits(arb_addr):
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                stream_from_aio,
                exit_early=True,
                infect_asyncio=True,
            )
            # should trigger remote actor error
            await portal.result()

    # should be a quiet exit on a simple channel exit
    trio.run(main)


def test_aio_errors_and_channel_propagates_and_closes(arb_addr):
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                stream_from_aio,
                aio_raise_err=True,
                infect_asyncio=True,
            )
            # should trigger remote actor error
            await portal.result()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # ensure boxed error is correct
    assert excinfo.value.type == Exception


@tractor.context
async def trio_to_aio_echo_server(
    ctx: tractor.Context,
):

    async def aio_echo_server(
        to_trio: trio.MemorySendChannel,
        from_trio: asyncio.Queue,
    ) -> None:

        to_trio.send_nowait('start')

        while True:
            msg = await from_trio.get()

            # echo the msg back
            to_trio.send_nowait(msg)

            # if we get the terminate sentinel
            # break the echo loop
            if msg is None:
                print('breaking aio echo loop')
                break

        print('exiting asyncio task')

    async with to_asyncio.open_channel_from(
        aio_echo_server,
    ) as (first, chan):

        assert first == 'start'
        await ctx.started(first)

        async with ctx.open_stream() as stream:

            async for msg in stream:
                print(f'asyncio echoing {msg}')
                await chan.send(msg)

                out = await chan.receive()
                # echo back to parent actor-task
                await stream.send(out)

                if out is None:
                    try:
                        out = await chan.receive()
                    except trio.EndOfChannel:
                        break
                    else:
                        raise RuntimeError('aio channel never stopped?')


@pytest.mark.parametrize(
    'raise_error_mid_stream',
    [False, Exception, KeyboardInterrupt],
    ids='raise_error={}'.format,
)
def test_echoserver_detailed_mechanics(
    arb_addr,
    raise_error_mid_stream,
):

    async def main():
        async with tractor.open_nursery() as n:
            p = await n.start_actor(
                'aio_server',
                enable_modules=[__name__],
                infect_asyncio=True,
            )
            async with p.open_context(
                trio_to_aio_echo_server,
            ) as (ctx, first):

                assert first == 'start'

                async with ctx.open_stream() as stream:
                    for i in range(100):
                        await stream.send(i)
                        out = await stream.receive()
                        assert i == out

                        if raise_error_mid_stream and i == 50:
                            raise raise_error_mid_stream

                    # send terminate msg
                    await stream.send(None)
                    out = await stream.receive()
                    assert out is None

                    if out is None:
                        # ensure the stream is stopped
                        # with trio.fail_after(0.1):
                        try:
                            await stream.receive()
                        except trio.EndOfChannel:
                            pass
                        else:
                            pytest.fail(
                                "stream wasn't stopped after sentinel?!")

            # TODO: the case where this blocks and
            # is cancelled by kbi or out of task cancellation
            await p.cancel_actor()

    if raise_error_mid_stream:
        with pytest.raises(raise_error_mid_stream):
            trio.run(main)

    else:
        trio.run(main)
