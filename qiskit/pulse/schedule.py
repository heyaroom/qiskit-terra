# -*- coding: utf-8 -*-

# Copyright 2019, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""
Schedule.
"""
import itertools
import logging
from operator import attrgetter
from typing import List, Tuple, Iterable

from qiskit.pulse import ops
from .channels import Channel
from .interfaces import ScheduleComponent
from .timeslots import TimeslotCollection
from .exceptions import PulseError

logger = logging.getLogger(__name__)


class Schedule(ScheduleComponent):
    """Schedule of `ScheduleComponent`s. The composite node of a schedule tree."""

    def __init__(self, *schedules: List[ScheduleComponent],
                 shift: int = 0, name: str = None):
        """Create empty schedule.

        Args:
            schedules: Child Schedules of this parent Schedule
            shift: Time to shift schedule children by
            name: Name of this schedule

        Raises:
            PulseError: If timeslots intercept.
        """
        self._name = name
        self._shift = shift
        try:
            timeslots = []
            for sched in schedules:
                sched_timeslots = sched.timeslots
                if shift:
                    sched_timeslots = sched_timeslots.shift(shift)
                timeslots.append(sched_timeslots.timeslots)

            self._timeslots = TimeslotCollection(*itertools.chain(*timeslots))
            self._children = tuple(schedules)

        except PulseError as ts_err:
            raise PulseError('Child schedules {0} overlap.'.format(schedules)) from ts_err

    @property
    def name(self) -> str:
        return self._name

    @property
    def timeslots(self) -> TimeslotCollection:
        return self._timeslots

    @property
    def duration(self) -> int:
        return self.timeslots.duration

    @property
    def start_time(self) -> int:
        return self.timeslots.start_time

    @property
    def stop_time(self) -> int:
        return self.timeslots.stop_time

    @property
    def channels(self):
        """Returns channels that this schedule uses.

        Returns:
            Tuple
        """
        return self.timeslots.channels

    @property
    def children(self) -> Tuple[ScheduleComponent, ...]:
        return self._children

    def ch_duration(self, *channels: List[Channel]) -> int:
        """Return start time on this schedule or channel.

        Args:
            channels: Supplied channels

        Returns:
            The latest stop time in this collection.
        """
        return self.timeslots.ch_start_time(*channels)

    def ch_start_time(self, *channels: List[Channel]) -> int:
        """Return minimum start time for supplied channels.

        Args:
            channels: Supplied channels

        Returns:
            The latest stop time in this collection.
        """
        return self.timeslots.ch_start_time(*channels)

    def ch_stop_time(self, *channels: List[Channel]) -> int:
        """Return maximum start time for supplied channels.

        Args:
            channels: Supplied channels

        Returns:
            The latest stop time in this collection.
        """
        return self.timeslots.ch_stop_time(*channels)

    def union(self, *schedules: List[ScheduleComponent]) -> 'Schedule':
        """Return a new schedule which is the union of `self` and `schedule`.

        Args:
            schedules: Schedules to be take the union with the parent `Schedule`.
        """
        return ops.union(self, *schedules)

    def shift(self: ScheduleComponent, time: int) -> 'Schedule':
        """Return a new schedule shifted forward by `time`.

        Args:
            time: Time to shift by
        """
        return ops.shift(self, time)

    def insert(self, start_time: int, schedule: ScheduleComponent) -> 'Schedule':
        """Return a new schedule with `schedule` inserted within `self` at `start_time`.

        Args:
            start_time: time to be inserted
            schedule: schedule to be inserted
        """
        return ops.insert(self, start_time, schedule)

    def append(self, schedule: ScheduleComponent) -> 'Schedule':
        """Return a new schedule with `schedule` inserted at the maximum time over
        all channels shared between `self` and `schedule`.

        Args:
            schedule: schedule to be appended
        """
        return ops.append(self, schedule)

    def flatten(self, time: int = 0) -> Iterable[Tuple[int, ScheduleComponent]]:
        """Iterable for flattening Schedule tree.

        Args:
            node: Root of Schedule tree to traverse
            time: Shifted time due to parent
        """
        for child in self.children:
            yield from child.flatten(time + self._shift)

    def __add__(self, schedule: ScheduleComponent) -> 'Schedule':
        """Return a new schedule with `schedule` inserted within `self` at `start_time`."""
        return self.append(schedule)

    def __or__(self, schedule: ScheduleComponent) -> 'Schedule':
        """Return a new schedule which is the union of `self` and `schedule`."""
        return self.union(schedule)

    def __lshift__(self, time: int) -> 'Schedule':
        """Return a new schedule which is shifted forward by `time`."""
        return self.shift(time)

    def __rshift__(self, time: int) -> 'Schedule':
        """Return a new schedule which is shifted backwards by `time`."""
        return self.shift(-time)

    def __repr__(self):
        res = 'Schedule("name=%s", ' % self._name if self._name else 'Schedule('
        res += '%d, ' % self.start_time
        instructions = [repr(child) for child in self.flatten()]
        res += ', '.join([str(i) for i in instructions[:50]])
        if len(instructions) > 50:
            return res + ', ...)'
        return res + ')'
