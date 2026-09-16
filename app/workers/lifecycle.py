"""Signal hooks shared by standalone workers, without importing any dispatcher."""
import signal


def install_stop_signals(loop, stop):
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    return lambda: [signal.signal(sig, handler) for sig, handler in previous.items()]
