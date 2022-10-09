"""
``tractor`` testing!!
"""
import sys
import subprocess
import os
import random
import signal
import platform
import time
import inspect
from functools import partial, wraps

import pytest
import trio
import tractor

pytest_plugins = ['pytester']


def tractor_test(fn):
    """
    Use:

    @tractor_test
    async def test_whatever():
        await ...

    If fixtures:

        - ``arb_addr`` (a socket addr tuple where arbiter is listening)
        - ``loglevel`` (logging level passed to tractor internals)
        - ``start_method`` (subprocess spawning backend)

    are defined in the `pytest` fixture space they will be automatically
    injected to tests declaring these funcargs.
    """
    @wraps(fn)
    def wrapper(
        *args,
        loglevel=None,
        arb_addr=None,
        start_method=None,
        **kwargs
    ):
        # __tracebackhide__ = True

        if 'arb_addr' in inspect.signature(fn).parameters:
            # injects test suite fixture value to test as well
            # as `run()`
            kwargs['arb_addr'] = arb_addr

        if 'loglevel' in inspect.signature(fn).parameters:
            # allows test suites to define a 'loglevel' fixture
            # that activates the internal logging
            kwargs['loglevel'] = loglevel

        if start_method is None:
            if platform.system() == "Windows":
                start_method = 'spawn'
            else:
                start_method = 'trio'

        if 'start_method' in inspect.signature(fn).parameters:
            # set of subprocess spawning backends
            kwargs['start_method'] = start_method

        if kwargs:

            # use explicit root actor start

            async def _main():
                async with tractor.open_root_actor(
                    # **kwargs,
                    arbiter_addr=arb_addr,
                    loglevel=loglevel,
                    start_method=start_method,

                    # TODO: only enable when pytest is passed --pdb
                    # debug_mode=True,

                ) as actor:
                    await fn(*args, **kwargs)

            main = _main

        else:
            # use implicit root actor start
            main = partial(fn, *args, **kwargs)

        return trio.run(main)
            # arbiter_addr=arb_addr,
            # loglevel=loglevel,
            # start_method=start_method,
        # )

    return wrapper

_arb_addr = '127.0.0.1', random.randint(1000, 9999)


# Sending signal.SIGINT on subprocess fails on windows. Use CTRL_* alternatives
if platform.system() == 'Windows':
    _KILL_SIGNAL = signal.CTRL_BREAK_EVENT
    _INT_SIGNAL = signal.CTRL_C_EVENT
    _INT_RETURN_CODE = 3221225786
    _PROC_SPAWN_WAIT = 2
else:
    _KILL_SIGNAL = signal.SIGKILL
    _INT_SIGNAL = signal.SIGINT
    _INT_RETURN_CODE = 1 if sys.version_info < (3, 8) else -signal.SIGINT.value
    _PROC_SPAWN_WAIT = 0.6 if sys.version_info < (3, 7) else 0.4


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)


def repodir():
    """Return the abspath to the repo directory.
    """
    dirname = os.path.dirname
    dirpath = os.path.abspath(
        dirname(dirname(os.path.realpath(__file__)))
        )
    return dirpath


def pytest_addoption(parser):
    parser.addoption(
        "--ll", action="store", dest='loglevel',
        default='ERROR', help="logging level to set when testing"
    )

    parser.addoption(
        "--spawn-backend", action="store", dest='spawn_backend',
        default='trio',
        help="Processing spawning backend to use for test run",
    )


def pytest_configure(config):
    backend = config.option.spawn_backend

    if backend == 'mp':
        tractor._spawn.try_set_start_method('spawn')
    elif backend == 'trio':
        tractor._spawn.try_set_start_method(backend)


@pytest.fixture(scope='session', autouse=True)
def loglevel(request):
    orig = tractor.log._default_loglevel
    level = tractor.log._default_loglevel = request.config.option.loglevel
    tractor.log.get_console_log(level)
    yield level
    tractor.log._default_loglevel = orig


@pytest.fixture(scope='session')
def spawn_backend(request):
    return request.config.option.spawn_backend


_ci_env: bool = os.environ.get('CI', False)


@pytest.fixture(scope='session')
def ci_env() -> bool:
    """Detect CI envoirment.
    """
    return _ci_env


@pytest.fixture(scope='session')
def arb_addr():
    return _arb_addr


def pytest_generate_tests(metafunc):
    spawn_backend = metafunc.config.option.spawn_backend
    if not spawn_backend:
        # XXX some weird windows bug with `pytest`?
        spawn_backend = 'mp'
    assert spawn_backend in ('mp', 'trio')

    if 'start_method' in metafunc.fixturenames:
        if spawn_backend == 'mp':
            from multiprocessing import get_all_start_methods
            methods = get_all_start_methods()
            if 'fork' in methods:
                # fork not available on windows, so check before
                # removing XXX: the fork method is in general
                # incompatible with trio's global scheduler state
                methods.remove('fork')
        elif spawn_backend == 'trio':
            methods = ['trio']

        metafunc.parametrize("start_method", methods, scope='module')


def sig_prog(proc, sig):
    "Kill the actor-process with ``sig``."
    proc.send_signal(sig)
    time.sleep(0.1)
    if not proc.poll():
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        proc.send_signal(_KILL_SIGNAL)
    ret = proc.wait()
    assert ret


@pytest.fixture
def daemon(loglevel, testdir, arb_addr):
    """Run a daemon actor as a "remote arbiter".
    """
    if loglevel in ('trace', 'debug'):
        # too much logging will lock up the subproc (smh)
        loglevel = 'info'

    cmdargs = [
        sys.executable, '-c',
        "import tractor; tractor.run_daemon([], arbiter_addr={}, loglevel={})"
        .format(
            arb_addr,
            "'{}'".format(loglevel) if loglevel else None)
    ]
    kwargs = dict()
    if platform.system() == 'Windows':
        # without this, tests hang on windows forever
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = testdir.popen(
        cmdargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    assert not proc.returncode
    time.sleep(_PROC_SPAWN_WAIT)
    yield proc
    sig_prog(proc, _INT_SIGNAL)
