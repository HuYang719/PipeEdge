"""
Global monitoring utilities.

Multiple threads may use the same key, but an iteration must start/end within a single thread.
Multithreading has implications, e.g., consider iterations A and B that run in separate threads:
(1) A and B may overlap in time.
(2) A and B may be reported out of order, regardless if order is defined by start or end time.
(3) The window period may expire while A or B are still in-flight or reported out of order.
"""
import logging
import os
import threading
from typing import Union
from pipeedge.monitoring import MonitorContext, MonitorIterationContext

# Environment variables to override parameters
ENV_WINDOW_SIZE: str = "WINDOW_SIZE"
ENV_CSV_FILE_MODE: str = "CSV_FILE_MODE"

_PRINT_FIELDS_INSTANT = True
_PRINT_FIELDS_WINDOW = True
_PRINT_FIELDS_GLOBAL = True
_WINDOW_SIZE = 10
_CSV_FILE_MODE = 'w' # NOTE: will overwrite existing files!

logger = logging.getLogger(__name__)

# this really does need to be a global variable (pylint incorrectly assumes it's a constant)
_monitor_ctx = None # pylint: disable=invalid-name
_monitor_ctx_lock = threading.Lock()

# Per-thread iteration contexts
# key: thread ident, value: dict (key: key, value: MonitorIterationContext)
_thr_ctx = {}

# Locks are only for reporting iterations, there's no thread safety for managing lifecycle or keys
_locks = {}

# User-friendly field names
_work_types = {}
_acc_types = {}


def _log_instant(key):
    logger.info("%s: Instant Time:     %s sec",
                key, _monitor_ctx.get_instant_time_s(key=key))
    logger.info("%s: Instant Rate:     %s microbatches/sec",
                key, _monitor_ctx.get_instant_heartrate(key=key))
    logger.info("%s: Instant Work:     %s %s",
                key, _monitor_ctx.get_instant_work(key=key), _work_types[key])
    logger.info("%s: Instant Perf:     %s %s/sec",
                key, _monitor_ctx.get_instant_perf(key=key), _work_types[key])
    logger.info("%s: Instant Energy:   %s Joules",
                key, _monitor_ctx.get_instant_energy_j(key=key))
    logger.info("%s: Instant Power:    %s Watts",
                key, _monitor_ctx.get_instant_power_w(key=key))
    logger.info("%s: Instant Acc:      %s %s",
                key, _monitor_ctx.get_instant_accuracy(key=key), _acc_types[key])
    logger.info("%s: Instant Acc Rate: %s %s/sec",
                key, _monitor_ctx.get_instant_accuracy_rate(key=key), _acc_types[key])

def _log_window(key):
    logger.info("%s: Window Time:     %s sec",
                key, _monitor_ctx.get_window_time_s(key=key))
    logger.info("%s: Window Rate:     %s microbatches/sec",
                key, _monitor_ctx.get_window_heartrate(key=key))
    logger.info("%s: Window Work:     %s %s",
                key, _monitor_ctx.get_window_work(key=key), _work_types[key])
    logger.info("%s: Window Perf:     %s %s/sec",
                key, _monitor_ctx.get_window_perf(key=key), _work_types[key])
    logger.info("%s: Window Energy:   %s Joules",
                key, _monitor_ctx.get_window_energy_j(key=key))
    logger.info("%s: Window Power:    %s Watts",
                key, _monitor_ctx.get_window_power_w(key=key))
    logger.info("%s: Window Acc:      %s %s",
                key, _monitor_ctx.get_window_accuracy(key=key), _acc_types[key])
    logger.info("%s: Window Acc Rate: %s %s/sec",
                key, _monitor_ctx.get_window_accuracy_rate(key=key), _acc_types[key])

def _log_global(key):
    logger.info("%s: Global Time:     %s sec",
                key, _monitor_ctx.get_global_time_s(key=key))
    logger.info("%s: Global Rate:     %s microbatches/sec",
                key, _monitor_ctx.get_global_heartrate(key=key))
    logger.info("%s: Global Work:     %s %s",
                key, _monitor_ctx.get_global_work(key=key), _work_types[key])
    logger.info("%s: Global Perf:     %s %s/sec",
                key, _monitor_ctx.get_global_perf(key=key), _work_types[key])
    logger.info("%s: Global Energy:   %s Joules",
                key, _monitor_ctx.get_global_energy_j(key=key))
    logger.info("%s: Global Power:    %s Watts",
                key, _monitor_ctx.get_global_power_w(key=key))
    logger.info("%s: Global Acc:      %s %s",
                key, _monitor_ctx.get_global_accuracy(key=key), _acc_types[key])
    logger.info("%s: Global Acc Rate: %s %s/sec",
                key, _monitor_ctx.get_global_accuracy_rate(key=key), _acc_types[key])

def get_window_size() -> int:
    """Get the window size."""
    return int(os.getenv(ENV_WINDOW_SIZE, str(_WINDOW_SIZE)))

def _init(key: str, work_type: str='items', acc_type: str='acc') -> None:
    """Create monitoring context."""
    global _monitor_ctx # pylint: disable=global-statement,invalid-name
    window_size = get_window_size()
    log_name = key + '.csv'
    log_mode = os.getenv(ENV_CSV_FILE_MODE, _CSV_FILE_MODE)
    try:
        _monitor_ctx = MonitorContext(key=key, window_size=window_size, log_name=log_name,
                                      log_mode=log_mode)
        logger.info("Monitoring energy source: %s", _monitor_ctx.energy_source)
    except FileNotFoundError:
        _monitor_ctx = MonitorContext(key=key, window_size=window_size, log_name=log_name,
                                      log_mode=log_mode, energy_lib=None)
        logger.warning("Couldn't find energymon-default library, disabling energy metrics...")
    try:
        _monitor_ctx.open()
    except OSError as e:
        # Usually happens if energymon can't be initialized, e.g., b/c the power/energy sensors
        # aren't available or we don't have permission to access them (which often requires root).
        logger.error("Error code: %d: %s", e.errno, e.strerror)
        logger.warning("Couldn't init monitor context, trying without energy metrics...")
        _monitor_ctx = MonitorContext(key=key, window_size=window_size, log_name=log_name,
                                      log_mode=log_mode, energy_lib=None)
        _monitor_ctx.open()
    _locks[key] = threading.Lock()
    _work_types[key] = work_type
    _acc_types[key] = acc_type

def init(key: str, work_type: str='items', acc_type: str='acc') -> None:
    with _monitor_ctx_lock:
        return _init(key, work_type, acc_type)

def _finish() -> None:
    """Destroy monitoring context."""
    global _monitor_ctx # pylint: disable=global-statement,invalid-name
    if _monitor_ctx is None:
        return
    if _PRINT_FIELDS_GLOBAL:
        for key in _monitor_ctx.keys():
            _log_global(key)
    _monitor_ctx.close()
    _monitor_ctx = None
    _thr_ctx.clear()
    _locks.clear()
    _work_types.clear()
    _acc_types.clear()

def finish() -> None:
    with _monitor_ctx_lock:
        _finish()

def _add_key(key: str, work_type: str='items', acc_type: str='acc') -> None:
    """Add a new key."""
    if _monitor_ctx is None:
        return
    _monitor_ctx.add_heartbeat(key=key, log_name=key+'.csv')
    _locks[key] = threading.Lock()
    _work_types[key] = work_type
    _acc_types[key] = acc_type

def add_key(key: str, work_type: str='items', acc_type: str='acc') -> None:
    with _monitor_ctx_lock:
        _add_key(key, work_type, acc_type)

def _iter_ctx_push(key):
    ident = threading.get_ident()
    if ident not in _thr_ctx:
        _thr_ctx[ident] = {}
    keymap = _thr_ctx[ident]
    if key in keymap:
        # Should only happen if a previous iteration didn't complete (not currently supported).
        # Otherwise, using the key in a reentrant manner would produce incorrect results.
        raise KeyError(f"Thread iteration context already exists for key: {key}")
    keymap[key] = MonitorIterationContext()
    return keymap[key]

def _iter_ctx_pop(key):
    # requires that iteration_start was called first
    ident = threading.get_ident()
    iter_ctx = _thr_ctx[ident].pop(key)
    if len(_thr_ctx[ident]) == 0:
        # clean up
        del _thr_ctx[ident]
    return iter_ctx

def _iteration_start(key: str) -> None:
    """Start an iteration."""
    if _monitor_ctx is None:
        return
    with _locks[key]:
        _monitor_ctx.iteration_start(iter_ctx=_iter_ctx_push(key))

def iteration_start(key: str) -> None:
    with _monitor_ctx_lock:
        _iteration_start(key)

def _iteration(key: str, work: int=1, accuracy: Union[int, float]=0, safe: bool=True) -> None:
    """Complete an iteration."""
    if _monitor_ctx is None:
        return
    with _locks[key]:
        try:
            iter_ctx = _iter_ctx_pop(key)
        except KeyError:
            # Should only happen if `iteration_start()` isn't used.
            # The underlying monitoring API allows this, but the thread safety we provide is lost.
            if safe:
                raise KeyError(f"No thread iteration context for key: {key}") from None
            iter_ctx = None
        _monitor_ctx.iteration(key=key, work=work, accuracy=accuracy, iter_ctx=iter_ctx)
        # tag=0 only if this call was an initial "start", in which case it's not a real iteration
        tag = _monitor_ctx.get_tag(key=key)
        if tag > 0:
            if _PRINT_FIELDS_INSTANT:
                _log_instant(key)
            if _PRINT_FIELDS_WINDOW:
                if tag > 0 and (tag + 1) % _WINDOW_SIZE == 0:
                    _log_window(key)

def iteration(key: str, work: int=1, accuracy: Union[int, float]=0, safe: bool=True) -> None:
    with _monitor_ctx_lock:
        _iteration(key, work, accuracy, safe)
