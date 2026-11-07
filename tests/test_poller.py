import asyncio
import json

from harness_watch.server import Poller


def test_shared_poller_pushes_changes_once_to_all_and_stops_when_idle():
    async def scenario():
        calls = []
        values = iter([1, 1, 2, 2, 2, 2, 2, 2, 2, 2])

        def fn():
            calls.append(1)
            return {"v": next(values, 2)}

        poller = Poller(fn, interval=0.01)
        poller.subscribe()
        poller.subscribe()
        v1, d1 = await poller.wait_newer(0, timeout=1)
        assert json.loads(d1) == {"v": 1}
        v2, d2 = await poller.wait_newer(v1, timeout=1)
        assert json.loads(d2) == {"v": 2}  # unchanged polls (1 -> 1) produce no new version
        assert v2 == v1 + 1

        # No change: the wait times out and returns the same version.
        v3, _ = await poller.wait_newer(v2, timeout=0.05)
        assert v3 == v2

        poller.unsubscribe()
        poller.unsubscribe()
        await asyncio.sleep(0.05)
        n = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == n  # loop stopped with no pages open

    asyncio.run(scenario())


def test_poller_survives_a_failing_poll():
    async def scenario():
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("ps failed")
            return {"n": min(state["n"], 3)}

        poller = Poller(fn, interval=0.01)
        poller.subscribe()
        v, d = await poller.wait_newer(0, timeout=1)
        while json.loads(d)["n"] < 3:
            v, d = await poller.wait_newer(v, timeout=1)
        assert json.loads(d) == {"n": 3}
        poller.unsubscribe()

    asyncio.run(scenario())
