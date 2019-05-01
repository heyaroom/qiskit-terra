# -*- coding: utf-8 -*-

# Copyright 2019, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""
Command definition module. Relates circuit gates to pulse commands.
"""
from typing import List, Tuple, Iterable, Union, Dict

from qiskit.exceptions import QiskitError
from qiskit.qobj import PulseQobjInstruction
from qiskit.qobj.converters import QobjToInstructionConverter

from .commands import SamplePulse

from .exceptions import PulseError
from .schedule import Schedule, ParameterizedSchedule


def _to_qubit_tuple(qubit_tuple: Union[int, Iterable[int]]):
    """Convert argument to tuple.
    Args:
        qubit_tuple: Qubits to enforce as tuple.

    Returns:
        tuple
    """
    try:
        qubit_tuple = tuple(qubit_tuple)
    except TypeError:
        qubit_tuple = (qubit_tuple,)

    if not all(isinstance(i, int) for i in qubit_tuple):
        raise QiskitError("All qubits must be integers.")

    return qubit_tuple


class CmdDef:
    """Command definition class.
    Relates `Gate`s to `PulseSchedule`s.
    """

    def __init__(self, schedules: Dict = None):
        """Create command definition from backend.

        Args:
            dict: Keys are tuples of (cmd_name, *qubits) and values are
                `Schedule` or `ParameterizedSchedule`
        """
        self._cmd_dict = {}

        if schedules:
            for key, schedule in schedules.items():
                self.add(key[0], key[1:], schedule)

    @classmethod
    def from_defaults(cls, flat_cmd_def: List[PulseQobjInstruction],
                      pulse_library: Dict[str, SamplePulse]) -> 'CmdDef':
        """Create command definition from backend defaults output.
        Args:
            flat_cmd_def: Command definition list returned by backend
            pulse_library: Dictionary of `SamplePulse`s
        """
        converter = QobjToInstructionConverter(pulse_library, buffer=0)
        cmd_def = cls()

        for cmd in flat_cmd_def:
            qubits = cmd.qubits
            name = cmd.name
            instructions = []
            for instr in cmd.sequence:
                instructions.append(converter(instr))

            cmd_def.add(name, qubits, ParameterizedSchedule(*instructions, name=name))

        return cmd_def

    def add(self, cmd_name: str, qubits: Union[int, Iterable[int]],
            schedule: Union[ParameterizedSchedule, Schedule]):
        """Add a command to the `CommandDefinition`

        Args:
            cmd_name: Name of the command
            qubits: Qubits command applies to
            schedule: Schedule to be added
        """
        qubits = _to_qubit_tuple(qubits)
        cmd_dict = self._cmd_dict.setdefault(cmd_name, {})
        if isinstance(schedule, Schedule):
            schedule = ParameterizedSchedule(schedule, name=schedule.name)
        cmd_dict[qubits] = schedule

    def has(self, cmd_name: str, qubits: Union[int, Iterable[int]]) -> bool:
        """Has command of name with qubits.

        Args:
            cmd_name: Name of the command
            qubits: Ordered list of qubits command applies to
        """
        qubits = _to_qubit_tuple(qubits)
        if cmd_name in self._cmd_dict:

            if qubits in self._cmd_dict[cmd_name]:
                return True

        return False

    def get(self, cmd_name: str, qubits: Union[int, Iterable[int]],
            **params: Dict[str, Union[float, complex]]) -> Schedule:
        """Get command from command definition.
        Args:
            cmd_name: Name of the command
            qubits: Ordered list of qubits command applies to
            **params: Command parameters to be used to generate schedule

        Raises:
            PulseError
        """
        qubits = _to_qubit_tuple(qubits)
        if self.has(cmd_name, qubits):
            schedule = self._cmd_dict[cmd_name][qubits]

            if isinstance(schedule, ParameterizedSchedule):
                return schedule.bind_parameters(**params)

            return schedule.flatten()

        else:
            raise PulseError('Command {0} for qubits {1} is not present'
                             'in CmdDef'.format(cmd_name, qubits))

    def get_parameters(self, cmd_name: str, qubits: Union[int, Iterable[int]]) -> Tuple[str]:
        """Get command parameters from command definition.
        Args:
            cmd_name: Name of the command
            qubits: Ordered list of qubits command applies to

        Raises:
            PulseError
        """
        qubits = _to_qubit_tuple(qubits)
        if self.has(cmd_name, qubits):
            schedule = self._cmd_dict[cmd_name][qubits]
            return schedule.parameters

        else:
            raise PulseError('Command {0} for qubits {1} is not present'
                             'in CmdDef'.format(cmd_name, qubits))

    def pop(self, cmd_name: str, qubits: Union[int, Iterable[int]],
            **params: Dict[str, Union[float, complex]]) -> Schedule:
        """Pop command from command definition.

        Args:
            cmd_name (str): Name of the command
            qubits (int, list or tuple): Ordered list of qubits command applies to
            **params: Command parameters to be used to generate schedule
        """
        qubits = _to_qubit_tuple(qubits)
        if self.has(cmd_name, qubits):
            cmd_dict = self._cmd_dict[cmd_name]
            schedule = cmd_dict.pop(qubits)

            if isinstance(schedule, ParameterizedSchedule):
                return schedule.bind_parameters(**params)

            return schedule

        else:
            raise PulseError('Command {0} for qubits {1} is not present'
                             'in CmdDef'.format(name=cmd_name, qubits=qubits))

    def cmds(self) -> List[str]:
        """Return all command names available in CmdDef."""

        return list(self._cmd_dict.keys())

    def cmd_qubits(self, cmd_name: str) -> List[Tuple[int]]:
        """Get all qubit orderings this command exists for."""
        if cmd_name in self._cmd_dict:
            return list(self._cmd_dict.keys())

        raise PulseError('Command %s does not exist in CmdDef.' % cmd_name)

    def __repr__(self):
        return repr(self._cmd_dict)
