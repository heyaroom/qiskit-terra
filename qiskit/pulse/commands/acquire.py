# -*- coding: utf-8 -*-

# Copyright 2019, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""
Acquire.
"""
from typing import Union, List, Set

from qiskit.pulse.channels import Channel, Qubit, MemorySlot, RegisterSlot
from qiskit.pulse.common.interfaces import Instruction
from qiskit.pulse.common.timeslots import Interval, Timeslot, TimeslotOccupancy
from qiskit.pulse.exceptions import CommandsError
from .meas_opts import Discriminator, Kernel
from .pulse_command import PulseCommand


class Acquire(PulseCommand):
    """Acquire."""

    def __init__(self, duration, discriminator=None, kernel=None):
        """Create new acquire command.

        Args:
            duration (int): Duration of acquisition.
            discriminator (Discriminator): Discriminators to be used
                (from the list of available discriminator) if the measurement level is 2.
            kernel (Kernel): The data structures defining the measurement kernels
                to be used (from the list of available kernels) and set of parameters
                (if applicable) if the measurement level is 1 or 2.

        Raises:
            CommandsError: when invalid discriminator or kernel object is input.
        """

        super().__init__(duration=duration, name='acquire')

        if discriminator:
            if isinstance(discriminator, Discriminator):
                self.discriminator = discriminator
            else:
                raise CommandsError('Invalid discriminator object is specified.')
        else:
            self.discriminator = Discriminator()

        if kernel:
            if isinstance(kernel, Kernel):
                self.kernel = kernel
            else:
                raise CommandsError('Invalid kernel object is specified.')
        else:
            self.kernel = Kernel()

    def __eq__(self, other):
        """Two Acquires are the same if they are of the same type
        and have the same kernel and discriminator.

        Args:
            other (Acquire): Other Acquire

        Returns:
            bool: are self and other equal.
        """
        if type(self) is type(other) and \
                self.kernel == other.kernel and \
                self.discriminator == other.discriminator:
            return True
        return False

    def __repr__(self):
        return '%s(%s, duration=%d, kernel=%s, discriminator=%s)' % \
               (self.__class__.__name__, self.name, self.duration,
                self.kernel.name, self.discriminator.name)

    def __call__(self,
                 qubits: Union[Qubit, List[Qubit]],
                 mem_slots: Union[MemorySlot, List[MemorySlot]],
                 reg_slots: Union[RegisterSlot, List[RegisterSlot]] = None) -> 'AcquireInstruction':
        return AcquireInstruction(self, qubits, mem_slots, reg_slots)


class AcquireInstruction(Instruction):
    """Pulse to acquire measurement result. """

    def __init__(self,
                 command: Acquire,
                 qubits: Union[Qubit, List[Qubit]],
                 mem_slots: Union[MemorySlot, List[MemorySlot]],
                 reg_slots: Union[RegisterSlot, List[RegisterSlot]] = None):
        if isinstance(qubits, Qubit):
            qubits = [qubits]
        if isinstance(mem_slots, MemorySlot):
            mem_slots = [mem_slots]
        if reg_slots:
            if isinstance(reg_slots, RegisterSlot):
                reg_slots = [reg_slots]
            if len(qubits) != len(reg_slots):
                raise CommandsError("#reg_slots must be equals to #qubits")
        else:
            reg_slots = []
        self._command = command
        self._acquire_channels = [q.acquire for q in qubits]
        self._mem_slots = mem_slots
        self._reg_slots = reg_slots
        # TODO: more precise time-slots
        slots = [Timeslot(Interval(0, command.duration), q.acquire) for q in qubits]
        slots.extend([Timeslot(Interval(0, command.duration), mem) for mem in mem_slots])
        self._occupancy = TimeslotOccupancy(slots)

    @property
    def duration(self):
        return self._command.duration

    @property
    def channelset(self) -> Set[Channel]:
        channels = []
        channels.extend(self._acquire_channels)
        channels.extend(self._mem_slots)
        channels.extend(self._reg_slots)
        return {_ for _ in channels}

    @property
    def occupancy(self):
        return self._occupancy

    @property
    def command(self):
        """Acquire command. """
        return self._command

    @property
    def acquire_channels(self):
        """Acquire channels. """
        return self._acquire_channels

    @property
    def mem_slots(self):
        """MemorySlots. """
        return self._mem_slots

    @property
    def reg_slots(self):
        """RegisterSlots. """
        return self._reg_slots

    def __repr__(self):
        return '%s >> #AcquireChannel=%d' % (self._command, len(self._acquire_channels))
