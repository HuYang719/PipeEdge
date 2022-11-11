"""Quantization utilities."""
import math
from typing import List, Tuple
from .controller import AdaptiveIntegralXupController

class AdaptiveBitwidthPerformanceController(AdaptiveIntegralXupController):
    """
    An adaptive controller that computes bitwidths to meet a data movement performance constraint.

    Models speedup as inversely proportional to bitwidth, normalized to the max bitwidth.
    This model assumes perfect data packing and no other data transfer overhead.
    In reality, packing is imperfect and compression metadata may also need to be sent.

    Parameters
    ----------
    perf_constraint : float
        The performance constraint to satisfy.
    bitwidths : List[int]
        The available bitwidth values.
    bitwidth_start : int
        The bitwidth used prior to the first iteration.

    References
    ----------
    [1] H. Hoffmann, M. Maggio, M. D. Santambrogio, A. Leva and A. Agarwal.
    A generalized software framework for accurate and efficient management of performance goals.
    2013 Proceedings of the International Conference on Embedded Software (EMSOFT). 2013.
    """

    def __init__(self, perf_constraint: float, bitwidths: List[int], bitwidth_start: int):
        self._bitwidths = list(bitwidths) # copy, then sort in reverse
        self._bitwidths.sort(reverse=True)
        self._speedups = [self._bitwidths[0] / b for b in self._bitwidths]
        # Use the parent controller class to compute speedup over max bitwidth baseline.
        u_0 = self._bitwidths[0] / bitwidth_start
        # We could use a performance measurement to estimate `x_hat_0` for the underlying Kalman
        # filter, but there's no real benefit - the filter converges on the first iteration anyway.
        super().__init__(perf_constraint, u_0, u_max=self._speedups[-1])

    def __call__(self, perf_measured: float, window_len: int) -> Tuple[int, int, int]:
        """
        Split a window period between two bitwidths to achieve ``perf_constraint``.

        The number of iterations to spend in a bitwidth may be ``0`` or ``window_len``.

        Parameters
        ----------
        perf_measured : float
            The measured performance.
        window_len : int
            The window length.

        Returns
        -------
        tuple
            Tuple with 3 values: bitwidth #1, bitwidth #2, and the number of iterations to spend
            in bitwidth #1 during the next window period.
        """
        xup_targ = super().__call__(perf_measured)
        idx_slow = max(0, len([s for s in self._speedups if s <= xup_targ]) - 1)
        idx_fast = min(idx_slow + 1, len(self._speedups) - 1)
        xup_slow = self._speedups[idx_slow]
        xup_fast = self._speedups[idx_fast]
        # The time period of the combined rates must equal the time period of the target rate.
        # 1 / target_rate = x / slower_rate + (1 - x) / faster_rate
        # Solve for x:
        if math.isclose(xup_slow, xup_fast):
            _x = 0 # could also be 1.0
        else:
            _x = (xup_slow * (xup_fast - xup_targ)) / (xup_targ * (xup_fast - xup_slow))
        # Num of iterations = x * window_size
        num_iter = round(window_len * _x)
        return (self._bitwidths[idx_slow], self._bitwidths[idx_fast], num_iter)
