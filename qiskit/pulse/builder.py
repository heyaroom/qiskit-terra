# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Context based pulse programming interface.

Use the context builder interface to program pulse programs with assembly-like
syntax. For example::

.. code-block:: python

    import math

    import qiskit.pulse as pulse
    import qiskit.pulse.pulse_lib as pulse_lib
    from qiskit.test.mock import FakeOpenPulse2Q
    from qiskit import QuantumCircuit

    backend = FakeOpenPulse2Q()

    with build(backend) as pulse_prog:
        # Create a pulse.
        gaussian_pulse = pulse_lib.gaussian(10, 1.0, 2)
        # Create a channel type.
        d0 = drive_channel(0)
        d1 = drive_channel(1)
        # Play a pulse at t=0,
        play(gaussian_pulse, d0)
        # Play another pulse directly after at t=10,
        play(gaussian_pulse, d0)
        # The default scheduling behavior is to schedule pulse in parallel
        # across independent resources. For example, this
        # plays the same pulse on a different channel at t=0.
        play(gaussian_pulse, d1)

        # We also provide alignment contexts.
        # This context starts at t=10 due to earlier pulses.
        with align_sequential():
            play(gaussian_pulse, d0)
            # Play another pulse after at t=20.
            play(gaussian_pulse, d1)

            # We can also layer contexts as each instruction is
            # contained in its local scheduling context.
            # The output of a child context is a
            # fixed scheduled block in its parent context.

            # Context starts at t=30.
            with align_left():
                # Start at t=30.
                play(gaussian_pulse, d0)
                # Start at t=30.
                play(gaussian_pulse, d1)
            # Context ends at t=40.

            # We also support different alignment contexts.
            # The default is,
            # with align_left():

            # Alignment context where all pulse instructions are
            # aligned to the right at their end.
            with align_right():
                shift_phase(math.pi, d1)
                # Starts at t=40.
                delay(100, d0)
                # Ends at t=140.

                # Starts at t=130.
                play(gaussian_pulse, d1)
                # Ends at t=140.

            # Acquire a qubit.
            acquire(100, 0, pulse.MemorySlot(0))

            # We also support a variety of helper functions for common operations.

            # Measure all qubits.
            measure_all()

            # Delay on a qubit.
            # This requires knowledge of which channels belong to which qubits.
            delay_qubits(100, 0)

            # Call a quantum circuit. This functions by behind the scene by calling
            # the scheduler on the given quantum circuit to output a new schedule.
            # NOTE: assumes quantum registers correspond to physical qubit indices.
            qc = QuantumCircuit(2, 2)
            qc.cx(0, 1)
            call(qc)
            # We will also support decomposing a small set of standard gates
            # to pulse schedules.
            u3(0, np.pi, 0, 0)
            cx(0, 1)


            # It is also be possible to call a preexisting
            # schedule constructed with another
            # NOTE: once internals are fleshed out, Schedule may not be the default class
            tmp_sched = Schedule()
            tmp_sched += pulse.Play(gaussian_pulse, d0)
            call(tmp_sched)

            # We also support:

            # frequency instructions
            set_frequency(5.0e9, d0)

            # phase instructions
            shift_phase(0.1, d0)

            # offset contexts
            with phase_offset(math.pi, d0):
                play(gaussian_pulse, d0)

    # execute on real backend
    # qiskit.execute(pulse_prog, backend)

.. warning:: The pulse builder interface is still in development and is
subject to change.
"""
import collections
import contextvars
import functools
import itertools
from contextlib import contextmanager
from typing import (Any,
                    Callable,
                    ContextManager,
                    Dict,
                    Iterable,
                    List,
                    Mapping,
                    Optional,
                    Set,
                    Tuple,
                    TypeVar,
                    Union)

import numpy as np

from qiskit import circuit
from qiskit.circuit.library import standard_gates as gates
from qiskit.pulse import channels
from qiskit.pulse import configuration
from qiskit.pulse import exceptions
from qiskit.pulse import instructions
from qiskit.pulse import macros
from qiskit.pulse import pulse_lib
from qiskit.pulse import transforms
from qiskit.pulse.instructions import directives
from qiskit.pulse.schedule import Schedule

__all__ = [
    "build",
    "active_backend",
    "append_block",
    "append_instruction",
    "num_qubits",
    "qubit_channels",
    "active_transpiler_settings",
    "active_circuit_scheduler_settings",
    "align_left",
    "align_right",
    "align_sequential",
    "group",
    "inline",
    "pad",
    "transpiler_settings",
    "circuit_scheduler_settings",
    "phase_offset",
    "frequency_offset",
    "drive_channel",
    "measure_channel",
    "acquire_channel",
    "control_channel",
    "delay",
    "play",
    "acquire",
    "set_frequency",
    "shift_frequency",
    "set_phase",
    "shift_phase",
    "snapshot",
    "call_schedule",
    "call_circuit",
    "call",
    "barrier",
    "measure",
    "measure_all",
    "delay_qubits",
    "call_gate",
    "cx",
    "u1",
    "u2",
    "u3",
    "x",
]

#: contextvars.ContextVar[BuilderContext]: active builder
BUILDER_CONTEXTVAR = contextvars.ContextVar("backend")

T = TypeVar('T')  # pylint: disable=invalid-name


def _compile_lazy_circuit_before(function: Callable[..., T]
                                 ) -> Callable[..., T]:
    """Decorator thats schedules and calls the lazily compiled circuit before
    executing the decorated builder method."""
    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):
        self._compile_lazy_circuit()
        return function(self, *args, **kwargs)
    return wrapper


class BackendNotSet(exceptions.PulseError):
    """Raised if the builder context does not have a backend."""


def _requires_backend(function: Callable[..., T]) -> Callable[..., T]:
    """Decorator a function to raise if it is called without a builder with a
    set backend.
    """
    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):
        if self.backend is None:
            raise BackendNotSet(
                'This function requires the builder to '
                'have a "backend" set.')
        return function(self, *args, **kwargs)
    return wrapper


class _PulseBuilder():
    """Builder context class."""

    def __init__(self,
                 backend=None,
                 schedule: Optional[Schedule] = None,
                 default_alignment: Union[str, Callable] = 'left',
                 default_transpiler_settings: Mapping = None,
                 default_circuit_scheduler_settings: Mapping = None):
        """Initialize the builder context.

        .. note:: At some point we may consider incorpating the builder into
            the :class:`~qiskit.pulse.Schedule` class. However, the risk of
            this is tying the user interface to the intermediate
            representation. For now we avoid this at the cost of some code
            duplication.

        Args:
            backend (BaseBackend): Input backend to use in builder. If not set
                certain functionality will be unavailable.
            schedule: Initital schedule block to build off. If not supplied
                a schedule will be created.
            default_alignment: Default scheduling alignment policy for the
                builder. One of 'left', 'right', 'sequential' or an alignment
                contextmanager.
            default_transpiler_settings: Default settings for the transpiler.
            default_circuit_scheduler_settings: Default settings for the
                circuit to pulse scheduler.
        """
        #: BaseBackend: Backend instance for context builder.
        self._backend = backend

        #: Union[None, ContextVar]: Token for this ``_PulseBuilder``'s ``ContextVar``.
        self._backend_ctx_token = None

        #: pulse.Schedule: Active schedule of BuilderContext.
        self._block = None

        #: QuantumCircuit: Lazily constructed quantum circuit
        self._lazy_circuit = None

        # ContextManager: Default alignment context.
        self._default_alignment_context = _align(default_alignment)

        if default_transpiler_settings is None:
            default_transpiler_settings = {}
        self._transpiler_settings = default_transpiler_settings

        if default_circuit_scheduler_settings is None:
            default_circuit_scheduler_settings = {}
        self._circuit_scheduler_settings = default_circuit_scheduler_settings

        if schedule is None:
            schedule = Schedule()
        # pulse.Schedule: Root program block
        self._schedule = schedule

        self.set_active_block(Schedule())

    def __enter__(self) -> Schedule:
        """Enter this builder context and yield either the supplied schedule
        or the schedule created for the user.

        Returns:
            The schedule that the builder will build on.
        """
        self._backend_ctx_token = BUILDER_CONTEXTVAR.set(self)
        self._default_alignment_context.__enter__()
        return self._schedule

    @_compile_lazy_circuit_before
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the builder context and compile the built pulse program."""
        self._default_alignment_context.__exit__(exc_type, exc_val, exc_tb)
        self.compile()
        BUILDER_CONTEXTVAR.reset(self._backend_ctx_token)

    @property
    def backend(self):
        """Returns the builder backend if set.

        Returns:
            Optional[BaseBackend]: The builder's backend.
        """
        return self._backend

    @property
    def block(self) -> Schedule:
        """Return the active block of this bulder."""
        return self._block

    @property
    @_requires_backend
    def num_qubits(self):
        """Get the number of qubits in the backend."""
        return self.backend.configuration().n_qubits

    @property
    def transpiler_settings(self) -> Mapping:
        """The builder's transpiler settings."""
        return self._transpiler_settings

    @transpiler_settings.setter
    @_compile_lazy_circuit_before
    def transpiler_settings(self, settings: Mapping):
        self._compile_lazy_circuit()
        self._transpiler_settings = settings

    @property
    def circuit_scheduler_settings(self) -> Mapping:
        """The builder's circuit to pulse scheduler settings."""
        return self._circuit_scheduler_settings

    @circuit_scheduler_settings.setter
    @_compile_lazy_circuit_before
    def circuit_scheduler_settings(self, settings: Mapping):
        self._compile_lazy_circuit()
        self._circuit_scheduler_settings = settings

    @_compile_lazy_circuit_before
    def compile(self) -> Schedule:
        """Compile and output the built pulse program."""
        # Not much happens because we currently compile as we build.
        # This should be offloaded to a true compilation module
        # once we define a more sophisticated IR.
        built_program = transforms.remove_directives(self.block)
        program = self._schedule.append(built_program, mutate=True)
        self.set_active_block(Schedule())
        return program

    @_compile_lazy_circuit_before
    def set_active_block(self, block: Schedule):
        """Set the active block for the builder."""
        assert isinstance(block, Schedule)
        self._block = block

    @_compile_lazy_circuit_before
    def append_block(self, block: Schedule):
        """Add a block to the active block."""
        self.block.append(block, mutate=True)

    @_compile_lazy_circuit_before
    def append_instruction(self, instruction: instructions.Instruction):
        """Add an instruction to the active block."""
        self.block.append(instruction, mutate=True)

    def _compile_lazy_circuit(self):
        """Call a QuantumCircuit and append the output pulse schedule
        to the active block."""
        # check by length, can't check if QuantumCircuit is None
        # so disable pylint error.
        if self._lazy_circuit:
            import qiskit.compiler as compiler  # pylint: disable=cyclic-import

            lazy_circuit = self._lazy_circuit
            # reset lazy circuit
            self._lazy_circuit = self.new_circuit()
            transpiled_circuit = compiler.transpile(lazy_circuit,
                                                    self.backend,
                                                    **self.transpiler_settings)
            sched = compiler.schedule(transpiled_circuit,
                                      self.backend,
                                      **self.circuit_scheduler_settings)
            self.call_schedule(sched)

    def call_schedule(self, schedule: Schedule):
        """Call a schedule and append to the active block."""
        self.append_block(schedule)

    def new_circuit(self):
        """Create a new circuit for lazy circuit scheduling."""
        return circuit.QuantumCircuit(self.num_qubits)

    @_requires_backend
    def call_circuit(self,
                     circ: circuit.QuantumCircuit,
                     lazy: bool = True):
        """Call a circuit in the pulse program.

        The circuit is assumed to be defined on physical qubits.

        If ``lazy == True`` this circuit will extend a lazily constructed
        quantum circuit. When an operation occurs that breaks the underlying
        circuit scheduling assumptions such as adding a pulse instruction or
        changing the alignment context the circuit will be
        transpiled and scheduled into pulses with the current active settings.

        Args:
            circ: Circuit to call.
            lazy: If false the circuit will be transpiled and pulse scheduled
                immediately. Otherwise, it will extend the active lazy circuit
                as defined above.
        """
        if self._lazy_circuit is None:
            self._lazy_circuit = self.new_circuit()

        if lazy:
            self._lazy_circuit.extend(circ)
        else:
            self._compile_lazy_circuit()
            self._lazy_circuit.extend(circ)
            self._compile_lazy_circuit()

    @_requires_backend
    def call_gate(self,
                  gate: circuit.Gate,
                  qubits: Tuple[int, ...],
                  lazy: bool = True):
        """Call the circuit ``gate`` in the pulse program.

        The qubits are assumed to be defined on physical qubits.

        If ``lazy == True`` this circuit will extend a lazily constructed
        quantum circuit. When an operation occurs that breaks the underlying
        circuit scheduling assumptions such as adding a pulse instruction or
        changing the alignment context the circuit will be
        transpiled and scheduled into pulses with the current active settings.

        Args:
            gate: Gate to call.
            qubits: Qubits to call gate on.
            lazy: If false the circuit will be transpiled and pulse scheduled
                immediately. Otherwise, it will extend the active lazy circuit
                as defined above.
        """
        try:
            iter(qubits)
        except TypeError:
            qubits = (qubits,)

        qc = circuit.QuantumCircuit(self.num_qubits)
        qc.append(gate, qargs=qubits)
        self.call_circuit(qc, lazy=lazy)


def build(backend=None,
          schedule: Optional[Schedule] = None,
          default_alignment: str = 'left',
          default_transpiler_settings: Optional[Dict[str, Any]] = None,
          default_circuit_scheduler_settings: Optional[Dict[str, Any]] = None
          ) -> ContextManager[Schedule]:
    """A context manager for launching the imperative pulse builder DSL.

    To enter a building context and starting building a pulse program::

    .. code-block:: python
        :emphasize-lines: 9

        from qiskit import execute
        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.play(pulse.Constant(100,0.5), d0)

        # execute on real backend
        # qiskit.execute(pulse_prog, backend)

    Args:
        backend (BaseBackend): A Qiskit backend. If not supplied certain
            builder functionality will be unavailable.
        schedule: a *mutable* pulse Schedule in which your pulse program will
            be built.
        default_alignment: Default scheduling alignment for builder.
            One of ``left``, ``right``, ``sequential`` or an alignment
            contextmanager.
        default_transpiler_settings: Default settings for the transpiler.
        default_circuit_scheduler_settings: Default settings for the
            circuit to pulse scheduler.

    Returns:
        A new builder context which has the active builder inititalized.
    """
    return _PulseBuilder(
        backend=backend,
        schedule=schedule,
        default_alignment=default_alignment,
        default_transpiler_settings=default_transpiler_settings,
        default_circuit_scheduler_settings=default_circuit_scheduler_settings)


# Builder Utilities ############################################################
class NoActiveBuilder(exceptions.PulseError):
    """Raised if no builder context is active."""


def _active_builder() -> _PulseBuilder:
    """Get the active builder in the active context.

    Returns:
        The active active builder in this context.

    Raises:
        NoActiveBuilder: If a pulse builder function is called outside of a
            builder context.
    """
    try:
        return BUILDER_CONTEXTVAR.get()
    except LookupError:
        raise NoActiveBuilder(
            'A Pulse builder function was called outside of '
            'a builder context. Try calling within a builder '
            'context, eg., "with pulse.build() as schedule: ...".')


def active_backend():
    """Get the backend of the currently active builder context.

    Returns:
        BaseBackend: The active backend in the currently active
            builder context.

    Raises:
        exceptions.BackendNotSet: If the builder does not have a backend set.
    """
    builder = _active_builder().backend
    if builder is None:
        raise BackendNotSet(
            'This function requires the active builder to '
            'have a "backend" set.')
    return builder


def append_block(block: Schedule):
    """Call a block by appending to the active builder block."""
    _active_builder().append_block(block)


def append_instruction(instruction: instructions.Instruction):
    """Append an instruction to the current active builder context block.

    For example::

    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.append_instruction(pulse.Delay(10, d0))
    """
    _active_builder().append_instruction(instruction)


def num_qubits() -> int:
    """Return number of qubits in currently active backend.

    For example::

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            print(pulse.num_qubits())

    .. note:: Requires the active builder context to have a backend set.
    """
    return active_backend().configuration().n_qubits


def qubit_channels(qubit: int) -> Set[channels.Channel]:
    """Returns the set of channels associated with a qubit.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            print(pulse.qubit_channels(0))

    .. note:: Requires the active builder context to have a backend set.

    .. note:: A channel may still be associated with another qubit in this list
        such as in the case where significant crosstalk exists.

    """
    return set(active_backend().configuration().get_qubit_channels(qubit))


def _qubits_to_channels(*channels_or_qubits: Union[int, channels.Channel]
                        ) -> Set[channels.Channel]:
    """Returns the unique channels of the input qubits."""
    chans = set()
    for channel_or_qubit in channels_or_qubits:
        if isinstance(channel_or_qubit, int):
            chans.union(qubit_channels(channel_or_qubit))
        elif isinstance(channel_or_qubit, channels.Channel):
            chans.add(channel_or_qubit)
        else:
            raise exceptions.PulseError(
                '{} is not a "Channel" or '
                'qubit (integer).'.format(channel_or_qubit))
    return chans


def active_transpiler_settings() -> Dict[str, Any]:
    """Return the current active builder context's transpiler settings.

    .. code-block:: python
        :emphasize-lines: 10

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        transpiler_settings = {'optimization_level': 3}

        with pulse.build(backend,
                         default_transpiler_settings=transpiler_settings):
            print(pulse.active_transpiler_settings())

    """
    return _active_builder().transpiler_settings


# pylint: disable=invalid-name
def active_circuit_scheduler_settings() -> Dict[str, Any]:
    """Return the current active builder context's circuti scheduler settings.

    .. code-block:: python
        :emphasize-lines: 11

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        circuit_scheduler_settings = {'method': 'alap'}

        with pulse.build(
                backend,
                default_circuit_scheduler_settings=circuit_scheduler_settings):
            print(pulse.active_circuit_scheduler_settings())

    """
    return _active_builder().circuit_scheduler_settings


# Contexts ###########################################################
def _transform_context(transform: Callable[[Schedule], Schedule],
                       **transform_kwargs: Any
                       ) -> Callable[..., ContextManager[None]]:
    """A tranform context generator, decorator.

    Decorator accepts a transformation function, and then decorates a new
    ContextManager function.

    When the context is entered it creates a new schedule, sets it as the
    active block and then yields.

    Finally it will reset the initial active block after exiting
    the context and apply the decorated transform function to the
    context Schedule. The output transformed schedule will then be
    appended to the initial active block. This effectively builds a stack
    of active blocks that is automatically collapsed upon exiting the context.

    Args:
        transform: Transform to decorate as context.
        transform_kwargs: Additional override keyword arguments for the
            decorated transform.

    Returns:
        A function that generates a new transformation ``ContextManager``.
    """
    @functools.wraps(transform)
    def wrap(function):  # pylint: disable=unused-argument
        @contextmanager
        def wrapped_transform(*args, **kwargs):
            builder = _active_builder()
            active_block = builder.block
            transform_block = Schedule()
            builder.set_active_block(transform_block)
            try:
                yield
            finally:
                builder._compile_lazy_circuit()
                transformed_block = transform(transform_block,
                                              *args,
                                              **kwargs,
                                              **transform_kwargs)
                builder.set_active_block(active_block)
                builder.append_block(transformed_block)
        return wrapped_transform

    return wrap


@_transform_context(transforms.align_left)
def align_left() -> ContextManager[None]:
    """Left alignment pulse scheduling context.

    Pulse instructions within this context are scheduled as early as possible
    by shifting them left to the earliest available time.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)

        with pulse.build() as pulse_prog:
            with pulse.align_left():
                # this pulse will start at t=0
                pulse.play(pulse.Constant(100, 1.0), d0)
                # this pulse will start at t=0
                pulse.play(pulse.Constant(20, 1.0), d1)

        assert pulse_prog.ch_start_time(d0) == pulse_prog.ch_start_time(d1)
    """


@_transform_context(transforms.align_right)
def align_right() -> ContextManager[None]:
    """Right alignment pulse scheduling context.

    Pulse instructions within this context are scheduled as late as possible
    by shifting them right to the latest available time.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)

        with pulse.build() as pulse_prog:
            with pulse.align_right():
                # this pulse will start at t=0
                pulse.play(pulse.Constant(100, 1.0), d0)
                # this pulse will start at t=80
                pulse.play(pulse.Constant(20, 1.0), d1)

        assert pulse_prog.ch_stop_time(d0) == pulse_prog.ch_stop_time(d1)
    """


@_transform_context(transforms.align_sequential)
def align_sequential() -> ContextManager[None]:
    """Sequential alignment pulse scheduling context.

    Pulse instructions within this context are scheduled sequentially in time
    such that no two instructions will be played at the same time.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)

        with pulse.build() as pulse_prog:
            with pulse.align_sequential():
                # this pulse will start at t=0
                pulse.play(pulse.Constant(100, 1.0), d0)
                # this pulse will also start at t=100
                pulse.play(pulse.Constant(20, 1.0), d1)

        assert pulse_prog.ch_stop_time(d0) == pulse_prog.ch_start_time(d1)
    """


def _align(alignment: str = 'left') -> ContextManager[None]:
    """General alignment context. Used by the :class:`_Builder` to choose the
    default alignment policy.

    Args:
        alignment: Alignment scheduling policy to follow.
            One of "left", "right" or "sequential".

    Returns:
        An alignment context that will schedule the instructions it contains
        according to the selected alignment policy upon exiting the context.

    Raises:
        exceptions.PulseError: If an unsupported alignment context is selected.
    """
    if alignment == 'left':
        return align_left()
    elif alignment == 'right':
        return align_right()
    elif alignment == 'sequential':
        return align_sequential()
    else:
        raise exceptions.PulseError('Alignment "{}" is not '
                                    'supported.'.format(alignment))


@_transform_context(transforms.group)
def group() -> ContextManager[None]:
    """Group the instructions within this context as a :class:`pulse.Schedule`.
    fixing their relative time in parent contexts.

    .. code-block:: python
        :emphasize-lines: 11

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)
        d2 = pulse.DriveChannel(2)

        with pulse.build() as pulse_prog:
            # will be ignored due to internal grouping
            with pulse.align_sequential():
                pulse.play(pulse.Constant(10, 1.0), d0)
                with pulse.group():
                    with pulse.align_left():
                        # this pulse will start at t=10
                        pulse.play(pulse.Constant(100, 1.0), d1)
                        # this pulse will also start at t=10
                        pulse.play(pulse.Constant(20, 1.0), d2)

        assert pulse_prog.ch_start_time(d1) == pulse_prog.ch_start_time(d2)
    """


@contextmanager
def inline() -> ContextManager[None]:
    """Inline all instructions within this context into the parent context,
    inheriting the scheduling policy of the parent context.

    .. code-block:: python
        :emphasize-lines: 11

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)
        d2 = pulse.DriveChannel(2)

        with pulse.build() as pulse_prog:
            # will be ignored due to internal grouping
            with pulse.align_left():
                pulse.play(pulse.Constant(10, 1.0), d0)
                with pulse.inline():
                    with pulse.align_right():
                        # this pulse will start at t=0
                        pulse.play(pulse.Constant(100, 1.0), d1)
                        # this pulse will also start at t=0
                        pulse.play(pulse.Constant(20, 1.0), d2)

        assert (pulse_prog.ch_start_time(d1) ==
                pulse_prog.ch_start_time(d2) ==
                pulse_prog.ch_start_time(d1))

    .. warning:: This will cause all scheduling directives within this context
        to be ignored.
    """
    builder = _active_builder()
    active_block = builder.block
    transform_block = Schedule()
    builder.set_active_block(transform_block)
    try:
        yield
    finally:
        builder._compile_lazy_circuit()
        builder.set_active_block(active_block)
        for _, instruction in transform_block.instructions:
            append_instruction(instruction)


@_transform_context(transforms.pad, mutate=True)
def pad(*chs: channels.Channel) -> ContextManager[None]:  # pylint: disable=unused-argument
    """Pad all availale timeslots with delays upon exiting context.

    Args:
        chs: Channels to pad with delays. Defaults to all channels in context
            if none are supplied.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)

        with pulse.build() as pulse_prog:
            with pulse.pad():
                with pulse.align_right():
                    # this pulse will start at t=0
                    pulse.play(pulse.Constant(100, 1.0), d0)
                    # this pulse will start at t=80
                    # a delay will be inserted from t=0 to t=80
                    pulse.play(pulse.Constant(20, 1.0), d1)
        assert pulse_prog.ch_start_time(d0) == pulse_prog.ch_start_time(d1)
        assert pulse_prog.ch_stop_time(d0) == pulse_prog.ch_stop_time(d1)
    """


@contextmanager
def transpiler_settings(**settings) -> ContextManager[None]:
    """Set the currently active transpiler settings for this context.

    .. code-block:: python
        :emphasize-lines: 8

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            print(pulse.active_transpiler_settings())
            with pulse.transpiler_settings(optimization_level=3):
                print(pulse.active_transpiler_settings())
    """
    builder = _active_builder()
    curr_transpiler_settings = builder.transpiler_settings
    builder.transpiler_settings = collections.ChainMap(
        settings, curr_transpiler_settings)
    try:
        yield
    finally:
        builder.transpiler_settings = curr_transpiler_settings


@contextmanager
def circuit_scheduler_settings(**settings) -> ContextManager[None]:
    """Set the currently active circuit scheduler settings for this context.

    .. code-block:: python
        :emphasize-lines: 8

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            print(pulse.active_circuit_scheduler_settings())
            with pulse.circuit_scheduler_settings(method='alap'):
                print(pulse.active_circuit_scheduler_settings())
    """
    builder = _active_builder()
    curr_circuit_scheduler_settings = builder.circuit_scheduler_settings
    builder.circuit_scheduler_settings = collections.ChainMap(
        settings, curr_circuit_scheduler_settings)
    try:
        yield
    finally:
        builder.circuit_scheduler_settings = curr_circuit_scheduler_settings


@contextmanager
def phase_offset(phase: float,
                 channel: channels.PulseChannel
                 ) -> ContextManager[None]:
    """Shift the phase of a channel on entry into context and undo on exit.

    .. code-block:: python
        :emphasize-lines: 8

        import math

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build(backend) as pulse_prog:
            with pulse.phase_offset(math.pi, d0):
                pulse.play(pulse.Constant(10, 1.0), d0)

        assert len(pulse_prog.instructions) == 3

    Args:
        phase: Amount of phase offset in radians.
        channel: Channel to offset phase of.

    Yields:
        None
    """
    shift_phase(phase, channel)
    try:
        yield
    finally:
        shift_phase(-phase, channel)


@contextmanager
def frequency_offset(frequency: float,
                     channel: channels.PulseChannel,
                     compensate_phase: bool = False
                     ) -> ContextManager[None]:
    """Shift the frequency of a channel on entry into context and undo on exit.

    .. code-block:: python
        :emphasize-lines: 7, 16

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build(backend) as pulse_prog:
            # shift frequency by 1GHz
            with pulse.frequency_offset(1e9, d0):
                pulse.play(pulse.Constant(10, 1.0), d0)

        assert len(pulse_prog.instructions) == 3

        with pulse.build(backend) as pulse_prog:
            # Shift frequency by 1GHz.
            # Undo accumulated phase in the shifted freqeuncy frame
            # when exiting the context.
            with pulse.frequency_offset(1e9, d0, compensate_phase=True):
                pulse.play(pulse.Constant(10, 1.0), d0)

        assert len(pulse_prog.instructions) == 4

    Args:
        frequency: Amount of frequency offset in Hz.
        channel: Channel to offset phase of.
        compensate_phase: Compensate for accumulated phase in accumulated with
            respect to the channels frame at its initial frequency.

    Yields:
        None
    """
    builder = _active_builder()
    t0 = builder.block.duration
    shift_frequency(channel, frequency)
    try:
        yield
    finally:
        if compensate_phase:
            duration = builder.block.duration - t0
            dt = active_backend().configuration().dt
            accumulated_phase = duration * dt * frequency % (2*np.pi)
            shift_phase(-accumulated_phase, channel)
        shift_frequency(-frequency, channel)


# Types ########################################################################
def drive_channel(qubit: int) -> channels.DriveChannel:
    """Return ``DriveChannel`` for ``qubit`` on the active builder backend.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            assert pulse.drive_channel(0) == pulse.DriveChannel(0)

    .. note:: Requires the active builder context to have a backend set.
    """
    return active_backend().configuration().drive(qubit)


def measure_channel(qubit: int) -> channels.MeasureChannel:
    """Return ``MeasureChannel`` for ``qubit`` on the active builder backend.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            assert pulse.measure_channel(0) == pulse.MeasureChannel(0)

    .. note:: Requires the active builder context to have a backend set.
    """
    return active_backend().configuration().measure(qubit)


def acquire_channel(qubit: int) -> channels.AcquireChannel:
    """Return ``AcquireChannel`` for ``qubit`` on the active builder backend.

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend):
            assert pulse.acquire_channel(0) == pulse.AcquireChannel(0)

    .. note:: Requires the active builder context to have a backend set.
    """
    return active_backend().configuration().acquire(qubit)


def control_channel(*qubits: Iterable[int]) -> List[channels.ControlChannel]:
    """Return ``AcquireChannel`` for ``qubit`` on the active builder backend.

    Return the secondary drive channel for the given qubit -- typically
    utilized for controlling multi-qubit interactions.

    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()
        with pulse.build(backend):
            assert pulse.control_channel(0, 1) == [pulse.ControlChannel(0)]

    .. note:: Requires the active builder context to have a backend set.

    Args:
      qubits: Tuple or list of ordered qubits of the form
        `(control_qubit, target_qubit)`.

    Returns:
        List of control channels associated with the supplied ordered list
        of qubits.
    """
    return active_backend().configuration().control(qubits=qubits)


# Base Instructions ############################################################
def delay(duration: int,
          channel: channels.Channel):
    """Delay on a ``channel`` for a ``duration``.

    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.delay(10, d0)

    Args:
        duration: Number of cycles to delay for on ``channel``.
        channel: Channel to delay on.
    """
    append_instruction(instructions.Delay(duration, channel))


def play(pulse: Union[pulse_lib.Pulse, np.ndarray],
         channel: channels.PulseChannel):
    """Play a ``pulse`` on a ``channel``.

    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.play(pulse.Constant(10, 1.0), d0)

    Args:
        pulse: Pulse to play.
        channel: Channel to play pulse on.
    """

    if not isinstance(pulse, pulse_lib.Pulse):
        pulse = pulse_lib.SamplePulse(pulse)

    append_instruction(instructions.Play(pulse, channel))


def acquire(duration: int,
            qubit_or_channel: Union[int, channels.AcquireChannel],
            register: Union[channels.RegisterSlot, channels.MemorySlot],
            **metadata: Union[configuration.Kernel,
                              configuration.Discriminator]):
    """Acquire for a ``duration`` on a ``channel`` and store the result
    in a ``register``.

    .. code-block:: python
        :emphasize-lines: 6, 10

        from qiskit import pulse

        d0 = pulse.MeasureChannel(0)
        mem0 = pulse.MemorySlot(0)

        with pulse.build() as pulse_prog:
            pulse.acquire(100, d0, mem0)

            # measurement metadata
            kernel = pulse.configuration.Kernel('linear_discriminator')
            pulse.acquire(100, d0, mem0, kernel=kernel)

    .. note:: The type of data acquire will depend on the execution
        ``meas_level``.

    Args:
        duration: Duration to acquire data for
        qubit_or_channel: Either the qubit to acquire data for or the specific
            :class:`~qiskit.pulse.channels.AcquireChannel` to acquire on.
        register: Location to store measured result.
        metadata: Additional metadata for measurement. See
            :class:`~qiskit.pulse.instructions.Acquire` for more information.

    Raises:
        exceptions.PulseError: If the register type is not supported.
    """
    if isinstance(qubit_or_channel, int):
        qubit_or_channel = channels.AcquireChannel(qubit_or_channel)

    if isinstance(register, channels.MemorySlot):
        append_instruction(instructions.Acquire(
            duration, qubit_or_channel, mem_slot=register, **metadata))
    elif isinstance(register, channels.RegisterSlot):
        append_instruction(instructions.Acquire(
            duration, qubit_or_channel, reg_slot=register, **metadata))
    else:
        raise exceptions.PulseError(
            'Register of type: "{}" is not supported'.format(type(register)))


def set_frequency(frequency: float,
                  channel: channels.PulseChannel):
    """Set the ``frequency`` of a pulse ``channel``.

    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.set_frequency(1e9, d0)

    Args:
        frequency: Frequency in Hz to set channel to.
        channel: Channel to set frequency of.
    """
    append_instruction(instructions.SetFrequency(frequency, channel))


def shift_frequency(frequency: float,
                    channel: channels.PulseChannel):
    """Shift the ``frequency`` of a pulse ``channel``.


    .. code-block:: python
        :emphasize-lines: 6

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.shift_frequency(1e9, d0)

    Args:
        frequency: Frequency in Hz to shift channel frequency by.
        channel: Channel to shift frequency of.
    """
    raise NotImplementedError()


def set_phase(phase: float,
              channel: channels.PulseChannel):
    """Set the ``phase`` of a pulse ``channel``.

    .. code-block:: python
        :emphasize-lines: 8

        import math

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.set_phase(math.pi, d0)

    Args:
        phase: Phase in radians to set channel carrier signal to.
        channel: Channel to set phase of.
    """
    raise NotImplementedError()


def shift_phase(phase: float,
                channel: channels.PulseChannel):
    """Shift the ``phase`` of a pulse ``channel``.

    .. code-block:: python
        :emphasize-lines: 8

        import math

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        with pulse.build() as pulse_prog:
            pulse.shift_phase(math.pi, d0)

    Args:
        phase: Phase in radians to shift channel carrier signal by.
        channel: Channel to shift phase of.
    """
    append_instruction(instructions.ShiftPhase(phase, channel))


def snapshot(label: str,
             snapshot_type: str = 'statevector'):
    """Simulator snapshot.

    .. code-block:: python
        :emphasize-lines: 4

        from qiskit import pulse

        with pulse.build() as pulse_prog:
            pulse.snapshot('first', 'statevector')

    Args:
        label: Label for snapshot.
        snapshot_type: Type of snapshot.
    """
    append_instruction(
        instructions.Snapshot(label, snapshot_type=snapshot_type))


def call_schedule(schedule: Schedule):
    """Call a pulse ``schedule`` in the builder context.

    .. code-block:: python
        :emphasize-lines: 9

        from qiskit import pulse

        d0 = pulse.DriveChannel(0)

        sched = pulse.Schedule()
        sched += pulse.Play(pulse.Constant(10, 1.0), d0)

        with pulse.build() as pulse_prog:
            pulse.call_schedule(sched)

        assert pulse_prog == sched

    Args:
        Schedule to call.
    """
    _active_builder().call_schedule(schedule)


def call_circuit(circ: circuit.QuantumCircuit):
    """Call a quantum ``circuit`` within the active builder context.

    .. code-block:: python
        :emphasize-lines: 18, 22

        from qiskit import circuit
        from qiskit import pulse
        from qiskit import schedule
        from qiskit import transpile
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        d0 = pulse.DriveChannel(0)

        qc = circuit.QuantumCircuit(2)
        qc.cx(0, 1)
        qc_transpiled = transpile(qc, optimization_level=3)
        sched = schedule(qc_transpiled, backend)

        with pulse.build(backend) as pulse_prog:
            # with default settings
            pulse.call_circuit(qc)

        with pulse.build(backend) as pulse_prog:
            with pulse.transpiler_settings(optimization_level=3):
                pulse.call_circuit(qc)

        assert pulse_prog == sched

    .. note:: Requires the active builder context to have a backend set.

    Args:
        Circuit to call.
    """
    _active_builder().call_circuit(circ, lazy=True)


def call(target: Union[circuit.QuantumCircuit, Schedule]):
    """Call the ``target`` within the currently active builder context.

    .. code-block:: python
        :emphasize-lines: 15, 16

        from qiskit import circuit
        from qiskit import pulse
        from qiskit import schedule
        from qiskit import transpile
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        qc = circuit.QuantumCircuit(2)
        qc.cx(0, 1)
        qc_transpiled = transpile(qc, optimization_level=3)
        sched = schedule(qc_transpiled, backend)

        with pulse.build(backend) as pulse_prog:
                pulse.call(sched)
                pulse.call(qc)

    Args:
        target: Target circuit or pulse schedule to call.

    Raises:
        exceptions.PulseError: If the input ``target`` type is not supported.
    """
    if isinstance(target, circuit.QuantumCircuit):
        call_circuit(target)
    elif isinstance(target, Schedule):
        call_schedule(target)
    else:
        raise exceptions.PulseError(
            'Target of type "{}" is not supported.'.format(type(target)))


# Directives ###################################################################
def barrier(*channels_or_qubits: Union[channels.Channel, int]):
    """Barrier directive for a set of channels and qubits.

    This directive prevents the compiler from moving instructions across
    the barrier. Consider the case where we want to enforce that one pulse
    happens after another on separate channels, this can be done with::

    .. code-block:: python
        :emphasize-lines: 11

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        d0 = pulse.DriveChannel(0)
        d1 = pulse.DriveChannel(1)

        with pulse.build(backend) as barrier_pulse_prog:
            pulse.play(pulse.Constant(10, 1.0), d0)
            pulse.barrier(d0, d1)
            pulse.play(pulse.Constant(10, 1.0), d1)

    Of course this could have been accomplished with::

    .. code-block:: python

        with pulse.build(backend) as aligned_pulse_prog:
            with pulse.align_sequential():
                pulse.play(pulse.Constant(10, 1.0), d0)
                pulse.play(pulse.Constant(10, 1.0), d1)
        assert barrier_pulse_prog == aligned_pulse_prog

    The barrier allows the pulse compiler to take care of more advanced
    scheduling aligment operations across channels. For example
    in the case where we are calling an outside circuit or schedule and
    want to align a pulse at the end of one call::

    .. code-block:: python
        :emphasize-lines: 9

        import math

        d0 = pulse.DriveChannel(0)

        with pulse.build(backend) as pulse_prog:
            with pulse.align_right():
                pulse.x(1)
                # Barrier qubit 1 and d0.
                pulse.barrier(1, d0)
                # Due to barrier this will play before the gate on qubit 1.
                pulse.play(pulse.Constant(10, 1.0), d0)
                # This will end at the same time as the pulse above due to
                # the barrier.
                pulse.x(1)

    .. note:: Requires the active builder context to have a backend set if
        qubits are barriered on.

    Args:
        channels_or_qubits: Channels or qubits to barrier.
    """
    chans = _qubits_to_channels(*channels_or_qubits)
    append_instruction(directives.RelativeBarrier(*chans))


# Macros #######################################################################
def measure(qubit: int,
            register: Union[channels.MemorySlot, channels.RegisterSlot] = None,
            ) -> Union[channels.MemorySlot, channels.RegisterSlot]:
    """Measure a qubit within the currently active builder context.

    At the pulse level a measurement is composed of both a stimulus pulse and
    an acquisition instruction which tells the systems measurement unit to
    acquire data and process it. We provide this measurement macro to automate
    the process for you, but if desired full control is still available with
    :func:`acquire` and :func:`play`.

    To use the measurement it is as simple as specifying the qubit you wish to
    measure::

    .. code-block:: python
        :emphasize-lines: 10

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        qubit = 0

        with pulse.build(backend) as pulse_prog:
            # Do something to the qubit.
            qubit_drive_chan = pulse.drive_channel(0)
            pulse.play(pulse.Constant(100, 1.0), qubit_drive_chan)
            # Measure the qubit.
            reg = pulse.measure(qubit)

    For now it is not possible to do much with the handle to ``reg`` but in the
    future we will support using this handle to a result register to build
    up ones program. It is also possible to supply this register::

    .. code-block:: python
        :emphasize-lines: 5

        with pulse.build(backend) as pulse_prog:
            pulse.play(pulse.Constant(100, 1.0), qubit_drive_chan)
            # Measure the qubit.
            mem0 = pulse.MemorySlot(0)
            reg = pulse.measure(qubit, mem0)

        assert reg == mem0

    .. note:: Requires the active builder context to have a backend set.

    Args:
        qubit: Physical qubit to measure.
        register: Register to store result in. If not selected the current
            behaviour is to return the :class:`MemorySlot` with the same
            index as ``qubit``. This register will be returned.
    Returns:
        The ``register`` the qubit measurement result will be stored in.
    """
    backend = active_backend()
    if not register:
        register = channels.MemorySlot(qubit)

    measure_sched = macros.measure(
        qubits=[qubit],
        inst_map=backend.defaults().instruction_schedule_map,
        meas_map=backend.configuration().meas_map,
        qubit_mem_slots={register: register})
    call_schedule(measure_sched)

    return register


def measure_all() -> List[channels.MemorySlot]:
    """Measure all qubits within the currently active builder context.

    A simple macro function to measure all of the qubits in the device at the
    same time. This is useful for handling device ``meas_map`` and single
    measurement constraints.

    Example usage::

    .. code-block:: python
        :emphasize-lines: 8

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            # Measure all qubits and return associated registers.
            regs = pulse.measure_all()

    .. note:: Requires the active builder context to have a backend set.

    Returns:
        The ``register``s the qubit measurement results will be stored in.
    """
    backend = active_backend()
    qubits = range(num_qubits())
    registers = [channels.MemorySlot(qubit) for qubit in qubits]
    measure_sched = macros.measure(
        qubits=qubits,
        inst_map=backend.defaults().instruction_schedule_map,
        meas_map=backend.configuration().meas_map,
        qubit_mem_slots={register: register for register in registers})
    call_schedule(measure_sched)

    return registers


def delay_qubits(duration: int,
                 qubits: Union[int, Iterable[int]]):
    r"""Insert delays on all of the :class:`channels.Channel`\s that correspond
    to the input ``qubits`` at the same time.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 8

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            # Measure all qubits and return associated registers.
            regs = pulse.delay_qubits(0, 1)

    .. note:: Requires the active builder context to have a backend set.

    Args:
        duration: Duration to delay for.
        qubits: Physical qubits to delay on. Delays will be inserted based on
            the channels returned by :function:`qubit_channels`.
    """
    try:
        iter(qubits)
    except TypeError:
        qubits = [qubits]

    qubit_chans = set(itertools.chain.from_iterable(qubit_channels(qubit) for
                                                    qubit in qubits))
    with align_left(), group():
        for chan in qubit_chans:
            delay(duration, chan)


# Gate instructions ############################################################
def call_gate(gate: circuit.Gate, qubits: Tuple[int, ...], lazy: bool = True):
    """Call a gate and lazily schedule it to its corresponding
    pulse instruction.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 8

        from qiskit import pulse
        from qiskit.circuit.library import standard_gates as gates
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.call_gate(gates.CXGate(), (0, 1))

    We can see the role of the transpiler in scheduling gates by optimizing
    away two consecutive CNOT gates::

    .. code-block:: python
        :emphasize-lines: 3, 4

        with pulse.build(backend) as pulse_prog:
            with pulse.transpiler_settings(optimization_level=3):
                pulse.call_gate(gates.CXGate(), (0, 1))
                pulse.call_gate(gates.CXGate(), (0, 1))

        assert pulse_prog == pulse.Schedule()

    .. note:: If multiple gates are called in a row they may be optimized by
        the transpiler, depending on the
        :function:`active_transpiler_settings``.

    .. note:: Requires the active builder context to have a backend set.

    Args:
        gate: Circuit gate instance to call.
        qubits: Qubits to call gate on.
        lazy: If ``false`` the gate will be compiled immediately, otherwise
            it will be added onto a lazily evaluated quantum circuit to be
            compiled when the builder is forced to by a circuit assumption
            being broken, such as the inclusion of a pulse instruction or
            new alignment context.
    """
    _active_builder().call_gate(gate, qubits, lazy=lazy)


def cx(control: int, target: int):
    """Call a cx gate on physical qubits.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.cx(0, 1)

    :class:`~qiskit.circuit.library.standard_gates.CXGate`: for the full
    description of the gate.
    """
    call_gate(gates.CXGate(), (control, target))


def u1(theta: float, qubit: int):
    """Call a u1 gate on physical qubits.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 9

        import math

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.u1(math.pi, 1)

    .. seealso::

        :class:`~qiskit.circuit.library.standard_gates.U1Gate`: for the full
        description of the gate.
    """
    call_gate(gates.U1Gate(theta), qubit)


def u2(phi: float, lam: float, qubit: int):
    """Call a u2 gate on physical qubits.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 9

        import math

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.u2(0, math.pi, 1)

    .. seealso::

        :class:`~qiskit.circuit.library.standard_gates.U2Gate`: for the full
        description of the gate.
    """
    call_gate(gates.U2Gate(phi, lam), qubit)


def u3(theta: float, phi: float, lam: float, qubit: int):
    """Call a u3 gate on physical qubits.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 9

        import math

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.u3(math.pi, 0, math.pi, 1)

    .. seealso::

        :class:`~qiskit.circuit.library.standard_gates.U3Gate`: for the full
        description of the gate.
    """
    call_gate(gates.U3Gate(theta, phi, lam), qubit)


def x(qubit: int):
    """Call a x gate on physical qubits.

    Example Usage::

    .. code-block:: python
        :emphasize-lines: 7

        from qiskit import pulse
        from qiskit.test.mock import FakeOpenPulse2Q

        backend = FakeOpenPulse2Q()

        with pulse.build(backend) as pulse_prog:
            pulse.x(0)

    .. seealso::

        :class:`~qiskit.circuit.library.standard_gates.XGate`: for the full
        description of the gate.
    """
    call_gate(gates.XGate(), qubit)
