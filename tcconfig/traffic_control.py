# encoding: utf-8

"""
.. codeauthor:: Tsuyoshi Hombashi <gogogo.vm@gmail.com>
"""

from __future__ import absolute_import
from __future__ import division
import re

import dataproperty
from dataproperty import (
    FloatType
)
import six
from subprocrunner import SubprocessRunner

from ._common import (
    sanitize_network,
    verify_network_interface,
    run_command_helper,
)
from ._converter import Humanreadable
from ._error import (
    NetworkInterfaceNotFoundError,
    EmptyParameterError,
    InvalidParameterError
)
from ._iptables import IptablesMangleController
from ._logger import logger
from ._traffic_direction import TrafficDirection
from .shaper.tbf import TbfShaper


def _validate_within_min_max(param_name, value, min_value, max_value):
    if value is None:
        return

    if value > max_value:
        raise ValueError(
            "{:s} is too high: expected<={:f}[%], value={:f}[%]".format(
                param_name, max_value, value))

    if value < min_value:
        raise ValueError(
            "{:s} is too low: expected>={:f}[%], value={:f}[%]".format(
                param_name, min_value, value))


class TrafficControl(object):
    __NETEM_QDISC_MAJOR_ID_OFFSET = 10

    MIN_PACKET_LOSS_RATE = 0  # [%]
    MAX_PACKET_LOSS_RATE = 100  # [%]

    MIN_LATENCY_MS = 0  # [millisecond]
    MAX_LATENCY_MS = 10000  # [millisecond]

    MIN_CORRUPTION_RATE = 0  # [%]
    MAX_CORRUPTION_RATE = 100  # [%]

    __MIN_PORT = 0
    __MAX_PORT = 65535

    REGEXP_FILE_EXISTS = re.compile("RTNETLINK answers: File exists")

    EXISTS_MSG_TEMPLATE = (
        "{:s} "
        "execute with --overwrite option if you want to overwrite "
        "the existing settings.")

    @property
    def ifb_device(self):
        return "ifb{:d}".format(self.__qdisc_major_id)

    @property
    def direction(self):
        return self.__direction

    @property
    def bandwidth_rate(self):
        return self.__bandwidth_rate

    @property
    def latency_ms(self):
        return self.__latency_ms

    @property
    def latency_distro_ms(self):
        return self.__latency_distro_ms

    @property
    def packet_loss_rate(self):
        return self.__packet_loss_rate

    @property
    def corruption_rate(self):
        return self.__corruption_rate

    @property
    def network(self):
        return self.__network

    @property
    def src_network(self):
        return self.__src_network

    @property
    def port(self):
        return self.__port

    @property
    def is_enable_iptables(self):
        return self.__is_enable_iptables

    @property
    def qdisc_major_id(self):
        return self.__qdisc_major_id

    @property
    def qdisc_major_id_str(self):
        return "{:x}".format(self.__qdisc_major_id)

    def __init__(
            self, device,
            direction=None, bandwidth_rate=None,
            latency_ms=None, latency_distro_ms=None,
            packet_loss_rate=None, corruption_rate=None,
            network=None, port=None,
            src_network=None,
            is_enable_iptables=True,
    ):
        self.__device = device

        self.__direction = direction
        self.__latency_ms = latency_ms  # [milliseconds]
        self.__latency_distro_ms = latency_distro_ms  # [milliseconds]
        self.__packet_loss_rate = packet_loss_rate  # [%]
        self.__corruption_rate = corruption_rate  # [%]
        self.__network = network
        self.__src_network = src_network
        self.__port = port
        self.__is_enable_iptables = is_enable_iptables

        self.__qdisc_major_id = self.__get_device_qdisc_major_id()
        self.__shaper = TbfShaper(self)

        # bandwidth string [G/M/K bps]
        try:
            self.__bandwidth_rate = Humanreadable(
                kilo_size=1000).humanreadable_to_kilobyte(bandwidth_rate)
        except ValueError:
            self.__bandwidth_rate = None

        IptablesMangleController.enable = is_enable_iptables

    def validate(self):
        verify_network_interface(self.__device)
        self.__validate_netem_parameter()
        self.__network = sanitize_network(self.network)
        self.__src_network = sanitize_network(self.src_network)
        self.__validate_port()

    def validate_bandwidth_rate(self):
        if not dataproperty.FloatType(self.bandwidth_rate).is_type():
            raise EmptyParameterError("bandwidth_rate is empty")

        if self.bandwidth_rate <= 0:
            raise InvalidParameterError(
                "rate must be greater than zero: actual={}".format(
                    self.bandwidth_rate))

    def get_tc_device(self):
        if self.direction == TrafficDirection.OUTGOING:
            return self.__device

        if self.direction == TrafficDirection.INCOMING:
            return self.ifb_device

        raise ValueError("unknown direction: " + self.direction)

    def get_netem_qdisc_major_id(self, base_id):
        if self.direction == TrafficDirection.OUTGOING:
            direction_offset = 0
        elif self.direction == TrafficDirection.INCOMING:
            direction_offset = 1

        return (
            base_id +
            self.__NETEM_QDISC_MAJOR_ID_OFFSET +
            direction_offset)

    def set_tc(self):
        self.__setup_ifb()
        self.__shaper.set_shaping()

    def delete_tc(self):
        result_list = []

        returncode = run_command_helper(
            "tc qdisc del dev {:s} root".format(self.__device),
            re.compile("RTNETLINK answers: No such file or directory"),
            "failed to delete qdisc: no qdisc for outgoing packets")
        result_list.append(returncode == 0)

        returncode = run_command_helper(
            "tc qdisc del dev {:s} ingress".format(self.__device),
            re.compile("|".join([
                "RTNETLINK answers: Invalid argument",
                "RTNETLINK answers: No such file or directory",
            ])),
            "failed to delete qdisc: no qdisc for incomming packets")
        result_list.append(returncode == 0)

        try:
            result_list.append(self.__delete_ifb_device() == 0)
        except NetworkInterfaceNotFoundError as e:
            logger.debug(e)
            result_list.append(False)

        IptablesMangleController.clear()

        return any(result_list)

    def __validate_network_delay(self):
        _validate_within_min_max(
            "latency_ms",
            self.latency_ms,
            self.MIN_LATENCY_MS, self.MAX_LATENCY_MS)

        _validate_within_min_max(
            "latency_distro_ms",
            self.latency_distro_ms,
            self.MIN_LATENCY_MS, self.MAX_LATENCY_MS)

    def __validate_packet_loss_rate(self):
        _validate_within_min_max(
            "packet_loss_rate",
            self.packet_loss_rate,
            self.MIN_PACKET_LOSS_RATE, self.MAX_PACKET_LOSS_RATE)

    def __validate_corruption_rate(self):
        _validate_within_min_max(
            "corruption_rate",
            self.corruption_rate,
            self.MIN_CORRUPTION_RATE, self.MAX_CORRUPTION_RATE)

    def __validate_netem_parameter(self):
        try:
            self.validate_bandwidth_rate()
        except EmptyParameterError:
            pass

        self.__validate_network_delay()
        self.__validate_packet_loss_rate()
        self.__validate_corruption_rate()

        param_list = [
            self.bandwidth_rate,
            self.latency_ms,
            self.packet_loss_rate,
            self.corruption_rate,
        ]

        if all([
            not FloatType(value).is_type() or value == 0
            for value in param_list
        ]):
            raise ValueError("there is no valid net emulation parameter")

    def __validate_port(self):
        _validate_within_min_max(
            "port", self.port, self.__MIN_PORT, self.__MAX_PORT)

    def __get_device_qdisc_major_id(self):
        import hashlib

        base_device_hash = hashlib.md5(six.b(self.__device)).hexdigest()[:3]
        device_hash_prefix = "1"

        return int(device_hash_prefix + base_device_hash, 16)

    def __setup_ifb(self):
        if self.direction != TrafficDirection.INCOMING:
            return 0

        if dataproperty.is_empty_string(self.ifb_device):
            return -1

        return_code = 0

        command = "modprobe ifb"
        return_code |= SubprocessRunner(command).run()

        return_code |= run_command_helper(
            "ip link add {:s} type ifb".format(self.ifb_device),
            self.REGEXP_FILE_EXISTS,
            self.EXISTS_MSG_TEMPLATE.format(
                "failed to add ip link: ip link already exists."))

        command = "ip link set dev {:s} up".format(self.ifb_device)
        return_code |= SubprocessRunner(command).run()

        return_code |= run_command_helper(
            "tc qdisc add dev {:s} ingress".format(self.__device),
            self.REGEXP_FILE_EXISTS,
            self.EXISTS_MSG_TEMPLATE.format(
                "failed to add qdisc: ingress qdisc already exists."))

        command_list = [
            "tc filter add",
            "dev " + self.__device,
            "parent ffff: protocol ip u32 match u32 0 0",
            "flowid {:x}:".format(self.__get_device_qdisc_major_id()),
            "action mirred egress redirect",
            "dev " + self.ifb_device,
        ]
        return_code |= SubprocessRunner(" ".join(command_list)).run()

        return return_code

    def __delete_ifb_device(self):
        verify_network_interface(self.ifb_device)

        command_list = [
            "tc qdisc del dev {:s} root".format(self.ifb_device),
            "ip link set dev {:s} down".format(self.ifb_device),
            "ip link delete {:s} type ifb".format(self.ifb_device),
        ]

        if all([
            SubprocessRunner(command).run() != 0
            for command in command_list
        ]):
            return 2

        return 0
