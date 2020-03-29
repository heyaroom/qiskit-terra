# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
===========================================
Circuit Scheduler (:mod:`qiskit.scheduler`)
===========================================

.. currentmodule:: qiskit.scheduler

..deprecated:: 0.13

A scheduler compiles a circuit program to a pulse program.

This module has been moved to :mod:`qiskit.pulse.circuit_scheduler`.

.. autosummary::
   :toctree: ../stubs/

   schedule_circuit
   ScheduleConfig

Scheduling utility functions

.. autosummary::
   :toctree: ../stubs/

   qiskit.scheduler.utils

.. automodule:: qiskit.scheduler.methods
"""
import warnings

from .config import ScheduleConfig
from .schedule_circuit import schedule_circuit
from .utils import measure, measure_all

warnings.warn('The scheduler module has been moved to '
              '"qiskit.pulse.circuit_scheduler".', DeprecationWarning)
