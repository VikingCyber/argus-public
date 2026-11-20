import threading
import time

from stream_server import Broadcaster


def test_broadcaster_latest_empty_initial():
    bc = Broadcaster()
    assert bc.latest() is None


def test_broadcaster_update_and_latest():
    bc = Broadcaster()
    bc.update(b"frame1")
    assert bc.latest() == b"frame1"
    bc.update(b"frame2")
    assert bc.latest() == b"frame2"


def test_broadcaster_wait_sees_new_frame():
    bc = Broadcaster()
    bc.update(b"hello")
    jpeg, gen = bc.wait(0, timeout=0.5)
    assert jpeg == b"hello"
    assert gen > 0


def test_broadcaster_wait_timeout_returns_none():
    bc = Broadcaster()
    jpeg, gen = bc.wait(0, timeout=0.1)
    assert jpeg is None
    assert gen == 0


def test_broadcaster_concurrent_updates():
    bc = Broadcaster()
    n_threads = 8
    n_iter = 50
    barrier = threading.Barrier(n_threads)

    def writer():
        barrier.wait()
        for _ in range(n_iter):
            bc.update(b"x")

    threads = [threading.Thread(target=writer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bc.latest() == b"x"


def test_broadcaster_wait_returns_new_generation_only():
    bc = Broadcaster()
    bc.update(b"first")
    _, gen = bc.wait(0, timeout=0.5)
    assert gen == 1

    def later():
        time.sleep(0.1)
        bc.update(b"second")

    threading.Thread(target=later, daemon=True).start()
    jpeg, gen2 = bc.wait(gen, timeout=1.0)
    assert jpeg == b"second"
    assert gen2 == 2