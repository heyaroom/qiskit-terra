# -*- coding: utf-8 -*-

# Copyright 2019, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""
Base command.
"""
from abc import ABCMeta, abstractmethod

from qiskit.pulse.exceptions import PulseError


class PulseCommand(metaclass=ABCMeta):
    """Super abstract class of command group."""

    pulseIndex = 0

    @abstractmethod
    def __init__(self, duration: int = None, name: str = None):
        """Create new pulse commands.

        Args:
            duration (int): Duration of pulse.
            name (str): Name of pulse command.
        Raises:
            PulseError: when duration is not number of points.
        """
        if isinstance(duration, int):
            self._duration = duration
        else:
            raise PulseError('Pulse duration should be integer.')

        if name:
            self._name = name
        else:
            self._name = 'p%d' % PulseCommand.pulseIndex
            PulseCommand.pulseIndex += 1

    @property
    def duration(self) -> int:
        """Duration of this command. """
        return self._duration

    @property
    def name(self) -> str:
        """Name of this command. """
        return self._name

    def __eq__(self, other):
        """Two PulseCommands are the same if they are of the same type
        and have the same duration and name.

        Args:
            other (PulseCommand): other PulseCommand.

        Returns:
            bool: are self and other equal.
        """
        if type(self) is type(other) and \
                self._duration == other._duration and \
                self._name == other._name:
            return True
        return False

    def __repr__(self):
        return '%s(name=%s, duration=%d)' % (self.__class__.__name__,
                                             self._name, self._duration)
