import tractor
import trio


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    while True:
        yield 'yo'
        await tractor.breakpoint()


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)


async def main():
    """Test breakpoint in a streaming actor.
    """
    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='error',
    ) as n:

        p0 = await n.start_actor('bp_forever', enable_modules=[__name__])
        p1 = await n.start_actor('name_error', enable_modules=[__name__])

        # retreive results
        stream = await p0.run(breakpoint_forever)
        await p1.run(name_error)


if __name__ == '__main__':
    trio.run(main)
