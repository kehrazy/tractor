# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Async context manager primitives with hard ``trio``-aware semantics

'''
from contextlib import asynccontextmanager as acm
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    AsyncIterator,
    Hashable,
    Optional,
    Sequence,
    TypeVar,
)

import trio
from trio_typing import TaskStatus

from ..log import get_logger
from .._state import current_actor


log = get_logger(__name__)

# A regular invariant generic type
T = TypeVar("T")


async def _enter_and_wait(

    mngr: AsyncContextManager[T],
    unwrapped: dict[int, T],
    all_entered: trio.Event,
    parent_exit: trio.Event,

) -> None:
    '''
    Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        unwrapped[id(mngr)] = value

        if all(unwrapped.values()):
            all_entered.set()

        await parent_exit.wait()


@acm
async def gather_contexts(

    mngrs: Sequence[AsyncContextManager[T]],

) -> AsyncGenerator[tuple[T, ...], None]:
    '''
    Concurrently enter a sequence of async context managers, each in
    a separate ``trio`` task and deliver the unwrapped values in the
    same order once all managers have entered. On exit all contexts are
    subsequently and concurrently exited.

    This function is somewhat similar to common usage of
    ``contextlib.AsyncExitStack.enter_async_context()`` (in a loop) in
    combo with ``asyncio.gather()`` except the managers are concurrently
    entered and exited cancellation just works.

    '''
    unwrapped = {}.fromkeys(id(mngr) for mngr in mngrs)

    all_entered = trio.Event()
    parent_exit = trio.Event()

    async with trio.open_nursery() as n:
        for mngr in mngrs:
            n.start_soon(
                _enter_and_wait,
                mngr,
                unwrapped,
                all_entered,
                parent_exit,
            )

        # deliver control once all managers have started up
        await all_entered.wait()

        yield tuple(unwrapped.values())

        # we don't need a try/finally since cancellation will be triggered
        # by the surrounding nursery on error.
        parent_exit.set()


# Per actor task caching helpers.
# Further potential examples of interest:
# https://gist.github.com/njsmith/cf6fc0a97f53865f2c671659c88c1798#file-cache-py-L8

class _Cache:
    '''
    Globally (actor-processs scoped) cached, task access to
    a kept-alive-while-in-use async resource.

    '''
    lock = trio.Lock()
    users: int = 0
    values: dict[Any,  Any] = {}
    resources: dict[
        int,
        tuple[trio.Nursery, trio.Event]
    ] = {}
    no_more_users: Optional[trio.Event] = None

    @classmethod
    async def run_ctx(
        cls,
        mng,
        key,
        task_status: TaskStatus[T] = trio.TASK_STATUS_IGNORED,

    ) -> None:
        async with mng as value:

            _, no_more_users = cls.resources[id(mng)]
            cls.values[key] = value
            task_status.started(value)
            try:
                await no_more_users.wait()
            finally:
                value = cls.values.pop(key)
                # discard nursery ref so it won't be re-used (an error)
                cls.resources.pop(id(mng))


@acm
async def maybe_open_context(

    key: Hashable,
    mngr: AsyncContextManager[T],

) -> AsyncIterator[tuple[bool, T]]:
    '''
    Maybe open a context manager if there is not already a _Cached
    version for the provided ``key``. Return the _Cached instance on
    a _Cache hit.

    '''
    await _Cache.lock.acquire()

    ctx_key = id(mngr)

    value = None
    try:
        # lock feed acquisition around task racing  / ``trio``'s
        # scheduler protocol
        value = _Cache.values[key]
        log.info(f'Reusing _Cached resource for {key}')
        _Cache.users += 1
        _Cache.lock.release()
        yield True, value

    except KeyError:
        log.info(f'Allocating new resource for {key}')

        # **critical section** that should prevent other tasks from
        # checking the _Cache until complete otherwise the scheduler
        # may switch and by accident we create more then one feed.

        # TODO: avoid pulling from ``tractor`` internals and
        # instead offer a "root nursery" in piker actors?
        service_n = current_actor()._service_n

        # TODO: does this need to be a tractor "root nursery"?
        assert not _Cache.resources.get(ctx_key), f'Resource exists? {ctx_key}'
        ln, _ = _Cache.resources[ctx_key] = (service_n, trio.Event())

        value = await ln.start(_Cache.run_ctx, mngr, key)
        _Cache.users += 1
        _Cache.lock.release()

        yield False, value

    finally:
        _Cache.users -= 1

        if value is not None:
            # if no more consumers, teardown the client
            if _Cache.users <= 0:
                log.info(f'De-allocating resource for {key}')

                if _Cache.lock.locked():
                    _Cache.lock.release()

                # terminate mngr nursery
                entry = _Cache.resources.get(ctx_key)
                if entry:
                    _, no_more_users = entry
                    no_more_users.set()
