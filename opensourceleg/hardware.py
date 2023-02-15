#!/usr/bin/python3
from typing import Any, Callable, Dict, List, Optional

import collections
import csv
import ctypes as c
import logging
import os
import sys
import threading
import time
import traceback
from enum import Enum
from logging.handlers import RotatingFileHandler
from math import isfinite

import numpy as np
import scipy.signal
from flexsea import fx_enums as fxe
from flexsea.dev_spec import AllDevices as fxd
from flexsea.device import Device

from opensourceleg.utilities import SoftRealtimeLoop

# TODO: Support for TMotor driver with similar structure
# TODO: Support for gRPC servers
# TODO: Event-handler

MOTOR_COUNT_PER_REV = 16384
NM_PER_AMP = 0.1133
NM_PER_MILLIAMP = NM_PER_AMP / 1000
RAD_PER_COUNT = 2 * np.pi / MOTOR_COUNT_PER_REV
RAD_PER_DEG = np.pi / 180
MOTOR_COUNT_TO_RADIANS = lambda x: x * (np.pi / 180.0 / 45.5111)
RADIANS_TO_MOTOR_COUNTS = lambda q: q * (180 * 45.5111 / np.pi)

RAD_PER_SEC_GYROLSB = np.pi / 180 / 32.8
M_PER_SEC_SQUARED_ACCLSB = 9.80665 / 8192


# Global Units Dictionary
ALL_UNITS = {
    "force": {
        "N": 1.0,
        "lbf": 4.4482216152605,
        "kgf": 9.80665,
    },
    "torque": {
        "N-m": 1.0,
        "lbf-in": 0.1129848290276167,
        "lbf-ft": 1.3558179483314004,
        "kgf-cm": 0.0980665,
        "kgf-m": 0.980665,
    },
    "stiffness": {
        "N/rad": 1.0,
        "N/deg": 0.017453292519943295,
        "lbf/rad": 0.224809,
        "lbf/deg": 0.003490659,
        "kgf/rad": 1.8518518518518519,
        "kgf/deg": 0.031746031746031744,
    },
    "damping": {
        "N/(rad/s)": 1.0,
        "N/(deg/s)": 0.017453292519943295,
        "lbf/(rad/s)": 0.224809,
        "lbf/(deg/s)": 0.003490659,
        "kgf/(rad/s)": 1.8518518518518519,
        "kgf/(deg/s)": 0.031746031746031744,
    },
    "length": {
        "m": 1.0,
        "cm": 0.01,
        "in": 0.0254,
        "ft": 0.3048,
    },
    "angle": {
        "rad": 1.0,
        "deg": 0.017453292519943295,
    },
    "mass": {
        "kg": 1.0,
        "g": 0.001,
        "lb": 0.45359237,
    },
    "velocity": {
        "rad/s": 1.0,
        "deg/s": 0.017453292519943295,
        "rpm": 0.10471975511965977,
    },
    "acceleration": {
        "rad/s^2": 1.0,
        "deg/s^2": 0.017453292519943295,
    },
    "time": {
        "s": 1.0,
        "ms": 0.001,
        "us": 0.000001,
    },
    "current": {
        "mA": 1,
        "A": 1000,
    },
    "voltage": {
        "mV": 1,
        "V": 1000,
    },
    "gravity": {
        "m/s^2": 1.0,
        "g": 9.80665,
    },
}


class UnitsDefinition(dict):
    """
    UnitsDefinition class is a dictionary with set and get methods that checks if the keys are valid

    Methods:
        __setitem__(key: str, value: dict) -> None
        __getitem__(key: str) -> dict
        convert(value: float, attribute: str) -> None
    """

    def __setitem__(self, key: str, value: dict) -> None:
        if key not in self:
            raise KeyError(f"Invalid key: {key}")

        if value not in ALL_UNITS[key].keys():
            raise ValueError(f"Invalid unit: {value}")

        super().__setitem__(key, value)

    def __getitem__(self, key: str) -> dict:
        if key not in self:
            raise KeyError(f"Invalid key: {key}")
        return super().__getitem__(key)

    def convert_to_default_units(self, value: float, attribute: str) -> None:
        """
        convert a value from one unit to the default unit

        Args:
            value (float): Value to be converted
            attribute (str): Attribute to be converted

        Returns:
            float: Converted value in the default unit
        """
        return value * ALL_UNITS[attribute][self[attribute]]

    def convert_from_default_units(self, value: float, attribute: str) -> None:
        """
        convert a value from the default unit to another unit

        Args:
            value (float): Value to be converted
            attribute (str): Attribute to be converted

        Returns:
            float: Converted value in the default unit
        """
        return value / ALL_UNITS[attribute][self[attribute]]


DEFAULT_UNITS = UnitsDefinition(
    {
        "force": "N",
        "torque": "N-m",
        "stiffness": "N/rad",
        "damping": "N/(rad/s)",
        "length": "m",
        "angle": "rad",
        "mass": "kg",
        "velocity": "rad/s",
        "acceleration": "rad/s^2",
        "time": "s",
        "current": "mA",
        "voltage": "mV",
        "gravity": "m/s^2",
    }
)


class Gains:
    def __init__(
        self, kp: int = 0, ki: int = 0, kd: int = 0, K: int = 0, B: int = 0, ff: int = 0
    ) -> None:

        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.K = K
        self.B = B
        self.ff = ff

    def __str__(self) -> str:
        return f"kp: {self.kp}, ki: {self.ki}, kd: {self.kd}, K: {self.K}, B: {self.B}, ff: {self.ff}"


DEFAULT_POSITION_GAINS = Gains(kp=200, ki=50, kd=0, K=0, B=0, ff=0)

DEFAULT_CURRENT_GAINS = Gains(kp=40, ki=400, kd=0, K=0, B=0, ff=128)

DEFAULT_IMPEDANCE_GAINS = Gains(kp=40, ki=400, kd=0, K=300, B=1600, ff=128)


class ActpackMode:
    def __init__(self, control_mode: c.c_int, device: "DephyActpack"):
        self._control_mode = control_mode
        self._device = device
        self._entry_callback: callable = lambda: None
        self._exit_callback: callable = lambda: None

        self._has_gains = False

    def __eq__(self, __o: object) -> bool:
        if isinstance(__o, ActpackMode):
            return self._control_mode == __o._control_mode
        return False

    def __str__(self) -> str:
        return str(self._control_mode)

    @property
    def mode(self) -> c.c_int:
        return self._control_mode

    @property
    def has_gains(self) -> bool:
        return self._has_gains

    def enter(self):
        self._entry_callback()

    def exit(self):
        self._exit_callback()

    def transition(self, to_state: "ActpackMode"):
        self.exit()
        to_state.enter()


class VoltageMode(ActpackMode):
    def __init__(self, device: "DephyActpack"):
        super().__init__(fxe.FX_VOLTAGE, device)
        self._entry_callback = self._entry
        self._exit_callback = self._exit

    def _entry(self):
        print("Entering VOLTAGE mode")

    def _exit(self):
        self._set_qaxis_voltage(0)
        print("Exiting VOLTAGE mode")

    def _set_qaxis_voltage(self, voltage: int):
        self._device.send_motor_command(
            self.mode,
            voltage,
        )


class CurrentMode(ActpackMode):
    def __init__(self, device: "DephyActpack"):
        super().__init__(fxe.FX_CURRENT, device)
        self._entry_callback = self._entry
        self._exit_callback = self._exit

    def _entry(self):
        if not self.has_gains:
            self._set_gains()

        self._set_qaxis_current(0)

    def _exit(self):
        self._device.send_motor_command(fxe.FX_VOLTAGE, 0)

    def _set_gains(
        self,
        kp: int = DEFAULT_CURRENT_GAINS.kp,
        ki: int = DEFAULT_CURRENT_GAINS.ki,
        ff: int = DEFAULT_CURRENT_GAINS.ff,
    ):

        assert 0 <= kp <= 80, "kp must be between 0 and 80"
        assert 0 <= ki <= 800, "ki must be between 0 and 800"
        assert 0 <= ff <= 128, "ff must be between 0 and 128"

        self._device.set_gains(kp=kp, ki=ki, kd=0, k=0, b=0, ff=ff)
        self._has_gains = True

    def _set_qaxis_current(self, current: int):
        self._device.send_motor_command(
            self.mode,
            current,
        )


class PositionMode(ActpackMode):
    def __init__(self, device: "DephyActpack"):
        super().__init__(fxe.FX_POSITION, device)
        self._entry_callback = self._entry
        self._exit_callback = self._exit

    def _entry(self):
        if not self.has_gains:
            self._set_gains()

        self._set_motor_angle(
            int(
                self._device._units.convert_to_default_units(
                    self._device.motor_angle, "angle"
                )
                / RAD_PER_COUNT
            )
        )

    def _exit(self):
        self._device.send_motor_command(fxe.FX_VOLTAGE, 0)
        print("Exiting POSITION mode")

    def _set_gains(
        self,
        kp: int = DEFAULT_POSITION_GAINS.kp,
        ki: int = DEFAULT_POSITION_GAINS.ki,
        kd: int = DEFAULT_POSITION_GAINS.kd,
    ):

        assert 0 <= kp <= 1000, "kp must be between 0 and 1000"
        assert 0 <= ki <= 1000, "ki must be between 0 and 1000"
        assert 0 <= kd <= 1000, "kd must be between 0 and 1000"

        self._device.set_gains(kp=kp, ki=ki, kd=kd, k=0, b=0, ff=0)
        self._has_gains = True

    def _set_motor_angle(self, counts: int):
        self._device.send_motor_command(
            self.mode,
            counts,
        )


class ImpedanceMode(ActpackMode):
    def __init__(self, device: "DephyActpack"):
        super().__init__(fxe.FX_IMPEDANCE, device)
        self._entry_callback = self._entry
        self._exit_callback = self._exit

    def _entry(self):
        if not self.has_gains:
            self._set_gains()

        self._set_motor_angle(
            int(
                self._device._units.convert_to_default_units(
                    self._device.motor_angle, "angle"
                )
                / RAD_PER_COUNT
            )
        )

    def _exit(self):
        self._device.send_motor_command(fxe.FX_VOLTAGE, 0)
        print("Exiting IMPEDANCE mode")

    def _set_motor_angle(self, counts: int):
        self._device.send_motor_command(
            self.mode,
            counts,
        )

    def _set_gains(
        self,
        kp: int = DEFAULT_IMPEDANCE_GAINS.kp,
        ki: int = DEFAULT_IMPEDANCE_GAINS.ki,
        K: int = DEFAULT_IMPEDANCE_GAINS.K,
        B: int = DEFAULT_IMPEDANCE_GAINS.B,
        ff: int = DEFAULT_IMPEDANCE_GAINS.ff,
    ):

        assert 0 <= kp <= 80, "kp must be between 0 and 80"
        assert 0 <= ki <= 800, "ki must be between 0 and 800"
        assert 0 <= ff <= 128, "ff must be between 0 and 128"
        assert 0 <= K, "K must be greater than 0"
        assert 0 <= B, "B must be greater than 0"

        self._device.set_gains(kp=kp, ki=ki, kd=0, k=K, b=B, ff=ff)
        self._has_gains = True


class DephyActpack(Device):
    """Class for the Dephy Actpack

    Args:
        Device (_type_): _description_

    Raises:
        KeyError: _description_
        ValueError: _description_
        KeyError: _description_

    Returns:
        _type_: _description_
    """

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        baud_rate: int = 230400,
        frequency: int = 500,
        logger: logging.Logger = None,
        units: UnitsDefinition = None,
        debug_level: int = 0,
        dephy_log: bool = False,
    ) -> None:
        """
        Initializes the Actpack class

        Args:
            port (str): _description_
            baud_rate (int): _description_. Defaults to 230400.
            frequency (int): _description_. Defaults to 500.
            logger (logging.Logger): _description_
            units (UnitsDefinition): _description_
            debug_level (int): _description_. Defaults to 0.
            dephy_log (bool): _description_. Defaults to False.
        """
        super().__init__(os.path.realpath(port), baud_rate, debug_level)
        self._debug_level = debug_level
        self._dephy_log = dephy_log
        self._frequency = frequency
        self._data = fxd.ActPackState()
        self.log = logger
        self._state = None
        self._units = units if units else DEFAULT_UNITS

        self._modes: dict[str, ActpackMode] = {
            "VOLTAGE": VoltageMode(self),
            "POSITION": PositionMode(self),
            "CURRENT": CurrentMode(self),
            "IMPEDANCE": ImpedanceMode(self),
        }

        self._mode: ActpackMode = self._modes["VOLTAGE"]

    def start(self):
        self.open(self._debug_level, log_enabled=self._dephy_log)
        self.start_streaming(self._frequency)
        time.sleep(0.1)
        self._mode.enter()

    def stop(self):
        self.close()

    def update(self):
        self._data = self.read()

    def set_position_gains(self, kp: int, ki: int, kd: int, force: bool = True):
        """
        Sets the position gains

        Args:
            kp (int): The proportional gain
            ki (int): The integral gain
            kd (int): The derivative gain
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not POSITION and force is False
        """
        if self._mode != self._modes["POSITION"]:
            if force:
                self._mode.transition(self._modes["POSITION"])
            else:
                raise ValueError(f"Cannot set position gains in mode {self._mode}")

        self._mode._set_gains(kp=kp, ki=ki, kd=kd)

    def set_current_gains(self, kp: int, ki: int, ff: int, force: bool = True):
        """
        Sets the current gains

        Args:
            kp (int): The proportional gain
            ki (int): The integral gain
            ff (int): The feedforward gain
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not CURRENT and force is False
        """
        if self._mode != self._modes["CURRENT"]:
            if force:
                self._mode.transition(self._modes["CURRENT"])
            else:
                raise ValueError(f"Cannot set current gains in mode {self._mode}")

        self._mode._set_gains(kp=kp, ki=ki, ff=ff)

    def set_impedance_gains(
        self, kp: int, ki: int, K: int, B: int, ff: int, force: bool = True
    ):
        """
        Sets the impedance gains

        Args:
            kp (int): The proportional gain
            ki (int): The integral gain
            K (int): The spring constant
            B (int): The damping constant
            ff (int): The feedforward gain
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not IMPEDANCE and force is False
        """
        if self._mode != self._modes["IMPEDANCE"]:
            if force:
                self._mode.transition(self._modes["IMPEDANCE"])
            else:
                raise ValueError(f"Cannot set impedance gains in mode {self._mode}")

        self._mode._set_gains(kp=kp, ki=ki, K=K, B=B, ff=ff)

    def set_q_axis_voltage(self, value: float, force: bool = False):
        """
        Sets the q axis voltage

        Args:
            value (float): The voltage to set
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not VOLTAGE and force is False

        """
        if self._mode != self._modes["VOLTAGE"]:
            if force:
                self._mode.transition(self._modes["VOLTAGE"])
            else:
                raise ValueError(f"Cannot set q_axis_voltage in mode {self._mode}")

        self._mode._set_qaxis_voltage(
            int(self._units.convert_to_default_units(value, "voltage")),
        )

    def set_q_axis_current(self, value: float, force: bool = False):
        """
        Sets the q axis current

        Args:
            value (float): The current to set
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not CURRENT and force is False
        """
        if self._mode != self._modes["CURRENT"]:
            if force:
                self._mode.transition(self._modes["CURRENT"])
            else:
                raise ValueError(f"Cannot set q_axis_current in mode {self._mode}")

        self._mode._set_qaxis_current(
            int(self._units.convert_to_default_units(value, "current")),
        )

    def set_motor_torque(self, torque: float, force: bool = False):
        """
        Sets the motor torque

        Args:
            torque (float): The torque to set
            force (bool): Force the mode transition. Defaults to False.

        Raises:
            ValueError: If the mode is not CURRENT and force is False
        """
        if self._mode != self._modes["CURRENT"]:
            if force:
                self._mode.transition(self._modes["CURRENT"])
            else:
                raise ValueError(f"Cannot set motor_torque in mode {self._mode}")

        self._mode._set_qaxis_current(
            int(
                self._units.convert_to_default_units(torque, "torque") / NM_PER_MILLIAMP
            ),
        )

    def set_motor_angle(self, angle: float):
        """
        Sets the motor angle

        Args:
            angle (float): The angle to set

        Raises:
            ValueError: If the mode is not POSITION or IMPEDANCE
        """
        if self._mode not in [self._modes["POSITION"], self._modes["IMPEDANCE"]]:
            raise ValueError(f"Cannot set motor angle in mode {self._mode}")

        self._mode._set_motor_angle(
            int(self._units.convert_to_default_units(angle, "angle") / RAD_PER_COUNT),
        )

    # Read only properties from the actpack

    @property
    def units(self):
        return self._units

    @property
    def mode(self):
        return self._mode()

    @property
    def battery_voltage(self):
        return self._units.convert_from_default_units(
            self._data.batt_volt,
            "voltage",
        )

    @property
    def batter_current(self):
        return self._units.convert_from_default_units(
            self._data.batt_curr,
            "current",
        )

    @property
    def q_axis_voltage(self):
        return self._units.convert_from_default_units(
            self._data.mot_volt,
            "voltage",
        )

    @property
    def q_axis_current(self):
        return self._units.convert_from_default_units(
            self._data.mot_cur,
            "current",
        )

    @property
    def motor_torque(self):
        return self._units.convert_from_default_units(
            self._data.mot_cur * NM_PER_MILLIAMP,
            "torque",
        )

    @property
    def motor_angle(self):
        return self._units.convert_from_default_units(
            int(self._data.mot_ang) * RAD_PER_COUNT,
            "angle",
        )

    @property
    def motor_velocity(self):
        return self._units.convert_from_default_units(
            self._data.mot_vel * RAD_PER_DEG,
            "velocity",
        )

    @property
    def motor_acceleration(self):
        return self._units.convert_from_default_units(
            self._data.mot_acc,
            "acceleration",
        )

    @property
    def motor_torque(self):
        return self._units.convert_from_default_units(
            self.q_axis_current * NM_PER_AMP,
            "torque",
        )

    @property
    def joint_angle(self):
        return self._units.convert_from_default_units(
            self._data.ank_ang * RAD_PER_COUNT,
            "angle",
        )

    @property
    def joint_velocity(self):
        return self._units.convert_from_default_units(
            self._data.ank_vel * RAD_PER_COUNT,
            "velocity",
        )

    @property
    def genvars(self):
        return np.array(
            [
                self._data.genvar_0,
                self._data.genvar_1,
                self._data.genvar_2,
                self._data.genvar_3,
                self._data.genvar_4,
                self._data.genvar_5,
            ]
        )

    @property
    def acc_x(self):
        return self._units.convert_from_default_units(
            self._data.accelx * M_PER_SEC_SQUARED_ACCLSB,
            "gravity",
        )

    @property
    def acc_y(self):
        return self._units.convert_from_default_units(
            self._data.accely * M_PER_SEC_SQUARED_ACCLSB,
            "gravity",
        )

    @property
    def acc_z(self):
        return self._units.convert_from_default_units(
            self._data.accelz * M_PER_SEC_SQUARED_ACCLSB,
            "gravity",
        )

    @property
    def gyro_x(self):
        return self._units.convert_from_default_units(
            self._data.gyrox * RAD_PER_SEC_GYROLSB,
            "velocity",
        )

    @property
    def gyro_y(self):
        return self._units.convert_from_default_units(
            self._data.gyroy * RAD_PER_SEC_GYROLSB,
            "velocity",
        )

    @property
    def gyro_z(self):
        return self._units.convert_from_default_units(
            self._data.gyroz * RAD_PER_SEC_GYROLSB,
            "velocity",
        )


# class Joint(Actpack):
#     def __init__(
#         self, name, fxs, port, baud_rate, frequency, logger, debug_level=0
#     ) -> None:
#         super().__init__(fxs, port, baud_rate, frequency, logger, debug_level)

#         self._name = name
#         self._filename = "./encoder_map_" + self._name + ".txt"

#         self._count2deg = 360 / 2**14
#         self._joint_angle_array = None
#         self._motor_count_array = None

#         self._state = JointState.NEUTRAL

#         self._k: int = 0
#         self._b: int = 0
#         self._theta: int = 0

#     def home(self, save=True, homing_voltage=2500, homing_rate=0.001):

#         # TODO Logging module
#         self.log.info(f"[{self._name}] Initiating Homing Routine.")

#         minpos_motor, minpos_joint, min_output = self._homing_routine(
#             direction=1.0, hvolt=homing_voltage, hrate=homing_rate
#         )
#         self.log.info(
#             f"[{self._name}] Minimum Motor angle: {minpos_motor}, Minimum Joint angle: {minpos_joint}"
#         )
#         time.sleep(0.5)
#         maxpos_motor, maxpos_joint, max_output = self._homing_routine(
#             direction=-1.0, hvolt=homing_voltage, hrate=homing_rate
#         )
#         self.log.info(
#             f"[{self.name}] Maximum Motor angle: {maxpos_motor}, Maximum Joint angle: {maxpos_joint}"
#         )

#         max_output = np.array(max_output).reshape((len(max_output), 2))
#         output_motor_count = max_output[:, 1]

#         _, ids = np.unique(output_motor_count, return_index=True)

#         if save:
#             self._save_encoder_map(data=max_output[ids])

#         self.log.info(f"[{self.name}] Homing Successfull.")

#     def _homing_routine(self, direction, hvolt=2500, hrate=0.001):
#         """Homing Routine

#         Args:
#             direction (_type_): _description_
#             hvolt (int, optional): _description_. Defaults to 2500.
#             hrate (float, optional): _description_. Defaults to 0.001.

#         Returns:
#             _type_: _description_
#         """
#         output = []
#         velocity_threshold = 0
#         go_on = True

#         self.update()
#         current_motor_position = self.motor_angle
#         current_joint_position = self.joint_angle

#         self.switch_state(JointState.VOLTAGE)
#         self.set_voltage(direction * hvolt)
#         time.sleep(0.05)
#         self.update()
#         cpos_motor = self.motor_angle
#         initial_velocity = self.joint_velocity
#         output.append([self.joint_angle * self._count2deg] + [cpos_motor])
#         velocity_threshold = abs(initial_velocity / 2.0)

#         while go_on:
#             time.sleep(hrate)
#             self.update()
#             cpos_motor = self.motor_angle
#             cvel_joint = self.joint_velocity
#             output.append([self.joint_angle * self._count2deg] + [cpos_motor])

#             if abs(cvel_joint) <= velocity_threshold:
#                 self.set_voltage(0)
#                 current_motor_position = self.motor_angle
#                 current_joint_position = self.joint_angle

#                 go_on = False

#         return current_motor_position, current_joint_position, output

#     def get_motor_count(self, desired_joint_angle):
#         """Returns Motor Count corresponding to the passed Joint angle value

#         Args:
#             desired_joint_angle (_type_): _description_

#         Returns:
#             _type_: _description_
#         """
#         if self._joint_angle_array is None:
#             self._load_encoder_map()

#         desired_motor_count = np.interp(
#             np.array(desired_joint_angle),
#             self._joint_angle_array,
#             self._motor_count_array,
#         )
#         return desired_motor_count

#     def switch_state(self, to_state: JointState = JointState.NEUTRAL):
#         self._state = to_state

#     def set_voltage(self, volt):
#         if self.state == JointState.VOLTAGE:
#             self._set_voltage(volt)

#     def set_current(self, current):
#         if self.state == JointState.CURRENT:
#             self._set_current_gains()
#             self._set_qaxis_current(current)
#         else:
#             self.log.warning("Joint State is incorrect.")

#     def set_position(self, position):
#         if self.state == JointState.POSITION:
#             self._set_position_gains()
#             self._set_motor_angle_counts(position)

#     def set_impedance(self, k: int = 300, b: int = 1600, theta: int = None):
#         self._k = k
#         self._b = b
#         self._theta = theta

#         if self.state == JointState.IMPEDANCE:
#             self._set_impedance_gains(K=k, B=b)
#             self._set_equilibrium_angle(theta=theta)

#     def _save_encoder_map(self, data):
#         """
#         Saves encoder_map: [Joint angle, Motor count] to a text file
#         """
#         np.savetxt(self._filename, data, fmt="%.5f")

#     def _load_encoder_map(self):
#         """
#         Loads Joint angle array, Motor count array, Min Joint angle, and Max Joint angle
#         """
#         data = np.loadtxt(self._filename, dtype=np.float64)
#         self._joint_angle_array = data[:, 0]
#         self._motor_count_array = np.array(data[:, 1], dtype=np.int32)

#         self._min_joint_angle = np.min(self._joint_angle_array)
#         self._max_joint_angle = np.max(self._joint_angle_array)

#         self._joint_angle_array = self._max_joint_angle - self._joint_angle_array

#         # Applying a median filter with a kernel size of 3
#         self._joint_angle_array = scipy.signal.medfilt(
#             self._joint_angle_array, kernel_size=3
#         )
#         self._motor_count_array = scipy.signal.medfilt(
#             self._motor_count_array, kernel_size=3
#         )

#     @property
#     def name(self):
#         return self._name

#     @property
#     def state(self):
#         return self._state

#     @property
#     def stiffness(self):
#         return self._k

#     @property
#     def damping(self):
#         return self._b

#     @property
#     def equilibrium_angle(self):
#         return self._theta


class Loadcell:
    def __init__(
        self,
        # joint: Joint,
        amp_gain: float = 125.0,
        exc: float = 5.0,
        loadcell_matrix=None,
        logger: logging.Logger = None,
    ) -> None:
        # self._joint = joint
        self._amp_gain = 125.0
        self._exc = 5.0

        if not loadcell_matrix:
            self._loadcell_matrix = np.array(
                [
                    (-38.72600, -1817.74700, 9.84900, 43.37400, -44.54000, 1824.67000),
                    (-8.61600, 1041.14900, 18.86100, -2098.82200, 31.79400, 1058.6230),
                    (
                        -1047.16800,
                        8.63900,
                        -1047.28200,
                        -20.70000,
                        -1073.08800,
                        -8.92300,
                    ),
                    (20.57600, -0.04000, -0.24600, 0.55400, -21.40800, -0.47600),
                    (-12.13400, -1.10800, 24.36100, 0.02300, -12.14100, 0.79200),
                    (-0.65100, -28.28700, 0.02200, -25.23000, 0.47300, -27.3070),
                ]
            )
        else:
            self._loadcell_matrix = loadcell_matrix

        self._loadcell_data = None
        self._loadcell_zero = np.zeros((1, 6), dtype=np.double)
        self._zeroed = False
        self.log = logger

    def reset(self):
        self._zeroed = False
        self._loadcell_zero = np.zeros((1, 6), dtype=np.double)

    def update(self, loadcell_zero=None):
        """
        Computes Loadcell data

        """

        # loadcell_signed = (self._joint.genvars - 2048) / 4095 * self._exc
        # loadcell_coupled = loadcell_signed * 1000 / (self._exc * self._amp_gain)

        # if loadcell_zero is None:
        #     self._loadcell_data = (
        #         np.transpose(self._loadcell_matrix.dot(np.transpose(loadcell_coupled)))
        #         - self._loadcell_zero
        #     )
        # else:
        #     self._loadcell_data = (
        #         np.transpose(self._loadcell_matrix.dot(np.transpose(loadcell_coupled)))
        #         - loadcell_zero
        #     )
        pass

    def initialize(self, number_of_iterations: int = 2000):
        """
        Obtains the initial loadcell reading (aka) loadcell_zero
        """
        ideal_loadcell_zero = np.zeros((1, 6), dtype=np.double)
        if not self._zeroed:
            pass
            # if self._joint.is_streaming:
            #     self._joint.update()
            #     self.update()
            #     self._loadcell_zero = self._loadcell_data

            #     for _ in range(number_of_iterations):
            #         self.update(ideal_loadcell_zero)
            #         loadcell_offset = self._loadcell_data
            #         self._loadcell_zero = (loadcell_offset + self._loadcell_zero) / 2.0

        elif input("Do you want to re-initialize loadcell? (Y/N)") == "Y":
            self.reset()
            self.initialize()

    @property
    def is_zeroed(self):
        return self._zeroed

    @property
    def fx(self):
        return self._loadcell_data[0][0]

    @property
    def fy(self):
        return self._loadcell_data[0][1]

    @property
    def fz(self):
        return self._loadcell_data[0][2]

    @property
    def mx(self):
        return self._loadcell_data[3]

    @property
    def my(self):
        return self._loadcell_data[4]

    @property
    def mz(self):
        return self._loadcell_data[5]


class Logger(logging.Logger):
    """
    Logger class is a class that logs attributes from a class to a csv file

    Methods:
        __init__(self, class_instance: object, file_path: str, logger: logging.Logger = None) -> None
        log(self) -> None
    """

    def __init__(
        self,
        file_path: str,
        log_format: str = "[%(asctime)s] %(levelname)s: %(message)s",
    ) -> None:

        self._file_path = file_path

        self._class_instances = []
        self._attributes = []

        self._file = open(self._file_path + ".csv", "w")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self._attributes)

        super().__init__(__name__)
        self.setLevel(logging.DEBUG)

        self._std_formatter = logging.Formatter(log_format)

        self._file_handler = RotatingFileHandler(
            self._file_path,
            mode="w",
            maxBytes=0,
            backupCount=10,
        )
        self._file_handler.setLevel(logging.DEBUG)
        self._file_handler.setFormatter(self._std_formatter)

        self._stream_handler = logging.StreamHandler()
        self._stream_handler.setLevel(logging.INFO)
        self._stream_handler.setFormatter(self._std_formatter)

        self.addHandler(self._stream_handler)
        self.addHandler(self._file_handler)

        self._is_logging = False

    def add_attributes(self, class_instance: object, attributes_str: list[str]) -> None:
        """
        Configures the logger to log the attributes of a class

        Args:
            class_instance (object): Class instance to log the attributes of
            attributes_str (list[str]): List of attributes to log
        """
        self._class_instances.append(class_instance)
        self._attributes.append(attributes_str)

    def data(self) -> None:
        """
        Logs the attributes of the class instance to the csv file
        """

        if not self._is_logging:
            for class_instance, attributes in zip(
                self._class_instances, self._attributes
            ):
                self._writer.writerow(
                    [
                        f"{class_instance.__class__.__name__}: {attribute}"
                        for attribute in attributes
                    ]
                )
            self._is_logging = True

        for class_instance, attributes in zip(self._class_instances, self._attributes):
            self._writer.writerow(
                [getattr(class_instance, attribute) for attribute in attributes]
            )

        self._file.flush()

    def close(self) -> None:
        """
        Closes the csv file
        """
        self._file.close()


class OpenSourceLeg:
    """
    OSL class: This class is the main class for the Open Source Leg project. It
    contains all the necessary functions to control the leg.

    Returns:
        none: none
    """

    # This is a singleton class
    _instance = None

    @staticmethod
    def get_instance():
        if OpenSourceLeg._instance is None:
            OpenSourceLeg()
        return OpenSourceLeg._instance

    def __init__(
        self, frequency: int = 200, log_data: bool = False, file_name: str = "./osl.log"
    ) -> None:

        super().__init__()

        self._fxs = None
        self._loadcell = None

        # self.joints: list[Joint] = []

        self._knee_id = None
        self._ankle_id = None

        self._frequency = frequency
        self.log = Logger(
            file_path=file_name, log_format="[%(asctime)s] %(levelname)s: %(message)s"
        )

        # self._initialize_logger(log_data=log_data, filename=file_name)

        self.loop = SoftRealtimeLoop(dt=1.0 / self._frequency, report=False, fade=0.1)

        self._units = DEFAULT_UNITS

    def __enter__(self):
        # for joint in self.joints:
        #     joint._start_streaming_data()

        if self._loadcell is not None:
            self._loadcell.initialize()

    def __exit__(self, type, value, tb):
        # for joint in self.joints:
        #     joint.switch_state()
        #     joint.shutdown()

        pass

    def __repr__(self) -> str:
        # return f"OSL object with {len(self.joints)} joints"
        return f"OSL object"

    # def add_joint(self, name: str, port, baud_rate, debug_level=0):

    #     if "knee" in name.lower():
    #         self._knee_id = len(self.joints)
    #     elif "ankle" in name.lower():
    #         self._ankle_id = len(self.joints)
    #     else:
    #         sys.exit("Joint can't be identified, kindly check the given name.")

    #     self.joints.append(
    #         Joint(
    #             name=name,
    #             fxs=self._fxs,
    #             port=port,
    #             baud_rate=baud_rate,
    #             frequency=self._frequency,
    #             logger=self.log,
    #             debug_level=debug_level,
    #         )
    #     )

    def add_loadcell(
        self,
        # joint: Joint,
        amp_gain: float = 125.0,
        exc: float = 5.0,
        loadcell_matrix=None,
    ):
        self._loadcell = Loadcell(
            # joint=joint,
            amp_gain=amp_gain,
            exc=exc,
            loadcell_matrix=loadcell_matrix,
            logger=self.log,
        )

    def _initialize_logger(self, log_data: bool = False, filename: str = "osl.log"):

        self._log_data = log_data
        self._log_filename = filename
        self.log = logging.getLogger(__name__)

        if log_data:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)

        self._std_formatter = logging.Formatter(
            "[%(asctime)s] %(levelname)s: %(message)s"
        )

        self._file_handler = RotatingFileHandler(
            self._log_filename,
            mode="w",
            maxBytes=0,
            backupCount=10,
        )
        self._file_handler.setLevel(logging.DEBUG)
        self._file_handler.setFormatter(self._std_formatter)

        self._stream_handler = logging.StreamHandler()
        self._stream_handler.setLevel(logging.INFO)
        self._stream_handler.setFormatter(self._std_formatter)

        self.log.addHandler(self._stream_handler)
        self.log.addHandler(self._file_handler)

    def update(self):
        # for joint in self.joints:
        #     joint.update()

        if self._loadcell is not None:
            print("hello")
            self._loadcell.update()

    # def home(self):
    #     for joint in self.joints:
    #         joint.home()

    @property
    def loadcell(self):
        if self._loadcell is not None:
            return self._loadcell
        else:
            sys.exit("Loadcell not connected.")

    # @property
    # def knee(self):
    #     if self._knee_id is not None:
    #         return self.joints[self._knee_id]
    #     else:
    #         sys.exit("Knee is not connected.")

    # @property
    # def ankle(self):
    #     if self._ankle_id is not None:
    #         return self.joints[self._ankle_id]
    #     else:
    #         sys.exit("Ankle is not connected.")

    @property
    def units(self):
        return self._units


if __name__ == "__main__":
    osl = OpenSourceLeg()
    print(osl.units.convert_to_default_units(1000, "voltage"))
